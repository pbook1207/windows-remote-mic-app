"""Per-session Win32 control channel for the invisible RC003 bridge.

The bridge owns one named manual-reset event for its complete lifetime.
The settings process can therefore (a) tell whether a controllable bridge
is running and (b) request a graceful stop without finding or terminating a
process by PID.  The event is closed only after ``RC003App.stop()`` has
finished, so disappearance is also the acknowledgement that BLE, HID,
audio, and input-hook cleanup has completed.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable, NamedTuple, Optional

_STOP_EVENT_NAME = r"Local\RemoteMicRC003_BridgeStopRequest"

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF


class BridgeControlUnavailableError(RuntimeError):
    """The named-event API could not be used safely."""


class EventCreationResult(NamedTuple):
    handle: int
    last_error: int


class EventOpenResult(NamedTuple):
    handle: int
    last_error: int


CreateEventFn = Callable[[str], EventCreationResult]
OpenEventFn = Callable[[int, str], EventOpenResult]
SetEventFn = Callable[[int], bool]
WaitEventFn = Callable[[int, int], int]
CloseHandleFn = Callable[[int], bool]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise BridgeControlUnavailableError(
            "the bridge control event is only available on Windows"
        )


def _real_create_event(name: str) -> EventCreationResult:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    raw_handle = kernel32.CreateEventW(None, True, False, name)
    last_error = ctypes.get_last_error()
    return EventCreationResult(int(raw_handle) if raw_handle else 0, last_error)


def _real_open_event(access: int, name: str) -> EventOpenResult:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.OpenEventW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    raw_handle = kernel32.OpenEventW(access, False, name)
    last_error = ctypes.get_last_error()
    return EventOpenResult(int(raw_handle) if raw_handle else 0, last_error)


def _real_set_event(handle: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    return bool(kernel32.SetEvent(handle))


def _real_wait_event(handle: int, timeout_ms: int) -> int:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    return int(kernel32.WaitForSingleObject(handle, timeout_ms))


def _real_close_handle(handle: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return bool(kernel32.CloseHandle(handle))


class BridgeStopEventOwner:
    """Owns the stop-request event for exactly one bridge lifetime."""

    def __init__(
        self,
        *,
        name: str = _STOP_EVENT_NAME,
        _create_event: CreateEventFn = _real_create_event,
        _wait_event: WaitEventFn = _real_wait_event,
        _close_handle: CloseHandleFn = _real_close_handle,
    ) -> None:
        self._name = name
        self._create_event = _create_event
        self._wait_event = _wait_event
        self._close_handle = _close_handle
        self._handle: Optional[int] = None

    def __enter__(self) -> "BridgeStopEventOwner":
        result = self._create_event(self._name)
        if not result.handle:
            raise BridgeControlUnavailableError(
                f"CreateEventW failed (GetLastError={result.last_error})"
            )
        if result.last_error == _ERROR_ALREADY_EXISTS:
            self._close_handle(result.handle)
            raise BridgeControlUnavailableError(
                "the RC003 bridge control event already exists"
            )
        self._handle = result.handle
        return self

    def stop_requested(self) -> bool:
        handle = self._handle
        if not handle:
            raise BridgeControlUnavailableError("bridge control event is not open")
        result = self._wait_event(handle, 0)
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        if result == _WAIT_FAILED:
            raise BridgeControlUnavailableError("WaitForSingleObject failed")
        raise BridgeControlUnavailableError(
            f"WaitForSingleObject returned unexpected result {result}"
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if handle and not self._close_handle(handle):
            raise BridgeControlUnavailableError("CloseHandle failed for bridge control event")
        return None


def is_bridge_running(
    *,
    name: str = _STOP_EVENT_NAME,
    _open_event: OpenEventFn = _real_open_event,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    """Returns whether a controllable bridge currently owns the event."""

    result = _open_event(_SYNCHRONIZE, name)
    if not result.handle:
        if result.last_error == _ERROR_FILE_NOT_FOUND:
            return False
        raise BridgeControlUnavailableError(
            f"OpenEventW failed (GetLastError={result.last_error})"
        )
    if not _close_handle(result.handle):
        raise BridgeControlUnavailableError("CloseHandle failed after bridge status query")
    return True


def request_bridge_stop(
    *,
    name: str = _STOP_EVENT_NAME,
    _open_event: OpenEventFn = _real_open_event,
    _set_event: SetEventFn = _real_set_event,
    _close_handle: CloseHandleFn = _real_close_handle,
) -> bool:
    """Signals a running bridge. Returns False if no bridge is running."""

    result = _open_event(_EVENT_MODIFY_STATE, name)
    if not result.handle:
        if result.last_error == _ERROR_FILE_NOT_FOUND:
            return False
        raise BridgeControlUnavailableError(
            f"OpenEventW failed (GetLastError={result.last_error})"
        )
    try:
        if not _set_event(result.handle):
            raise BridgeControlUnavailableError("SetEvent failed")
        return True
    finally:
        if not _close_handle(result.handle):
            raise BridgeControlUnavailableError(
                "CloseHandle failed after bridge stop request"
            )
