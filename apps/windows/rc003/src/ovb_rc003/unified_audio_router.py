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
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import audio_capture_activity_windows, audio_output, audio_playback

STATUS_FILENAME = "unified_audio_status.json"
REMOTE_SAMPLE_RATE_HZ = 16000
RETRY_SECONDS = 2.0
STATUS_STALE_SECONDS = 7.0
QUEUE_BLOCKS = 96
REMOTE_DRAIN_TIMEOUT_SECONDS = 0.5
ACTIVITY_POLL_SECONDS = 0.2
IDLE_RELEASE_SECONDS = 0.8

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
    return _STATUS_TEXT.get(code, "统一虚拟输入状态未知；请重启桥接。")


def _write_status_file(root: Path, code: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "code": code,
        "updated_at": time.time(),
    }
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
            self._pending_fade_in.add("remote")
            self._discard_queued_locked()
            self._set_status_locked("remote")

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
            if worker is None:
                self._set_status_locked("stopped")
                return
            self._remote_active = False
            self._remote_accepting = False
            self._remote_ending = False
            self._stop_event.set()
            input_stream = self._input_stream
            self._input_stream = None
        self._close_stream(input_stream, "system input shutdown")
        self._wake_event.set()
        worker.join(timeout=5.0)
        if worker.is_alive():
            raise RuntimeError("unified audio worker did not stop")
        with self._lock:
            self._worker = None
            self._set_status_locked("stopped")

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
        return changed

    def _idle_release_due(self, now: float) -> bool:
        if not self._on_demand_system_input:
            return False
        with self._lock:
            return bool(
                self._input_stream is not None
                and not self._consumer_active
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
            if not consumer_active:
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

        def callback(indata, frames, callback_time, status) -> None:
            del frames, callback_time
            if status:
                with self._lock:
                    self._input_faulted = True
                    self._set_status_locked("system_input_interrupted")
                self._wake_event.set()
                return
            with self._lock:
                if self._remote_active or self._stop_event.is_set():
                    return
            try:
                self._queue.put_nowait(("system", indata.copy(), rate, channels))
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

    def _write_item(self, item) -> None:
        source, samples, rate, channels = item
        with self._lock:
            expected = "remote" if self._remote_active else "system"
            sink = self._sink
        if source != expected or sink is None:
            return
        try:
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
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _set_status(self, code: str) -> None:
        with self._lock:
            self._set_status_locked(code)

    def _set_status_locked(self, code: str) -> None:
        changed = code != self._status_code
        self._status_code = code
        if changed or time.monotonic() - self._last_status_write >= 2.0:
            try:
                _write_status_file(self._config_root, code)
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
