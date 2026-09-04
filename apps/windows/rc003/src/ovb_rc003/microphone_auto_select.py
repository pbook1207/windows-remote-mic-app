"""Automatic system-microphone candidate selection.

The bridge uses the keyboard source only as a short-lived hint/cache key.
The microphone for the current press is chosen from live PCM levels, so one
keyboard source may use several microphones and any number of keyboard or
remote-control sources can be handled without fixed ``local``/``remote``
slots.  Only aggregate level numbers are retained; audio samples are never
stored by this module.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np

from . import audio_output


EndpointKey = tuple[str, str]


class PcmPreRollBuffer:
    """A bounded, memory-only PCM ring whose discarded storage is zeroed.

    The buffer never writes audio to disk and owns a copy of every block it
    retains. ``drain`` transfers one combined block to the caller; the caller
    then owns that returned array and must zero it after playback. ``clear``
    is safe to call repeatedly from timeout, supersession and shutdown paths.
    """

    def __init__(self, maximum_seconds: float = 1.5) -> None:
        self._maximum_seconds = max(0.05, float(maximum_seconds))
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._frames = 0
        self._rate = 0
        self._channels = 0

    def add(self, samples, rate: int, channels: int) -> None:
        rate = int(rate)
        channels = int(channels)
        if rate <= 0 or channels <= 0:
            return
        array = np.asarray(samples, dtype="int16")
        if array.size == 0:
            return
        try:
            owned = array.reshape(-1, channels).copy()
        except ValueError:
            return
        maximum_frames = max(1, int(rate * self._maximum_seconds))
        with self._lock:
            if self._rate and (self._rate != rate or self._channels != channels):
                self._clear_locked()
            self._rate = rate
            self._channels = channels
            self._chunks.append(owned)
            self._frames += len(owned)
            while self._frames > maximum_frames and self._chunks:
                excess = self._frames - maximum_frames
                first = self._chunks[0]
                if excess >= len(first):
                    self._chunks.popleft()
                    self._frames -= len(first)
                    first.fill(0)
                    continue
                kept = first[excess:].copy()
                first.fill(0)
                self._chunks[0] = kept
                self._frames -= excess

    def drain(self):
        """Transfer buffered PCM to the caller, zeroing internal chunks."""

        with self._lock:
            if not self._chunks or self._rate <= 0 or self._channels <= 0:
                self._clear_locked()
                return None
            combined = np.concatenate(tuple(self._chunks), axis=0)
            rate = self._rate
            channels = self._channels
            self._clear_locked()
        return combined, rate, channels

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        while self._chunks:
            self._chunks.popleft().fill(0)
        self._frames = 0
        self._rate = 0
        self._channels = 0

    @property
    def buffered_frames(self) -> int:
        with self._lock:
            return self._frames

_NON_MICROPHONE_NAME_MARKERS = (
    "stereo mix",
    "立体声混音",
    "line input",
    "线路输入",
    "sound mapper",
    "声音映射器",
    "primary sound capture",
    "主声音捕获",
)
_MICROPHONE_NAME_MARKERS = (
    "microphone",
    "mic",
    "麦克风",
    "remote",
    "远程",
    "virtual",
    "虚拟",
)
_VIRTUAL_MICROPHONE_NAME_MARKERS = (
    "virtual",
    "虚拟",
    "remote",
    "远程",
    "todesk",
    "uu",
)


def endpoint_key(endpoint: audio_output.AudioEndpoint) -> EndpointKey:
    return str(endpoint.name), str(endpoint.host_api)


def is_recommended_candidate(endpoint: audio_output.AudioEndpoint) -> bool:
    """Exclude loop/monitor/legacy alias endpoints from automatic probing."""

    name = endpoint.name.strip()
    folded = name.casefold()
    if not name or audio_output.is_cable_output_endpoint(name):
        return False
    if any(marker in folded for marker in _NON_MICROPHONE_NAME_MARKERS):
        return False
    if folded in {"input", "input ()", "麦克风", "microphone"}:
        return False
    if "wasapi" in endpoint.host_api.casefold():
        return True
    return any(marker in folded for marker in _MICROPHONE_NAME_MARKERS)


def recommended_candidates(
    endpoints: Iterable[audio_output.AudioEndpoint],
) -> list[audio_output.AudioEndpoint]:
    """Keep one recommended Windows interface per physical/logical mic.

    PortAudio commonly exposes one microphone through WASAPI, WDM-KS,
    DirectSound and MME.  Probing all duplicates adds contention but no useful
    choice, so prefer WASAPI and keep the other interfaces as fallbacks.
    CABLE Output is excluded because feeding it back into CABLE Input creates
    a loop.
    """

    grouped: dict[str, list[audio_output.AudioEndpoint]] = {}
    for endpoint in endpoints:
        if not is_recommended_candidate(endpoint):
            continue
        grouped.setdefault(endpoint.name.casefold(), []).append(endpoint)

    def rank(endpoint: audio_output.AudioEndpoint) -> tuple[int, str]:
        host = endpoint.host_api.casefold()
        if "wasapi" in host:
            return 0, host
        if "wdm" in host or host == "ks":
            return 1, host
        if "directsound" in host:
            return 2, host
        if "mme" in host:
            return 3, host
        return 4, host

    # ``dict`` preserves the user's/configuration order. Keep that order so
    # callers can put origin-compatible microphones first without excluding
    # the remaining fallbacks.
    chosen = [min(group, key=rank) for group in grouped.values()]
    # WDM-KS endpoints often require exclusive access and can remain invalid
    # briefly after a level-only probe closes.  If Windows exposes any WASAPI
    # microphones, use that stable shared-mode set for automatic switching;
    # retain legacy interfaces only as the last resort on older systems.
    wasapi = [
        endpoint
        for endpoint in chosen
        if "wasapi" in endpoint.host_api.casefold()
    ]
    if wasapi:
        chosen = wasapi
    return chosen


def is_virtual_microphone_candidate(
    endpoint: audio_output.AudioEndpoint,
) -> bool:
    """Return whether an endpoint represents remote-control/virtual audio."""

    folded = endpoint.name.casefold()
    return any(
        marker in folded for marker in _VIRTUAL_MICROPHONE_NAME_MARKERS
    )


def candidates_for_keyboard_origin(
    endpoints: Iterable[audio_output.AudioEndpoint],
    *,
    injected: Optional[bool],
) -> list[audio_output.AudioEndpoint]:
    """Order refinement by the origin-compatible microphone class.

    A physical keyboard press should be audible immediately through a real
    microphone such as H180.  A software-injected remote-control press should
    instead begin with a virtual microphone such as UU or ToDesk.  The live
    level probe checks that preferred class first while retaining every other
    safe microphone as a fallback. Remote-control products do not consistently
    set the Windows ``LLKHF_INJECTED`` flag, so ``None`` means the source is
    genuinely unknown and every microphone participates without class bias.
    """

    candidates = recommended_candidates(endpoints)
    if injected is None:
        return candidates
    compatible = [
        endpoint
        for endpoint in candidates
        if is_virtual_microphone_candidate(endpoint) == bool(injected)
    ]
    fallback = [endpoint for endpoint in candidates if endpoint not in compatible]
    # The origin is a latency hint, not an identity proof. Remote-control
    # products may omit LLKHF_INJECTED, macro drivers may add it to a physical
    # keyboard, and either side can intentionally use an unusual microphone.
    # Probe the preferred class first but always retain every safe candidate.
    return compatible + fallback


@dataclass(frozen=True)
class ActivityLevel:
    rms: float
    peak: int
    samples: int


class ActivityAccumulator:
    """Thread-safe aggregate level meter which never retains PCM samples."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sum_squares = 0.0
        self._peak = 0
        self._samples = 0
        self._interval_sum_squares = 0.0
        self._interval_peak = 0
        self._interval_samples = 0

    def add(self, values) -> None:
        try:
            flat = values.reshape(-1)
        except AttributeError:
            flat = values
        sum_squares = 0.0
        peak = 0
        count = 0
        for raw in flat:
            value = int(raw)
            magnitude = abs(value)
            peak = max(peak, magnitude)
            sum_squares += float(value) * float(value)
            count += 1
        if not count:
            return
        with self._lock:
            self._sum_squares += sum_squares
            self._peak = max(self._peak, peak)
            self._samples += count
            self._interval_sum_squares += sum_squares
            self._interval_peak = max(self._interval_peak, peak)
            self._interval_samples += count

    def snapshot(self) -> ActivityLevel:
        with self._lock:
            samples = self._samples
            rms = math.sqrt(self._sum_squares / samples) if samples else 0.0
            return ActivityLevel(rms=rms, peak=self._peak, samples=samples)

    def take_interval(self) -> ActivityLevel:
        """Return and reset the recent window while retaining totals."""

        with self._lock:
            samples = self._interval_samples
            rms = (
                math.sqrt(self._interval_sum_squares / samples)
                if samples
                else 0.0
            )
            result = ActivityLevel(
                rms=rms,
                peak=self._interval_peak,
                samples=samples,
            )
            self._interval_sum_squares = 0.0
            self._interval_peak = 0
            self._interval_samples = 0
            return result


def choose_active_endpoint(
    levels: Mapping[EndpointKey, ActivityLevel],
    *,
    preferred: Optional[EndpointKey] = None,
    minimum_rms: float = 120.0,
    switch_ratio: float = 1.6,
) -> Optional[EndpointKey]:
    """Choose a confident live microphone, with last-used hysteresis.

    Silence never causes a switch.  If two microphones hear approximately
    the same room sound, the previous choice is kept when it is one of them;
    otherwise the result is intentionally ambiguous.  This prevents rapid
    source hopping and avoids pretending that raw volume alone is identity.
    """

    usable = {
        key: level
        for key, level in levels.items()
        if level.samples > 0 and level.rms >= float(minimum_rms)
    }
    if not usable:
        return None
    ordered = sorted(usable.items(), key=lambda item: item[1].rms, reverse=True)
    winner_key, winner_level = ordered[0]
    if preferred in usable:
        preferred_level = usable[preferred]
        if preferred_level.rms * float(switch_ratio) >= winner_level.rms:
            return preferred
    if len(ordered) > 1:
        runner_up = ordered[1][1]
        if runner_up.rms * float(switch_ratio) > winner_level.rms:
            return None
    return winner_key


def choose_sustained_alternative(
    histories: Mapping[EndpointKey, Sequence[float]],
    *,
    preferred: EndpointKey,
    minimum_rms: float,
    minimum_active_windows: int = 5,
    preferred_hold_windows: int = 4,
    variation_ratio: float = 1.15,
    dominance_ratio: float = 1.6,
) -> tuple[Optional[EndpointKey], str]:
    """Choose a credible replacement without letting raw volume steal focus.

    Values in ``histories`` are privacy-safe interval scores, not PCM.  The
    current source is retained while it has recent confirmed activity.  A
    replacement therefore becomes eligible only after the current source has
    gone quiet, and only when the replacement supplies sustained *changing*
    activity.  The variation requirement rejects virtual endpoints that wake
    up into a large but nearly constant startup/background signal.

    The returned reason is intentionally suitable for aggregate diagnostics;
    it never contains or requires recorded audio.
    """

    minimum = max(0.0, float(minimum_rms))
    required = max(2, int(minimum_active_windows))
    hold = max(1, int(preferred_hold_windows))
    variation = max(1.01, float(variation_ratio))
    dominance = max(1.0, float(dominance_ratio))

    preferred_history = list(histories.get(preferred, ()))
    preferred_recent = preferred_history[-hold:]
    preferred_active = sum(value >= minimum for value in preferred_recent)
    # A few adjacent speech windows are enough to keep the already-routed
    # microphone. Once they age out, another microphone gets a fair trial.
    if preferred_active >= min(3, hold):
        return None, "preferred_recently_active"

    eligible: list[tuple[EndpointKey, float]] = []
    saw_sustained = False
    saw_steady = False
    for key, values in histories.items():
        if key == preferred:
            continue
        history = list(values)
        active_tail: list[float] = []
        for value in reversed(history):
            if value < minimum:
                break
            active_tail.append(float(value))
        active_tail.reverse()
        if len(active_tail) < required:
            continue
        saw_sustained = True
        low = max(minimum, min(active_tail))
        high = max(active_tail)
        if high < low * variation:
            saw_steady = True
            continue
        eligible.append((key, sum(active_tail) / len(active_tail)))

    if not eligible:
        if saw_steady:
            return None, "alternative_steady_background"
        if saw_sustained:
            return None, "alternative_not_dynamic"
        return None, "alternative_not_sustained"

    eligible.sort(key=lambda item: item[1], reverse=True)
    winner, winner_score = eligible[0]
    if len(eligible) > 1 and eligible[1][1] * dominance > winner_score:
        return None, "alternative_ambiguous"
    return winner, "alternative_sustained_after_preferred_quiet"


def normalize_configured_candidates(value) -> list[audio_output.AudioEndpoint]:
    """Read the privacy-safe ``[{name, host_api}, ...]`` config shape."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    candidates: list[audio_output.AudioEndpoint] = []
    seen: set[EndpointKey] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        host_api = str(item.get("host_api", "")).strip()
        key = (name, host_api)
        if not name or key in seen or audio_output.is_cable_output_endpoint(name):
            continue
        seen.add(key)
        candidates.append(audio_output.AudioEndpoint(name, host_api))
    return candidates


def serialize_candidates(
    endpoints: Iterable[audio_output.AudioEndpoint],
) -> list[dict[str, str]]:
    return [
        {"name": endpoint.name, "host_api": endpoint.host_api}
        for endpoint in endpoints
        if endpoint.name and not audio_output.is_cable_output_endpoint(endpoint.name)
    ]
