"""Detect whether VB-CABLE's recording endpoint has an active consumer.

This module only inspects Windows Core Audio session state.  It never opens
an audio stream and therefore does not itself trigger the Windows microphone
privacy indicator.  Imports are lazy so source-only and non-Windows tests can
still import the rest of the package without pycaw/comtypes installed.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from . import audio_output


class CaptureActivityUnavailableError(RuntimeError):
    """Core Audio activity could not be determined safely."""


class CableCaptureActivityDetector:
    """Poll active capture sessions on the canonical CABLE Output endpoint."""

    _ACTIVE_SESSION_STATE = 1

    def __init__(self) -> None:
        self._comtypes: Optional[Any] = None
        self._audio_utilities: Optional[Any] = None
        self._data_flow: Optional[Any] = None
        self._device_state: Optional[Any] = None
        self._device = None
        self._com_initialized = False

    def _load_backend(self) -> None:
        if self._audio_utilities is not None:
            return
        if sys.platform != "win32":
            raise CaptureActivityUnavailableError(
                "Windows Core Audio session detection is only available on Windows"
            )
        try:
            import comtypes  # type: ignore
            from pycaw.constants import DEVICE_STATE, EDataFlow  # type: ignore
            from pycaw.utils import AudioUtilities  # type: ignore
        except ImportError as exc:
            raise CaptureActivityUnavailableError(
                "pycaw/comtypes is not installed"
            ) from exc
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError as exc:
            # Importing comtypes can initialize the current thread as STA
            # before this explicit request. Polling session state does not
            # require MTA notifications, so retain that apartment and take a
            # balanced reference instead of treating RPC_E_CHANGED_MODE as a
            # detector failure.
            if getattr(exc, "winerror", None) != -2147417850:  # 0x80010106
                raise CaptureActivityUnavailableError(
                    "could not initialize Windows Core Audio COM access"
                ) from exc
            comtypes.CoInitialize()
        except Exception as exc:  # noqa: BLE001 - COM reports platform HRESULTs
            raise CaptureActivityUnavailableError(
                "could not initialize Windows Core Audio COM access"
            ) from exc
        self._com_initialized = True
        self._comtypes = comtypes
        self._audio_utilities = AudioUtilities
        self._data_flow = EDataFlow
        self._device_state = DEVICE_STATE

    def _resolve_device(self):
        self._load_backend()
        assert self._audio_utilities is not None
        assert self._data_flow is not None
        assert self._device_state is not None
        try:
            devices = self._audio_utilities.GetAllDevices(
                self._data_flow.eCapture.value,
                self._device_state.ACTIVE.value,
            )
            matches = [
                device
                for device in devices
                if audio_output.is_cable_output_endpoint(
                    str(device.FriendlyName or "")
                )
            ]
        except Exception as exc:  # noqa: BLE001 - normalize COM failures
            raise CaptureActivityUnavailableError(
                "could not enumerate Windows recording endpoints"
            ) from exc
        if len(matches) != 1:
            raise CaptureActivityUnavailableError(
                "CABLE Output recording endpoint is missing or ambiguous"
            )
        self._device = matches[0]
        return self._device

    def is_active(self) -> bool:
        """Return True while any client actively reads CABLE Output."""

        device = self._device or self._resolve_device()
        try:
            sessions = device.AudioSessionManager.GetSessionEnumerator()
            for index in range(sessions.GetCount()):
                if int(sessions.GetSession(index).GetState()) == self._ACTIVE_SESSION_STATE:
                    return True
            # Microsoft documents that a long-lived session enumerator/manager
            # can miss newly created sessions. Re-resolve after every inactive
            # poll so the next check asks the audio engine through a fresh
            # endpoint/session-manager object.
            self._device = None
            return False
        except Exception as exc:  # noqa: BLE001 - endpoint may have hot-unplugged
            # Force the next poll to re-enumerate after hot-plug/default-device
            # changes.  The router preserves audio by falling back to continuous
            # capture while this transient state cannot be determined.
            self._device = None
            raise CaptureActivityUnavailableError(
                "could not read CABLE Output audio-session state"
            ) from exc

    def close(self) -> None:
        self._device = None
        comtypes = self._comtypes
        self._audio_utilities = None
        self._data_flow = None
        self._device_state = None
        self._comtypes = None
        if self._com_initialized and comtypes is not None:
            self._com_initialized = False
            try:
                comtypes.CoUninitialize()
            except Exception:
                # Process/thread teardown must not be turned into an audio
                # routing failure merely because COM cleanup reported late.
                pass
