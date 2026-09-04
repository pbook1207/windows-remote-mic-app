"""Adaptive, per-endpoint gain for transparently forwarded microphones.

Only aggregate level measurements and the resulting gain value are retained;
audio samples are never persisted.  Each Windows microphone learns its own
gain so replacing one device does not apply the previous device's tuning.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Iterable

import numpy as np


PROFILE_FILENAME = "system_microphone_gain_profiles.json"
SCHEMA_VERSION = 1
DEFAULT_TARGET_RMS = 2800.0
DEFAULT_MINIMUM_SIGNAL_RMS = 24.0
DEFAULT_MAX_GAIN_DB = 32.0
DEFAULT_MIN_GAIN_DB = -6.0
DEFAULT_LIMIT_PEAK = 30000.0
MAX_PROFILES = 64

EndpointKey = tuple[str, str]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear_from_db(value: float) -> float:
    return math.pow(10.0, float(value) / 20.0)


class AdaptiveMicrophoneGain:
    """Apply fast automatic gain and remember one safe value per endpoint."""

    def __init__(
        self,
        config_root: Path,
        *,
        enabled: bool = True,
        target_rms: float = DEFAULT_TARGET_RMS,
        minimum_signal_rms: float = DEFAULT_MINIMUM_SIGNAL_RMS,
        maximum_gain_db: float = DEFAULT_MAX_GAIN_DB,
        minimum_gain_db: float = DEFAULT_MIN_GAIN_DB,
        limit_peak: float = DEFAULT_LIMIT_PEAK,
    ) -> None:
        self._path = Path(config_root) / PROFILE_FILENAME
        self._enabled = bool(enabled)
        self._target_rms = max(1.0, float(target_rms))
        self._minimum_signal_rms = max(0.0, float(minimum_signal_rms))
        self._maximum_gain_db = max(0.0, float(maximum_gain_db))
        self._minimum_gain_db = min(0.0, float(minimum_gain_db))
        self._limit_peak = _clamp(float(limit_peak), 1000.0, 32767.0)
        self._lock = threading.Lock()
        self._profiles = self._load_profiles()
        self._dirty = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def gain_db_for(self, endpoint: EndpointKey) -> float:
        with self._lock:
            return float(self._profiles.get(tuple(endpoint), 0.0))

    def process(self, endpoint: EndpointKey, samples):
        """Return amplified int16 PCM without mutating the callback buffer."""

        array = np.asarray(samples)
        if not self._enabled or array.size == 0:
            return array.copy()
        values = array.astype(np.float64, copy=False)
        rms = float(np.sqrt(np.mean(values * values)))
        peak = float(np.max(np.abs(values)))
        key = (str(endpoint[0]), str(endpoint[1]))

        with self._lock:
            current_db = float(self._profiles.get(key, 0.0))
            # Do not learn from digital silence. Once a profile is learned it
            # remains active during quiet gaps, avoiding audible gain pumping.
            if rms >= self._minimum_signal_rms and peak >= 96.0:
                wanted_db = 20.0 * math.log10(self._target_rms / rms)
                peak_headroom_db = 20.0 * math.log10(self._limit_peak / peak)
                wanted_db = _clamp(
                    min(wanted_db, peak_headroom_db),
                    self._minimum_gain_db,
                    self._maximum_gain_db,
                )
                # Raise weak microphones quickly; reduce gain even faster so
                # a newly loud source cannot clip while the profile catches up.
                smoothing = 0.80 if wanted_db < current_db else 0.30
                updated_db = current_db + smoothing * (wanted_db - current_db)
                updated_db = _clamp(
                    updated_db, self._minimum_gain_db, self._maximum_gain_db
                )
                if abs(updated_db - current_db) >= 0.05:
                    self._profiles[key] = updated_db
                    self._dirty = True
                    current_db = updated_db

        gain = _linear_from_db(current_db)
        if peak > 0.0:
            gain = min(gain, self._limit_peak / peak)
        amplified = np.clip(values * gain, -32768.0, 32767.0)
        return amplified.astype(np.int16)

    def _load_profiles(self) -> dict[EndpointKey, float]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            return {}
        raw_profiles = document.get("profiles")
        if not isinstance(raw_profiles, list):
            return {}
        profiles: dict[EndpointKey, float] = {}
        for item in raw_profiles[:MAX_PROFILES]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            host_api = str(item.get("host_api", "")).strip()
            try:
                gain_db = float(item.get("gain_db", 0.0))
            except (TypeError, ValueError):
                continue
            if not name or not math.isfinite(gain_db):
                continue
            profiles[(name, host_api)] = _clamp(
                gain_db, self._minimum_gain_db, self._maximum_gain_db
            )
        return profiles

    def _profile_document(self, profiles: Iterable[tuple[EndpointKey, float]]) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "profiles": [
                {
                    "name": key[0],
                    "host_api": key[1],
                    "gain_db": round(float(gain_db), 2),
                }
                for key, gain_db in profiles
            ],
        }

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            profiles = sorted(self._profiles.items())[:MAX_PROFILES]
            document = self._profile_document(profiles)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_name = handle.name
            os.replace(temporary_name, self._path)
            with self._lock:
                self._dirty = False
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def close(self) -> None:
        self.save()
