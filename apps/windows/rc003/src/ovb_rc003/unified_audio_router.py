"""Transparent system-microphone -> virtual-output routing with RC003 priority.

The router is constructed only when the opt-in setting is enabled.  One
worker owns the sole playback sink and serializes every write, so the system
microphone and RC003 can never write the virtual endpoint concurrently.
During an RC003 session the system capture stream is closed (not merely
mixed down).  In on-demand mode it is also closed while no application is
actively reading CABLE Output, and reopened only for a real consumer.  If
Windows session detection fails, routing falls back to continuous forwarding
so the established microphone behavior is preserved rather than silently lost.
"""

from __future__ import annotations

import json
import math
import os
import queue
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from . import (
    audio_capture_activity_windows,
    audio_output,
    audio_playback,
    microphone_auto_select,
    system_microphone_gain,
)

STATUS_FILENAME = "unified_audio_status.json"
REMOTE_SAMPLE_RATE_HZ = 16000
RETRY_SECONDS = 2.0
STATUS_STALE_SECONDS = 7.0
QUEUE_BLOCKS = 96
REMOTE_DRAIN_TIMEOUT_SECONDS = 0.5
ACTIVITY_POLL_SECONDS = 0.2
IDLE_RELEASE_SECONDS = 0.8
# The shortcut normally arrives before the user starts speaking.  Keep the
# level-only probe alive long enough for a natural pause after pressing it,
# but evaluate frequently so a clear microphone switches in quickly.
AUTOMATIC_PROBE_SECONDS = 3.0
AUTOMATIC_EVALUATION_SECONDS = 0.12
AUTOMATIC_MINIMUM_RMS = 24.0
AUTOMATIC_SWITCH_RATIO = 1.6
AUTOMATIC_PRE_ROLL_SECONDS = 1.5
AUTOMATIC_BASELINE_SECONDS = 0.24
AUTOMATIC_CONFIRM_WINDOWS = 2
AUTOMATIC_BASELINE_RATIO = 1.6
AUTOMATIC_ARM_GRACE_SECONDS = 0.8
AUTOMATIC_FAILURES_BEFORE_DEGRADE = 2
AUTOMATIC_DEGRADE_SECONDS = 30.0
AUTOMATIC_ALTERNATIVE_CONFIRM_WINDOWS = 5
AUTOMATIC_PREFERRED_HOLD_WINDOWS = 4
AUTOMATIC_ALTERNATIVE_VARIATION_RATIO = 1.15
AUTOMATIC_ALTERNATIVE_MIN_SECONDS = 1.0
# Some clients keep CABLE Output open for their entire lifetime, so the
# capture-session edge alone cannot represent each push-to-talk activation.
# Recheck live microphone evidence while that real consumer remains active.
# The probe itself lasts three seconds by default; this interval leaves a
# short gap instead of continuously holding every candidate endpoint open.
AUTOMATIC_DEMAND_RECHECK_SECONDS = 4.0
AUTOMATIC_DEMAND_SOURCE_ID = "capture-demand"

_STATUS_TEXT = {
    "starting": "正在启动统一虚拟输入…",
    "idle": "待机：没有软件使用 CABLE Output，系统麦克风已释放。",
    "system": "当前音源：系统麦克风",
    "remote": "当前音源：RC003 遥控器（系统麦克风已暂停）",
    "recovering": "正在恢复系统麦克风…",
    "output_unavailable": "错误：VB-CABLE 输出不可用，正在重试。",
    "system_input_unavailable": "错误：所选系统麦克风不可用或被占用，正在重试。",
    "output_write_failed": "错误：VB-CABLE 写入中断，已切回系统麦克风恢复流程。",
    "system_input_interrupted": "错误：系统麦克风连接中断，正在重试。",
    "queue_overflow": "错误：音频处理来不及，已安全停止当前 RC003 语音并恢复系统麦克风。",
    "demand_detection_failed": "错误：无法检测 CABLE Output 使用状态，已回退到持续转发系统麦克风。",
    "stopped": "统一虚拟输入已停止；未采集系统麦克风。",
}


def status_path(root: Path) -> Path:
    return root / STATUS_FILENAME


def read_status(root: Path) -> dict:
    path = status_path(root)
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def describe_status(root: Path, enabled: bool) -> str:
    if not enabled:
        return "已关闭；Remote Mic 不会采集系统麦克风。"
    value = read_status(root)
    updated_at = value.get("updated_at")
    if not isinstance(updated_at, (int, float)) or time.time() - updated_at > STATUS_STALE_SECONDS:
        return "尚无运行状态；保存后请启动或重启桥接。"
    code = str(value.get("code", ""))
    if code == "system":
        source_name = str(value.get("system_input_name", "")).strip()
        if source_name:
            return f"当前音源：{source_name}"
    return _STATUS_TEXT.get(code, "统一虚拟输入状态未知；请重启桥接。")


def _write_status_file(
    root: Path,
    code: str,
    *,
    system_input_name: str = "",
    system_input_host_api: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "code": code,
        "updated_at": time.time(),
    }
    if system_input_name:
        payload["system_input_name"] = str(system_input_name)
        payload["system_input_host_api"] = str(system_input_host_api)
    fd, temporary_name = tempfile.mkstemp(prefix=".unified-audio-", suffix=".tmp", dir=root)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, status_path(root))
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _apply_fade_in(samples, rate_hz: int, channels: int, duration_ms: int = 8):
    """Ramp, rather than discard, the first block after a source switch."""

    import numpy as np  # type: ignore

    array = np.asarray(samples, dtype="int16")
    original_shape = array.shape
    if array.size == 0:
        return array
    matrix = array.reshape(-1, channels).astype("float64")
    fade_frames = min(len(matrix), max(1, int(rate_hz * duration_ms / 1000)))
    matrix[:fade_frames] *= np.linspace(0.0, 1.0, fade_frames, endpoint=True).reshape(-1, 1)
    output = np.rint(matrix).clip(-32768, 32767).astype("int16")
    return output.reshape(original_shape)


class UnifiedAudioRouter:
    """Own one virtual output and switch it atomically between two sources."""

    def __init__(
        self,
        *,
        config_root: Path,
        output_name: str,
        output_host_api: str,
        system_input_name: str,
        system_input_host_api: str,
        logger,
        on_remote_failure: Optional[Callable[[], None]] = None,
        sink_factory=audio_playback.EndpointPlaybackSink,
        sounddevice_loader=None,
        retry_seconds: float = RETRY_SECONDS,
        on_demand_system_input: bool = False,
        activity_detector_factory=audio_capture_activity_windows.CableCaptureActivityDetector,
        activity_poll_seconds: float = ACTIVITY_POLL_SECONDS,
        idle_release_seconds: float = IDLE_RELEASE_SECONDS,
        automatic_probe_seconds: float = AUTOMATIC_PROBE_SECONDS,
        automatic_evaluation_seconds: float = AUTOMATIC_EVALUATION_SECONDS,
        automatic_minimum_rms: float = AUTOMATIC_MINIMUM_RMS,
        automatic_switch_ratio: float = AUTOMATIC_SWITCH_RATIO,
        automatic_pre_roll_seconds: float = AUTOMATIC_PRE_ROLL_SECONDS,
        automatic_baseline_seconds: float = AUTOMATIC_BASELINE_SECONDS,
        automatic_confirm_windows: int = AUTOMATIC_CONFIRM_WINDOWS,
        automatic_alternative_confirm_windows: int = (
            AUTOMATIC_ALTERNATIVE_CONFIRM_WINDOWS
        ),
        automatic_preferred_hold_windows: int = AUTOMATIC_PREFERRED_HOLD_WINDOWS,
        automatic_alternative_variation_ratio: float = (
            AUTOMATIC_ALTERNATIVE_VARIATION_RATIO
        ),
        automatic_alternative_min_seconds: float = AUTOMATIC_ALTERNATIVE_MIN_SECONDS,
        automatic_candidates=(),
        automatic_demand_recheck_seconds: float = (
            AUTOMATIC_DEMAND_RECHECK_SECONDS
        ),
        automatic_gain_enabled: bool = True,
    ) -> None:
        self._config_root = config_root
        self._output_name = output_name
        self._output_host_api = output_host_api
        self._system_input_name = system_input_name
        self._system_input_host_api = system_input_host_api
        self._logger = logger
        self._on_remote_failure = on_remote_failure
        self._sink_factory = sink_factory
        self._sounddevice_loader = sounddevice_loader or self._load_sounddevice
        self._retry_seconds = retry_seconds
        self._on_demand_system_input = bool(on_demand_system_input)
        self._activity_poll_seconds = max(0.05, float(activity_poll_seconds))
        self._idle_release_seconds = max(0.0, float(idle_release_seconds))
        self._automatic_probe_seconds = max(0.08, float(automatic_probe_seconds))
        self._automatic_evaluation_seconds = max(
            0.04, float(automatic_evaluation_seconds)
        )
        self._automatic_minimum_rms = max(0.0, float(automatic_minimum_rms))
        self._automatic_switch_ratio = max(1.05, float(automatic_switch_ratio))
        self._automatic_pre_roll_seconds = max(
            0.05, float(automatic_pre_roll_seconds)
        )
        self._automatic_baseline_seconds = max(
            0.0, float(automatic_baseline_seconds)
        )
        self._automatic_confirm_windows = max(1, int(automatic_confirm_windows))
        self._automatic_alternative_confirm_windows = max(
            2, int(automatic_alternative_confirm_windows)
        )
        self._automatic_preferred_hold_windows = max(
            1, int(automatic_preferred_hold_windows)
        )
        self._automatic_alternative_variation_ratio = max(
            1.01, float(automatic_alternative_variation_ratio)
        )
        self._automatic_alternative_min_seconds = max(
            0.0, float(automatic_alternative_min_seconds)
        )
        self._automatic_candidates = tuple(
            microphone_auto_select.recommended_candidates(automatic_candidates)
        )
        self._automatic_demand_recheck_seconds = max(
            self._automatic_probe_seconds,
            float(automatic_demand_recheck_seconds),
        )
        self._activity_detector = (
            activity_detector_factory() if self._on_demand_system_input else None
        )

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._worker: Optional[threading.Thread] = None
        self._sink = None
        self._input_stream = None
        self._input_rate_hz = 0
        self._input_channels = 0
        self._input_faulted = False
        self._remote_active = False
        self._remote_accepting = False
        self._remote_ending = False
        self._pending_fade_in = {"system"}
        self._consumer_active = not self._on_demand_system_input
        self._consumer_inactive_since: Optional[float] = None
        self._demand_detection_failed = False
        self._status_code = "stopped"
        self._last_status_write = 0.0
        self._automatic_generation = 0
        self._automatic_probe_source_id: Optional[str] = None
        self._automatic_demand_next_probe = 0.0
        self._automatic_activity: dict[
            microphone_auto_select.EndpointKey,
            microphone_auto_select.ActivityAccumulator,
        ] = {}
        self._automatic_buffers: dict[
            microphone_auto_select.EndpointKey,
            microphone_auto_select.PcmPreRollBuffer,
        ] = {}
        self._automatic_arm_until = 0.0
        self._automatic_source_last: dict[
            str, microphone_auto_select.EndpointKey
        ] = {}
        self._automatic_endpoint_failures: dict[
            microphone_auto_select.EndpointKey, int
        ] = {}
        self._automatic_endpoint_degraded_until: dict[
            microphone_auto_select.EndpointKey, float
        ] = {}
        self._automatic_endpoint_last_hard_failure: dict[
            microphone_auto_select.EndpointKey, float
        ] = {}
        self._automatic_probe_threads: set[threading.Thread] = set()
        self._system_microphone_gain = (
            system_microphone_gain.AdaptiveMicrophoneGain(
                self._config_root, enabled=automatic_gain_enabled
            )
        )

    @staticmethod
    def _load_sounddevice():
        import sounddevice as sd  # type: ignore

        return sd

    @property
    def remote_active(self) -> bool:
        with self._lock:
            return self._remote_active

    @property
    def status_code(self) -> str:
        with self._lock:
            return self._status_code

    @property
    def system_input_selection(self) -> tuple[str, str]:
        with self._lock:
            return self._system_input_name, self._system_input_host_api

    def select_system_input(
        self, name: str, host_api: str, *, pre_roll=None
    ) -> bool:
        """Switch the transparent system-microphone source safely.

        The current stream is detached under the routing lock and closed
        outside it.  The worker then opens the newly selected endpoint; the
        virtual output itself remains stable throughout the transition.
        """

        name = str(name).strip()
        host_api = str(host_api).strip()
        if not name or audio_output.is_cable_output_endpoint(name):
            if pre_roll is not None:
                self._zero_sensitive_samples(pre_roll[0])
            return False
        pre_roll_queued = False
        with self._lock:
            same_endpoint = (
                name == self._system_input_name
                and host_api == self._system_input_host_api
            )
            if same_endpoint and pre_roll is None:
                return True
            if same_endpoint:
                old_stream = None
            else:
                self._system_input_name = name
                self._system_input_host_api = host_api
                old_stream = self._input_stream
                self._input_stream = None
                self._input_rate_hz = 0
                self._input_channels = 0
                self._input_faulted = False
                self._discard_queued_locked()
                if not self._remote_active:
                    self._pending_fade_in.add("system")
                    self._set_status_locked("recovering")
            if pre_roll is not None and not self._remote_active:
                samples, rate, channels = pre_roll
                try:
                    self._queue.put_nowait(
                        ("system", samples, int(rate), int(channels), True)
                    )
                    pre_roll_queued = True
                except queue.Full:
                    self._zero_sensitive_samples(samples)
        if pre_roll is not None and not pre_roll_queued:
            self._zero_sensitive_samples(pre_roll[0])
        self._close_stream(old_stream, "system input source switch")
        self._wake_event.set()
        return True

    def _active_degraded_endpoints_locked(
        self,
        keys: list[microphone_auto_select.EndpointKey],
        now: float,
    ) -> set[microphone_auto_select.EndpointKey]:
        degraded = set()
        for key in keys:
            deadline = self._automatic_endpoint_degraded_until.get(key, 0.0)
            if deadline > now:
                degraded.add(key)
                continue
            if deadline:
                # A temporary demotion always gets another fair trial. A live
                # signal or successful reopen can clear it sooner.
                self._automatic_endpoint_degraded_until.pop(key, None)
                self._automatic_endpoint_failures.pop(key, None)
                self._automatic_endpoint_last_hard_failure.pop(key, None)
        return degraded

    def _note_automatic_endpoint_failure(
        self,
        key: microphone_auto_select.EndpointKey,
        *,
        hard: bool = False,
    ) -> None:
        key = (str(key[0]), str(key[1]))
        now = time.monotonic()
        with self._lock:
            if hard:
                last = self._automatic_endpoint_last_hard_failure.get(key, 0.0)
                # The pipeline retries periodically. Count one real attempt,
                # not every adjacent status heartbeat from the same outage.
                if now - last < max(1.0, self._retry_seconds * 0.75):
                    return
                self._automatic_endpoint_last_hard_failure[key] = now
            failures = self._automatic_endpoint_failures.get(key, 0) + 1
            self._automatic_endpoint_failures[key] = failures
            if hard or failures >= AUTOMATIC_FAILURES_BEFORE_DEGRADE:
                self._automatic_endpoint_degraded_until[key] = (
                    now + AUTOMATIC_DEGRADE_SECONDS
                )

    def _note_automatic_endpoint_success(
        self, key: microphone_auto_select.EndpointKey
    ) -> None:
        key = (str(key[0]), str(key[1]))
        with self._lock:
            self._automatic_endpoint_failures.pop(key, None)
            self._automatic_endpoint_degraded_until.pop(key, None)
            self._automatic_endpoint_last_hard_failure.pop(key, None)

    def request_automatic_system_input(
        self,
        source_id: str,
        candidates,
        *,
        fallback=None,
    ) -> bool:
        """Select immediately, then refine from short live level sampling.

        ``source_id`` is an install-salted anonymous keyboard fingerprint. It
        is used only as an in-memory last-choice cache: there is no fixed
        local/remote pair and any number of sources can participate.  Every
        candidate can win on every press, so one local keyboard may naturally
        use several microphones.
        """

        candidates = microphone_auto_select.recommended_candidates(candidates)
        if not candidates:
            return False
        old_buffers = []
        now = time.monotonic()
        source_id = str(source_id)
        # An unclassified hook edge may be local this time and remote the
        # next time. Reusing one winner across both would make a previous
        # ToDesk session poison the next H180 session (or vice versa).
        cacheable_source = (
            not source_id.startswith("shortcut:unknown:")
            and source_id != AUTOMATIC_DEMAND_SOURCE_ID
        )
        with self._lock:
            # RC003 has exclusive priority. Its own injected host shortcut
            # must never start probing/opening system microphones while the
            # remote voice stream is active.
            if self._remote_active:
                return False
            if not cacheable_source:
                # Remove values written by older builds as well. Otherwise an
                # in-process upgrade/restart path could leave a stale virtual
                # microphone associated with the shared unknown-hook key.
                self._automatic_source_last.pop(source_id, None)
            initial_keys = [
                microphone_auto_select.endpoint_key(item) for item in candidates
            ]
            degraded = self._active_degraded_endpoints_locked(initial_keys, now)
            if degraded and len(degraded) < len(candidates):
                candidates = [
                    item
                    for item in candidates
                    if microphone_auto_select.endpoint_key(item) not in degraded
                ] + [
                    item
                    for item in candidates
                    if microphone_auto_select.endpoint_key(item) in degraded
                ]
            keys = [microphone_auto_select.endpoint_key(item) for item in candidates]
            healthy_keys = [key for key in keys if key not in degraded]
            current = (self._system_input_name, self._system_input_host_api)
            preferred = (
                self._automatic_source_last.get(source_id)
                if cacheable_source
                else None
            )
            if preferred not in keys or (
                preferred in degraded and healthy_keys
            ):
                fallback_key = (
                    microphone_auto_select.endpoint_key(fallback)
                    if fallback is not None
                    else None
                )
                if fallback_key not in keys or (
                    fallback_key in degraded and healthy_keys
                ):
                    fallback_key = None
                current_key = (
                    current
                    if current in keys
                    and not (current in degraded and healthy_keys)
                    else None
                )
                preferred = fallback_key or current_key or keys[0]
            if cacheable_source:
                self._automatic_source_last[source_id] = preferred
            self._automatic_generation += 1
            generation = self._automatic_generation
            self._automatic_probe_source_id = source_id
            self._automatic_activity = {
                key: microphone_auto_select.ActivityAccumulator() for key in keys
            }
            old_buffers = list(self._automatic_buffers.values())
            buffers = {
                key: microphone_auto_select.PcmPreRollBuffer(
                    self._automatic_pre_roll_seconds
                )
                for key in keys
            }
            self._automatic_buffers = buffers
            # A real shortcut is stronger evidence than the capture-session
            # poll. Keep the input pipeline warm while Handy/Typeless opens
            # CABLE Output so the first syllable is not lost to a 200-ms poll.
            self._automatic_arm_until = max(
                self._automatic_arm_until,
                now
                + self._automatic_probe_seconds
                + AUTOMATIC_ARM_GRACE_SECONDS,
            )
            # A shortcut-triggered probe is a useful early hint. Do not let the
            # periodic capture-demand path immediately replace it with another
            # generation before it has collected enough speech evidence.
            self._automatic_demand_next_probe = max(
                self._automatic_demand_next_probe,
                now + self._automatic_demand_recheck_seconds,
            )

        for buffer in old_buffers:
            buffer.clear()

        # Route the last successful/fallback microphone immediately so the
        # first syllable is not held behind the short activity comparison.
        self.select_system_input(*preferred)
        worker = threading.Thread(
            target=self._run_automatic_probe,
            args=(
                generation,
                source_id,
                tuple(candidates),
                preferred,
                buffers,
                cacheable_source,
            ),
            name="RC003MicAutoSelect",
            daemon=True,
        )
        with self._lock:
            self._automatic_probe_threads.add(worker)
            # Publish and start atomically with respect to close(). Otherwise
            # shutdown can observe an unstarted Thread and join() raises before
            # the privacy-sensitive probe has a chance to clean up its buffers.
            worker.start()
        self._wake_event.set()
        return True

    def _run_automatic_probe(
        self,
        generation: int,
        source_id: str,
        candidates: tuple[audio_output.AudioEndpoint, ...],
        preferred: microphone_auto_select.EndpointKey,
        buffers: dict[
            microphone_auto_select.EndpointKey,
            microphone_auto_select.PcmPreRollBuffer,
        ],
        cacheable_source: bool,
    ) -> None:
        streams = []
        winner = None
        winner_due_to_hard_failure = False
        baselines: dict[microphone_auto_select.EndpointKey, float] = {}
        score_histories: dict[
            microphone_auto_select.EndpointKey, deque[float]
        ] = {
            microphone_auto_select.endpoint_key(item): deque(maxlen=12)
            for item in candidates
        }
        preferred_observed_speech = False
        preferred_active_windows = 0
        preferred_confirmed_for_press = False
        decision_reason = "probe_timeout"
        maximum_scores = {
            microphone_auto_select.endpoint_key(item): 0.0 for item in candidates
        }
        available_probe_keys: set[microphone_auto_select.EndpointKey] = set()
        started_at = time.monotonic()
        try:
            try:
                with self._lock:
                    current = (self._system_input_name, self._system_input_host_api)
                    current_stream_live = self._input_stream is not None
                    pipeline_worker_running = self._worker is not None
                    accumulators = dict(self._automatic_activity)
                try:
                    sd = self._sounddevice_loader()
                    for endpoint in candidates:
                        key = microphone_auto_select.endpoint_key(endpoint)
                        # The forwarding callback already measures the active
                        # endpoint; avoid opening a second handle to it.
                        if key == current and (
                            current_stream_live or pipeline_worker_running
                        ):
                            continue
                        accumulator = accumulators.get(key)
                        if accumulator is None:
                            continue
                        try:
                            stream = self._open_activity_probe_stream(
                                sd,
                                endpoint,
                                accumulator,
                                buffers.get(key),
                            )
                        except Exception:
                            # Busy/exclusive devices are skipped for this press.
                            continue
                        streams.append(stream)
                        available_probe_keys.add(key)
                    deadline = started_at + self._automatic_probe_seconds
                    while not self._stop_event.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._stop_event.wait(
                            min(self._automatic_evaluation_seconds, remaining)
                        )
                        with self._lock:
                            if generation != self._automatic_generation:
                                return
                            preferred_hard_failed = (
                                self._automatic_endpoint_last_hard_failure.get(
                                    preferred, 0.0
                                )
                                > started_at
                                and self._automatic_endpoint_degraded_until.get(
                                    preferred, 0.0
                                )
                                > time.monotonic()
                            )
                        if preferred_hard_failed:
                            winner = next(
                                (
                                    key
                                    for key in (
                                        microphone_auto_select.endpoint_key(item)
                                        for item in candidates
                                    )
                                    if key != preferred
                                    and key in available_probe_keys
                                ),
                                None,
                            )
                            if winner is not None:
                                winner_due_to_hard_failure = True
                                decision_reason = "preferred_open_failed"
                                break
                        raw_interval_levels = {
                            key: value.take_interval()
                            for key, value in accumulators.items()
                        }
                        interval_levels = {}
                        for key, level in raw_interval_levels.items():
                            learned_db = self._system_microphone_gain.gain_db_for(key)
                            learned_scale = math.pow(10.0, learned_db / 20.0)
                            interval_levels[key] = (
                                microphone_auto_select.ActivityLevel(
                                    rms=level.rms * learned_scale,
                                    peak=min(
                                        32767,
                                        int(level.peak * learned_scale),
                                    ),
                                    samples=level.samples,
                                )
                            )
                        elapsed = time.monotonic() - started_at
                        if elapsed <= self._automatic_baseline_seconds:
                            for key, level in interval_levels.items():
                                if level.samples:
                                    previous = baselines.get(key)
                                    baselines[key] = (
                                        level.rms
                                        if previous is None
                                        else min(previous, level.rms)
                                    )
                            continue
                        speech_levels = {}
                        for key, level in interval_levels.items():
                            baseline = baselines.get(key, 0.0)
                            threshold = max(
                                self._automatic_minimum_rms,
                                baseline * AUTOMATIC_BASELINE_RATIO,
                            )
                            if level.samples and level.rms >= threshold:
                                # Compare excess above each endpoint's own
                                # noise floor, not raw sensitivity alone.
                                score = level.rms / max(
                                    baseline,
                                    self._automatic_minimum_rms / 2.0,
                                )
                                speech_levels[key] = (
                                    microphone_auto_select.ActivityLevel(
                                        rms=score * self._automatic_minimum_rms,
                                        peak=level.peak,
                                        samples=level.samples,
                                    )
                                )
                        for key, history in score_histories.items():
                            level = speech_levels.get(key)
                            score = level.rms if level is not None else 0.0
                            history.append(score)
                            maximum_scores[key] = max(maximum_scores[key], score)
                        if preferred in speech_levels:
                            preferred_observed_speech = True
                            preferred_active_windows += 1
                            if (
                                preferred_active_windows
                                >= self._automatic_confirm_windows
                            ):
                                preferred_confirmed_for_press = True
                        else:
                            preferred_active_windows = 0
                        # Once the already-routed microphone supplies
                        # confirmed speech for this key activation, keep it
                        # for the whole press. A natural pause near the end of
                        # a sentence must not give a louder background source
                        # a chance to replace it and cut off the final words.
                        if preferred_confirmed_for_press:
                            decision_reason = "preferred_confirmed_for_press"
                            continue
                        if elapsed < self._automatic_alternative_min_seconds:
                            decision_reason = "alternative_startup_guard"
                            continue
                        candidate_winner, decision_reason = (
                            microphone_auto_select.choose_sustained_alternative(
                                score_histories,
                                preferred=preferred,
                                minimum_rms=self._automatic_minimum_rms,
                                minimum_active_windows=(
                                    self._automatic_alternative_confirm_windows
                                ),
                                preferred_hold_windows=(
                                    self._automatic_preferred_hold_windows
                                ),
                                variation_ratio=(
                                    self._automatic_alternative_variation_ratio
                                ),
                                dominance_ratio=self._automatic_switch_ratio,
                            )
                        )
                        if candidate_winner is not None:
                            winner = candidate_winner
                            break
                except Exception:
                    self._logger.exception(
                        "unified audio: automatic microphone probe unavailable"
                    )
            finally:
                for stream in streams:
                    self._close_stream(
                        stream, "automatic microphone activity probe"
                    )

            with self._lock:
                if generation != self._automatic_generation or self._stop_event.is_set():
                    return
                if winner is not None:
                    if winner == preferred:
                        self._note_automatic_endpoint_success(winner)
                    else:
                        # A different microphone winning is not proof that the
                        # preferred one is broken. Demote it only when it
                        # supplied no credible speech at all during this probe.
                        if (
                            not winner_due_to_hard_failure
                            and not preferred_observed_speech
                        ):
                            self._note_automatic_endpoint_failure(preferred)
                        self._note_automatic_endpoint_success(winner)
                    if cacheable_source:
                        self._automatic_source_last[source_id] = winner
            if winner is not None:
                winner_buffer = buffers.get(winner)
                pre_roll = winner_buffer.drain() if winner_buffer else None
                for key, buffer in buffers.items():
                    if key != winner:
                        buffer.clear()
                self.select_system_input(*winner, pre_roll=pre_roll)
                self._logger.info(
                    "automatic microphone decision: switch %s -> %s; "
                    "reason=%s; elapsed=%.2fs; max_scores=%s",
                    preferred[0],
                    winner[0],
                    decision_reason,
                    time.monotonic() - started_at,
                    {
                        key[0]: round(value, 2)
                        for key, value in maximum_scores.items()
                    },
                )
            else:
                for buffer in buffers.values():
                    buffer.clear()
                self._logger.info(
                    "automatic microphone decision: retain %s; reason=%s; "
                    "elapsed=%.2fs; max_scores=%s",
                    preferred[0],
                    decision_reason,
                    time.monotonic() - started_at,
                    {
                        key[0]: round(value, 2)
                        for key, value in maximum_scores.items()
                    },
                )
        except Exception:
            # Automatic refinement is optional; the immediate fallback route
            # selected before this worker started must remain usable.
            self._logger.exception(
                "unified audio: automatic microphone selection failed"
            )
        finally:
            for buffer in buffers.values():
                buffer.clear()
            current_thread = threading.current_thread()
            with self._lock:
                if generation == self._automatic_generation:
                    self._automatic_buffers = {}
                    self._automatic_activity = {}
                    self._automatic_probe_source_id = None
                self._automatic_probe_threads.discard(current_thread)

    def start(self) -> None:
        with self._lock:
            if self._worker is not None:
                return
            self._stop_event.clear()
            self._set_status_locked("starting")
            worker = threading.Thread(
                target=self._run_worker,
                name="RC003UnifiedAudio",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def finish_automatic_system_input(self) -> None:
        """End one shortcut-driven selection and erase all pending PCM."""

        with self._lock:
            self._automatic_generation += 1
            self._automatic_probe_source_id = None
            buffers = list(self._automatic_buffers.values())
            self._automatic_buffers = {}
            self._automatic_activity = {}
            self._automatic_arm_until = 0.0
            self._discard_sensitive_queued_locked()
        for buffer in buffers:
            buffer.clear()
        self._wake_event.set()

    def begin_remote(self) -> bool:
        """Give RC003 exclusive priority; return False if output is unavailable."""

        with self._lock:
            if self._remote_active and self._sink is not None:
                if self._remote_ending:
                    self._remote_ending = False
                    self._remote_accepting = True
                return True
            if self._sink is None or self._stop_event.is_set():
                return False
            self._remote_active = True
            self._remote_accepting = True
            self._remote_ending = False
            input_stream = self._input_stream
            self._input_stream = None
            self._input_rate_hz = 0
            self._input_channels = 0
            self._input_faulted = False
            self._automatic_generation += 1
            self._automatic_probe_source_id = None
            automatic_buffers = list(self._automatic_buffers.values())
            self._automatic_buffers = {}
            self._automatic_activity = {}
            self._automatic_arm_until = 0.0
            self._pending_fade_in.add("remote")
            self._discard_queued_locked()
            self._set_status_locked("remote")

        for buffer in automatic_buffers:
            buffer.clear()

        # Stop capture before accepting the first RC003 block. The source flag
        # was changed first, so a callback already in flight is discarded.
        self._close_stream(input_stream, "system input pause")
        with self._write_lock:
            with self._lock:
                sink = self._sink
            if sink is not None:
                sink.write_fade_to_silence()
                sink.reset_conversion()
        self._wake_event.set()
        return True

    def write_remote(self, samples) -> bool:
        with self._lock:
            if self._remote_active and not self._remote_accepting:
                # A release/stop cutoff was already established. Late BLE
                # callbacks are intentionally ignored, not treated as a new
                # routing failure that would trigger a reconnect loop.
                return True
            if not self._remote_active or self._sink is None or self._stop_event.is_set():
                return False
        try:
            self._queue.put_nowait(("remote", list(samples), REMOTE_SAMPLE_RATE_HZ, 1))
            return True
        except queue.Full:
            self._fail_remote("queue_overflow")
            return False

    def end_remote(self) -> None:
        """Restore the system microphone after stop, disconnect, or failure."""

        with self._lock:
            if not self._remote_active:
                return
            if self._remote_ending:
                return
            self._remote_ending = True
            self._remote_accepting = False

        # Preserve every RC003 block accepted before the release/ATVV stop.
        # A queue barrier lets the sole output worker finish them before the
        # source flag changes; no fixed sleep or blind queue discard is used.
        barrier = threading.Event()
        try:
            self._queue.put(("barrier", barrier, 0, 0), timeout=0.05)
            barrier.wait(REMOTE_DRAIN_TIMEOUT_SECONDS)
        except queue.Full:
            pass

        with self._lock:
            if not self._remote_active or not self._remote_ending:
                self._remote_ending = False
                return
            self._remote_active = False
            self._remote_ending = False
            self._pending_fade_in.add("system")
            self._discard_queued_locked()
            self._set_status_locked("recovering")
        with self._write_lock:
            with self._lock:
                sink = self._sink
            if sink is not None:
                try:
                    sink.write_fade_to_silence()
                    sink.reset_conversion()
                except Exception:
                    self._handle_output_failure("output_write_failed")
        self._wake_event.set()

    def close(self) -> None:
        with self._lock:
            worker = self._worker
            self._automatic_generation += 1
            probe_threads = tuple(self._automatic_probe_threads)
            automatic_buffers = list(self._automatic_buffers.values())
            self._automatic_buffers = {}
            self._automatic_activity = {}
            self._automatic_arm_until = 0.0
            if worker is None:
                self._set_status_locked("stopped")
                self._stop_event.set()
                input_stream = None
            else:
                self._remote_active = False
                self._remote_accepting = False
                self._remote_ending = False
                self._stop_event.set()
                input_stream = self._input_stream
                self._input_stream = None
            self._discard_queued_locked()
        for buffer in automatic_buffers:
            buffer.clear()
        self._close_stream(input_stream, "system input shutdown")
        self._wake_event.set()
        for probe_thread in probe_threads:
            probe_thread.join(timeout=1.0)
        if worker is None:
            self._save_system_microphone_gain()
            return
        try:
            worker.join(timeout=5.0)
            if worker.is_alive():
                raise RuntimeError("unified audio worker did not stop")
        finally:
            self._save_system_microphone_gain()
        with self._lock:
            self._worker = None
            self._set_status_locked("stopped")

    def _save_system_microphone_gain(self) -> None:
        try:
            self._system_microphone_gain.close()
        except Exception:
            # Learned gain is an optimization. A read-only/full config
            # directory must not prevent the audio router from shutting down.
            self._logger.exception(
                "unified audio: automatic microphone gain profile save failed"
            )

    def _run_worker(self) -> None:
        next_retry = 0.0
        next_activity_poll = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                activity_changed = False
                if self._on_demand_system_input and now >= next_activity_poll:
                    activity_changed = self._refresh_capture_demand(now)
                    next_activity_poll = now + self._activity_poll_seconds
                retry_due = now >= next_retry
                if retry_due or activity_changed or self._idle_release_due(now):
                    self._ensure_pipeline(now)
                self._maybe_probe_for_capture_demand(now)
                if retry_due:
                    next_retry = now + self._retry_seconds
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    item = None
                if item is not None:
                    if item[0] == "barrier":
                        item[1].set()
                    else:
                        self._write_item(item)
                self._write_heartbeat_if_due()
                if self._wake_event.is_set():
                    self._wake_event.clear()
                    next_retry = 0.0
                    next_activity_poll = 0.0
        finally:
            with self._lock:
                input_stream = self._input_stream
                sink = self._sink
                self._input_stream = None
                self._sink = None
            self._close_stream(input_stream, "system input worker shutdown")
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    self._logger.exception("unified audio: output shutdown failed")
            detector = self._activity_detector
            if detector is not None:
                try:
                    detector.close()
                except Exception:
                    self._logger.exception(
                        "unified audio: capture-activity detector shutdown failed"
                    )

    def _refresh_capture_demand(self, now: float) -> bool:
        detector = self._activity_detector
        if detector is None:
            return False
        try:
            active = bool(detector.is_active())
            failed = False
        except Exception:  # noqa: BLE001 - preserve audio on any detector failure
            active = True
            failed = True
            if not self._demand_detection_failed:
                self._logger.exception(
                    "unified audio: CABLE Output activity detection failed; "
                    "falling back to continuous system-mic forwarding"
                )
        with self._lock:
            changed = (
                active != self._consumer_active
                or failed != self._demand_detection_failed
            )
            self._consumer_active = active
            self._demand_detection_failed = failed
            if active:
                self._consumer_inactive_since = None
            elif self._consumer_inactive_since is None:
                self._consumer_inactive_since = now
            if not active:
                self._automatic_demand_next_probe = 0.0
        return changed

    def _maybe_probe_for_capture_demand(self, now: float) -> bool:
        """Use a real CABLE consumer as a key-independent selection trigger.

        A shortcut remains the fastest hint when Windows exposes it, but remote
        control products are allowed to bypass both Raw Input and the low-level
        hook.  A live capture session therefore starts the same privacy-safe
        aggregate-level comparison. Long-lived consumers are rechecked because
        their session state may not toggle for each utterance.
        """

        with self._lock:
            if (
                not self._on_demand_system_input
                or not self._consumer_active
                or self._demand_detection_failed
                or self._remote_active
                or not self._automatic_candidates
                or self._automatic_probe_threads
                or now < self._automatic_demand_next_probe
            ):
                return False
            candidates = self._automatic_candidates
            candidate_keys = {
                microphone_auto_select.endpoint_key(item) for item in candidates
            }
            current = audio_output.AudioEndpoint(
                self._system_input_name, self._system_input_host_api
            )
            fallback = (
                current
                if microphone_auto_select.endpoint_key(current) in candidate_keys
                else candidates[0]
            )
            # Reserve the next slot before leaving the lock. A simultaneous
            # keyboard hint may supersede this generation, but the worker will
            # not create duplicate demand probes of its own.
            self._automatic_demand_next_probe = (
                now + self._automatic_demand_recheck_seconds
            )

        started = self.request_automatic_system_input(
            AUTOMATIC_DEMAND_SOURCE_ID,
            candidates,
            fallback=fallback,
        )
        if started:
            self._logger.info(
                "automatic system microphone activity check started via "
                "CABLE Output consumer demand"
            )
        return started

    def _idle_release_due(self, now: float) -> bool:
        if not self._on_demand_system_input:
            return False
        with self._lock:
            return bool(
                self._input_stream is not None
                and not self._consumer_active
                and now >= self._automatic_arm_until
                and self._consumer_inactive_since is not None
                and now - self._consumer_inactive_since >= self._idle_release_seconds
            )

    def _ensure_pipeline(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            sink_missing = self._sink is None
        if sink_missing:
            try:
                audio_output.resolve_selected_endpoint(
                    audio_output.enumerate_output_endpoints(),
                    self._output_name,
                    self._output_host_api,
                )
                sink = self._sink_factory(self._output_name, self._output_host_api)
                sink.open()
            except Exception:
                self._logger.exception("unified audio: virtual output unavailable")
                self._set_status("output_unavailable")
                return
            with self._lock:
                if self._stop_event.is_set():
                    close_new_sink = True
                else:
                    self._sink = sink
                    close_new_sink = False
            if close_new_sink:
                sink.close()
                return

        with self._lock:
            remote_active = self._remote_active
            input_stream = self._input_stream
            input_faulted = self._input_faulted
        if remote_active:
            self._set_status("remote")
            return

        if self._on_demand_system_input:
            with self._lock:
                consumer_active = self._consumer_active
                inactive_since = self._consumer_inactive_since
                automatically_armed = now < self._automatic_arm_until
            if not consumer_active and not automatically_armed:
                release_now = (
                    input_stream is None
                    or inactive_since is None
                    or now - inactive_since >= self._idle_release_seconds
                )
                if release_now:
                    with self._lock:
                        idle_stream = self._input_stream
                        self._input_stream = None
                        self._input_rate_hz = 0
                        self._input_channels = 0
                        self._input_faulted = False
                        self._discard_queued_locked()
                        self._set_status_locked("idle")
                    self._close_stream(idle_stream, "idle system input release")
                    return
                self._set_status("system")
                return

        if input_stream is not None:
            try:
                if input_faulted:
                    raise RuntimeError("input callback reported a fault")
                if hasattr(input_stream, "active") and not input_stream.active:
                    raise RuntimeError("input stream inactive")
            except Exception:
                with self._lock:
                    if self._input_stream is input_stream:
                        self._input_stream = None
                        self._input_faulted = False
                self._close_stream(input_stream, "inactive system input")
                self._set_status("system_input_interrupted")
                return
            self._set_status(
                "demand_detection_failed"
                if self._demand_detection_failed
                else "system"
            )
            return

        try:
            stream, rate, channels = self._open_system_input()
        except Exception:
            with self._lock:
                failed_key = (
                    self._system_input_name,
                    self._system_input_host_api,
                )
            self._note_automatic_endpoint_failure(failed_key, hard=True)
            self._logger.exception("unified audio: selected system input unavailable")
            self._set_status("system_input_unavailable")
            return
        with self._lock:
            if self._remote_active or self._stop_event.is_set():
                close_new_stream = True
            else:
                self._input_stream = stream
                self._input_rate_hz = rate
                self._input_channels = channels
                self._input_faulted = False
                close_new_stream = False
                self._set_status_locked(
                    "demand_detection_failed"
                    if self._demand_detection_failed
                    else "system"
                )
        if close_new_stream:
            self._close_stream(stream, "late system input open")

    def _open_system_input(self):
        if audio_output.is_cable_output_endpoint(self._system_input_name):
            raise audio_output.AudioOutputUnavailableError(
                "CABLE Output cannot be used as the system input"
            )
        sd = self._sounddevice_loader()
        endpoints = audio_output.enumerate_input_endpoints()
        audio_output.resolve_selected_endpoint(
            endpoints,
            self._system_input_name,
            self._system_input_host_api,
        )
        host_apis = sd.query_hostapis()
        matches = []
        devices = sd.query_devices()
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels") or 0) <= 0:
                continue
            if device.get("name") != self._system_input_name:
                continue
            host_api_name = host_apis[device["hostapi"]]["name"] if host_apis else ""
            if self._system_input_host_api and host_api_name != self._system_input_host_api:
                continue
            matches.append((index, device))
        if len(matches) != 1:
            raise audio_output.AudioOutputUnavailableError(
                "selected system input is missing or ambiguous"
            )
        device_index, device = matches[0]
        max_channels = int(device.get("max_input_channels") or 0)
        channel_candidates = [2, 1] if max_channels >= 2 else [1]
        preferred = int(device.get("default_samplerate") or 0)
        candidates = [preferred, 48000, 44100, 16000]
        rate = 0
        channels = 0
        for channel_count in channel_candidates:
            for candidate in dict.fromkeys(value for value in candidates if value > 0):
                try:
                    sd.check_input_settings(
                        device=device_index,
                        channels=channel_count,
                        dtype="int16",
                        samplerate=candidate,
                    )
                    channels = channel_count
                    rate = candidate
                    break
                except Exception:
                    continue
            if rate > 0:
                break
        if rate <= 0:
            raise audio_output.AudioOutputUnavailableError(
                "selected system input has no supported PCM format"
            )

        capture_key = (self._system_input_name, self._system_input_host_api)

        def callback(indata, frames, callback_time, status) -> None:
            del frames, callback_time
            if status:
                with self._lock:
                    self._input_faulted = True
                    self._set_status_locked("system_input_interrupted")
                self._wake_event.set()
                return
            with self._lock:
                activity = self._automatic_activity.get(capture_key)
                if self._remote_active or self._stop_event.is_set():
                    return
            if activity is not None:
                activity.add(indata)
            forwarded = self._system_microphone_gain.process(
                capture_key, indata
            )
            try:
                self._queue.put_nowait(("system", forwarded, rate, channels))
            except queue.Full:
                # Dropping a system block is safer than blocking PortAudio's
                # callback thread. The next retry/status heartbeat remains live.
                with self._lock:
                    self._input_faulted = True
                    self._set_status_locked("system_input_interrupted")
                self._wake_event.set()

        stream = sd.InputStream(
            device=device_index,
            channels=channels,
            dtype="int16",
            samplerate=rate,
            latency="low",
            callback=callback,
        )
        stream.start()
        return stream, rate, channels

    def _open_activity_probe_stream(
        self,
        sd,
        endpoint: audio_output.AudioEndpoint,
        accumulator: microphone_auto_select.ActivityAccumulator,
        pre_roll_buffer: Optional[
            microphone_auto_select.PcmPreRollBuffer
        ] = None,
    ):
        """Open one short-lived level-only stream for automatic selection."""

        host_apis = sd.query_hostapis()
        matches = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels") or 0) <= 0:
                continue
            if device.get("name") != endpoint.name:
                continue
            host_api_name = host_apis[device["hostapi"]]["name"] if host_apis else ""
            if endpoint.host_api and host_api_name != endpoint.host_api:
                continue
            matches.append((index, device))
        if len(matches) != 1:
            raise audio_output.AudioOutputUnavailableError(
                "automatic microphone candidate is missing or ambiguous"
            )
        device_index, device = matches[0]
        max_channels = int(device.get("max_input_channels") or 0)
        channel_candidates = [2, 1] if max_channels >= 2 else [1]
        preferred_rate = int(device.get("default_samplerate") or 0)
        rate = 0
        channels = 0
        for channel_count in channel_candidates:
            for candidate_rate in dict.fromkeys(
                value for value in (preferred_rate, 48000, 44100, 16000) if value > 0
            ):
                try:
                    sd.check_input_settings(
                        device=device_index,
                        channels=channel_count,
                        dtype="int16",
                        samplerate=candidate_rate,
                    )
                    rate = candidate_rate
                    channels = channel_count
                    break
                except Exception:
                    continue
            if rate:
                break
        if not rate:
            raise audio_output.AudioOutputUnavailableError(
                "automatic microphone candidate has no supported PCM format"
            )

        def callback(indata, frames, callback_time, status) -> None:
            del frames, callback_time
            if not status:
                accumulator.add(indata)
                if pre_roll_buffer is not None:
                    pre_roll_buffer.add(indata, rate, channels)

        stream = sd.InputStream(
            device=device_index,
            channels=channels,
            dtype="int16",
            samplerate=rate,
            latency="low",
            callback=callback,
        )
        stream.start()
        return stream

    def _write_item(self, item) -> None:
        source, samples, rate, channels = item[:4]
        sensitive_pre_roll = len(item) > 4 and bool(item[4])
        sensitive_samples = samples
        try:
            with self._lock:
                expected = "remote" if self._remote_active else "system"
                sink = self._sink
            if source != expected or sink is None:
                return
            with self._write_lock:
                with self._lock:
                    expected = "remote" if self._remote_active else "system"
                    if source != expected or sink is not self._sink:
                        return
                    fade_in = source in self._pending_fade_in
                    self._pending_fade_in.discard(source)
                if fade_in:
                    samples = _apply_fade_in(samples, rate, channels)
                sink.write_pcm(samples, rate, channels)
        except Exception:
            self._logger.exception("unified audio: virtual output write failed")
            self._handle_output_failure("output_write_failed")
        finally:
            if sensitive_pre_roll:
                self._zero_sensitive_samples(sensitive_samples)
                if samples is not sensitive_samples:
                    self._zero_sensitive_samples(samples)

    def _handle_output_failure(self, code: str) -> None:
        with self._lock:
            was_remote = self._remote_active
            self._remote_active = False
            self._remote_accepting = False
            self._remote_ending = False
            sink = self._sink
            input_stream = self._input_stream
            self._sink = None
            self._input_stream = None
            self._input_faulted = False
            self._pending_fade_in.add("system")
            self._discard_queued_locked()
            self._set_status_locked(code)
        self._close_stream(input_stream, "output failure system input reset")
        if sink is not None:
            try:
                sink.close()
            except Exception:
                self._logger.exception("unified audio: failed output close failed")
        self._wake_event.set()
        if was_remote and self._on_remote_failure is not None:
            self._on_remote_failure()

    def _fail_remote(self, code: str) -> None:
        with self._lock:
            was_remote = self._remote_active
            self._remote_active = False
            self._remote_accepting = False
            self._remote_ending = False
            self._pending_fade_in.add("system")
            self._discard_queued_locked()
            self._set_status_locked(code)
            sink = self._sink
        with self._write_lock:
            if sink is not None:
                try:
                    sink.write_fade_to_silence()
                    sink.reset_conversion()
                except Exception:
                    self._handle_output_failure("output_write_failed")
        self._wake_event.set()
        if was_remote and self._on_remote_failure is not None:
            self._on_remote_failure()

    def _discard_queued_locked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
                if (
                    isinstance(item, tuple)
                    and len(item) > 4
                    and bool(item[4])
                ):
                    self._zero_sensitive_samples(item[1])
            except queue.Empty:
                return

    def _discard_sensitive_queued_locked(self) -> None:
        """Erase pre-roll without dropping ordinary live tail blocks."""

        retained = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if (
                isinstance(item, tuple)
                and len(item) > 4
                and bool(item[4])
            ):
                self._zero_sensitive_samples(item[1])
            else:
                retained.append(item)
        for item in retained:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                break

    @staticmethod
    def _zero_sensitive_samples(samples) -> None:
        try:
            samples.fill(0)
        except AttributeError:
            try:
                for index in range(len(samples)):
                    samples[index] = 0
            except (TypeError, IndexError):
                pass

    def _set_status(self, code: str) -> None:
        with self._lock:
            self._set_status_locked(code)

    def _set_status_locked(self, code: str) -> None:
        changed = code != self._status_code
        self._status_code = code
        if changed or time.monotonic() - self._last_status_write >= 2.0:
            try:
                _write_status_file(
                    self._config_root,
                    code,
                    system_input_name=self._system_input_name,
                    system_input_host_api=self._system_input_host_api,
                )
                self._last_status_write = time.monotonic()
            except OSError:
                self._logger.exception("unified audio: status write failed")

    def _write_heartbeat_if_due(self) -> None:
        with self._lock:
            if time.monotonic() - self._last_status_write >= 2.0:
                self._set_status_locked(self._status_code)

    def _close_stream(self, stream, label: str) -> None:
        if stream is None:
            return
        try:
            try:
                stream.stop()
            finally:
                stream.close()
        except Exception:
            self._logger.exception("unified audio: %s close failed", label)
