"""Session-local bridge-to-settings relay for physical RC003 button edges.

The running bridge owns the Frida HID report tap.  A separate settings
process therefore cannot open a second tap (or rely on Windows Raw Input for
the usages Windows drops).  This module exposes the already-decoded physical
edges through a small Windows named shared-memory ring buffer.

The relay is deliberately observation-only: it carries a fixed RC003 button
identifier and its up/down edge, never a configured action or arbitrary key
payload.  Failure is best-effort and must never affect the bridge's normal
button, voice, or audio paths.
"""

from __future__ import annotations

import mmap
import struct
import sys
import threading
from typing import Callable, Optional

from . import remote_layout


class ButtonDetectionRelayUnavailableError(RuntimeError):
    """Raised when the session-local relay cannot be opened or stopped."""


_TAG_NAME = r"Local\RemoteMicRC003_ButtonDetection_v1"
_MAGIC = b"RMCBTN01"
_HEADER = struct.Struct("<8sQ")
_SLOT = struct.Struct("<QBB6x")
_SLOT_COUNT = 64
_MAPPING_SIZE = _HEADER.size + (_SLOT.size * _SLOT_COUNT)
_POLL_INTERVAL_SECONDS = 0.01
_STOP_JOIN_TIMEOUT_SECONDS = 2.0

_BUTTON_TO_CODE = {
    button_id: index + 1 for index, button_id in enumerate(remote_layout.BUTTON_ORDER)
}
_CODE_TO_BUTTON = {code: button_id for button_id, code in _BUTTON_TO_CODE.items()}


def _open_named_mapping():
    if sys.platform != "win32":
        raise ButtonDetectionRelayUnavailableError(
            "RC003 按键检测共享通道仅适用于 Windows"
        )
    try:
        return mmap.mmap(
            -1,
            _MAPPING_SIZE,
            tagname=_TAG_NAME,
            access=mmap.ACCESS_WRITE,
        )
    except (OSError, ValueError) as exc:
        raise ButtonDetectionRelayUnavailableError(
            f"无法打开 RC003 按键检测共享通道：{exc}"
        ) from exc


def _read_at(mapping, offset: int, length: int) -> bytes:
    mapping.seek(offset)
    return mapping.read(length)


def _write_at(mapping, offset: int, data: bytes) -> None:
    mapping.seek(offset)
    mapping.write(data)


def _read_sequence(mapping) -> Optional[int]:
    raw = _read_at(mapping, 0, _HEADER.size)
    if len(raw) != _HEADER.size:
        return None
    magic, sequence = _HEADER.unpack(raw)
    return sequence if magic == _MAGIC else None


def _initialize_mapping(mapping) -> None:
    _write_at(mapping, 0, _HEADER.pack(_MAGIC, 0))


class ButtonDetectionPublisher:
    """Best-effort single-writer used by the running bridge process."""

    def __init__(self, *, _open_mapping: Callable[[], object] = _open_named_mapping) -> None:
        self._open_mapping = _open_mapping
        self._mapping = None
        self._lock = threading.Lock()

    def publish(self, button_id: str, is_pressed: bool) -> bool:
        code = _BUTTON_TO_CODE.get(button_id)
        if code is None:
            return False
        with self._lock:
            try:
                mapping = self._mapping
                if mapping is None:
                    mapping = self._open_mapping()
                    self._mapping = mapping
                sequence = _read_sequence(mapping)
                if sequence is None:
                    _initialize_mapping(mapping)
                    sequence = 0
                sequence += 1
                slot_offset = _HEADER.size + (
                    ((sequence - 1) % _SLOT_COUNT) * _SLOT.size
                )
                # Commit the slot before advancing the header sequence.  A
                # reader never observes a new sequence pointing at old data.
                _write_at(
                    mapping,
                    slot_offset,
                    _SLOT.pack(sequence, code, int(bool(is_pressed))),
                )
                _write_at(mapping, 0, _HEADER.pack(_MAGIC, sequence))
                return True
            except (ButtonDetectionRelayUnavailableError, OSError, ValueError):
                self._close_unlocked()
                return False

    def _close_unlocked(self) -> None:
        mapping = self._mapping
        self._mapping = None
        if mapping is not None:
            try:
                mapping.close()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()


class ButtonDetectionListener:
    """Read physical edges on a daemon thread and invoke one callback."""

    def __init__(
        self,
        callback: Callable[[str, bool], None],
        *,
        _open_mapping: Callable[[], object] = _open_named_mapping,
        poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self._callback = callback
        self._open_mapping = _open_mapping
        self._poll_interval_seconds = poll_interval_seconds
        self._mapping = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_sequence = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        mapping = self._open_mapping()
        try:
            sequence = _read_sequence(mapping)
            if sequence is None:
                _initialize_mapping(mapping)
                sequence = 0
        except Exception:
            mapping.close()
            raise
        self._mapping = mapping
        # Ignore stale edges written before the user clicked Detect.
        self._last_sequence = sequence
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="RC003ButtonDetectionRelay",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval_seconds):
            mapping = self._mapping
            if mapping is None:
                return
            try:
                current = _read_sequence(mapping)
                if current is None or current <= self._last_sequence:
                    continue
                first = max(self._last_sequence + 1, current - _SLOT_COUNT + 1)
                for sequence in range(first, current + 1):
                    slot_offset = _HEADER.size + (
                        ((sequence - 1) % _SLOT_COUNT) * _SLOT.size
                    )
                    raw = _read_at(mapping, slot_offset, _SLOT.size)
                    if len(raw) != _SLOT.size:
                        continue
                    stored_sequence, code, pressed = _SLOT.unpack(raw)
                    if stored_sequence != sequence or pressed not in (0, 1):
                        continue
                    button_id = _CODE_TO_BUTTON.get(code)
                    if button_id is not None:
                        self._callback(button_id, bool(pressed))
                self._last_sequence = current
            except (OSError, ValueError):
                return

    def stop(self) -> None:
        thread = self._thread
        self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise ButtonDetectionRelayUnavailableError(
                    "RC003 按键检测共享通道未能及时停止"
                )
        self._thread = None
        mapping = self._mapping
        self._mapping = None
        if mapping is not None:
            try:
                mapping.close()
            except (OSError, ValueError) as exc:
                raise ButtonDetectionRelayUnavailableError(
                    f"关闭 RC003 按键检测共享通道失败：{exc}"
                ) from exc
