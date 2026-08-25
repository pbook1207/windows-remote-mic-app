"""Modern, non-activating text menu used by the RC003 remote.

The bridge owns the menu state, while a small Qt Quick child process owns the
actual window. Keeping the GUI in a child process lets the bridge retain its
asyncio loop and gives the menu clear, anti-aliased text and a styled surface.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import key_mapping, uia_caret_windows


_MAX_VISIBLE_ITEMS = 8
_OVERLAY_FLAG = "--text-menu-overlay"


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    )


def _overlay_command() -> list[str]:
    """Return the hidden helper command for source and frozen builds."""

    if getattr(sys, "frozen", False):
        return [sys.executable, _OVERLAY_FLAG]
    return [sys.executable, "-m", "ovb_rc003", _OVERLAY_FLAG]


def _qml_file() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_rc003_qml" / "TextMenuOverlay.qml"
    return Path(__file__).resolve().parent / "qml" / "TextMenuOverlay.qml"


def _parse_overlay_message_line(raw_line: object) -> Optional[dict]:
    """Decode one parent-to-overlay message with an explicit UTF-8 contract.

    A frozen, windowed executable can inherit the Windows ANSI code page for
    ``sys.stdin``. The parent always writes UTF-8, so allowing TextIOWrapper
    to decode the pipe using that locale corrupts Chinese before JSON sees it.
    Reading ``sys.stdin.buffer`` and decoding here keeps source and packaged
    builds identical.
    """

    if isinstance(raw_line, bytes):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(raw_line, str):
        line = raw_line
    else:
        return None
    try:
        message = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def _panel_position_above_cursor(
    cursor_x: int,
    cursor_y: int,
    area_x: int,
    area_y: int,
    area_width: int,
    area_height: int,
    panel_width: int,
    panel_height: int,
) -> tuple[int, int]:
    """Place the panel above and left-aligned with the text caret, then clamp."""

    margin = 8
    left = area_x + margin
    top = area_y + margin
    right = area_x + area_width - margin
    bottom = area_y + area_height - margin
    x = cursor_x
    y = cursor_y - panel_height - margin
    return (
        min(max(x, left), max(left, right - panel_width)),
        min(max(y, top), max(top, bottom - panel_height)),
    )


def _write_overlay_event(message: dict) -> bool:
    """Write one child-to-parent event, explicitly encoded as UTF-8."""

    payload = (json.dumps(message, ensure_ascii=True) + "\n").encode("utf-8")
    text_stdout = sys.stdout
    binary_stdout = getattr(text_stdout, "buffer", None)
    try:
        if binary_stdout is not None:
            binary_stdout.write(payload)
            binary_stdout.flush()
        elif text_stdout is not None:
            text_stdout.write(payload.decode("ascii"))
            text_stdout.flush()
        else:
            return False
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True


class _TextMenuProcessBackend:
    """Send menu snapshots to the non-focusable Qt Quick helper."""

    _MOUSE_BUTTONS = (0x01, 0x02, 0x04)  # left, right, middle

    def __init__(self, on_dismiss=None, on_select=None) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._process_lock = threading.Lock()
        self._dismiss_callback = on_dismiss
        self._select_callback = on_select
        self._event_threads: list[threading.Thread] = []
        self._mouse_state_lock = threading.Lock()
        self._menu_visible = False
        self._mouse_was_down = False
        self._mouse_monitor_stop = threading.Event()
        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._user32.GetForegroundWindow.argtypes = ()
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        self._user32.WindowFromPoint.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        )
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._user32.PhysicalToLogicalPointForPerMonitorDPI.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        self._user32.PhysicalToLogicalPointForPerMonitorDPI.restype = wintypes.BOOL
        self._mouse_monitor = threading.Thread(
            target=self._monitor_mouse_clicks,
            name="RC003TextMenuMouseDismiss",
            daemon=True,
        )
        self._mouse_monitor.start()

    def set_dismiss_callback(self, callback) -> None:
        self._dismiss_callback = callback

    def set_select_callback(self, callback) -> None:
        self._select_callback = callback

    def foreground_window(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def show(
        self,
        items: Sequence[key_mapping.TextMenuItem],
        selected_index: int,
    ) -> None:
        caret = self.text_caret_position()
        self._send(
            {
                "visible": True,
                "items": [
                    {"label": item.label, "text": item.text} for item in items
                ],
                "selectedIndex": selected_index,
                "caretX": caret[0] if caret is not None else None,
                "caretY": caret[1] if caret is not None else None,
            }
        )
        with self._mouse_state_lock:
            if not self._menu_visible:
                self._mouse_was_down = self._any_mouse_button_down()
            self._menu_visible = True

    def hide(self) -> None:
        with self._mouse_state_lock:
            self._menu_visible = False
            self._mouse_was_down = False
        self._send({"visible": False}, launch=False)

    def restore_foreground(self, hwnd: int) -> None:
        if hwnd and self.foreground_window() != hwnd:
            self._user32.SetForegroundWindow(hwnd)

    def text_caret_position(self) -> Optional[tuple[int, int]]:
        """Return the foreground editor's insertion-caret screen position.

        GetGUIThreadInfo reads the caret owned by the foreground GUI thread,
        so opening the menu never depends on the mouse pointer.  The result
        is captured before the non-activating overlay is shown.
        """

        hwnd = self.foreground_window()
        if not hwnd:
            return None
        modern_caret = uia_caret_windows.text_caret_physical_position()
        if modern_caret is not None:
            point = wintypes.POINT(*modern_caret)
            try:
                converted = self._user32.PhysicalToLogicalPointForPerMonitorDPI(
                    hwnd, ctypes.byref(point)
                )
            except (AttributeError, OSError):
                converted = False
            if converted:
                return int(point.x), int(point.y)
            return modern_caret
        thread_id = self._user32.GetWindowThreadProcessId(hwnd, None)
        if not thread_id:
            return None
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        caret_hwnd = info.hwndCaret
        if not caret_hwnd:
            return None
        if info.rcCaret.bottom <= info.rcCaret.top:
            # Windowless editors can expose a zero-sized placeholder caret at
            # the host window origin. Treat it as unavailable instead of
            # placing the menu at that unrelated top-left coordinate.
            return None
        point = wintypes.POINT(info.rcCaret.left, info.rcCaret.top)
        if not self._user32.ClientToScreen(caret_hwnd, ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def shutdown(self) -> None:
        with self._mouse_state_lock:
            self._menu_visible = False
        self._mouse_monitor_stop.set()
        with self._process_lock:
            process = self._process
            if process is not None:
                self._write_locked(process, {"quit": True})
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1.0)
            self._process = None
        if self._mouse_monitor.is_alive():
            self._mouse_monitor.join(timeout=0.3)
        for thread in self._event_threads:
            if thread.is_alive():
                thread.join(timeout=0.2)

    def _any_mouse_button_down(self) -> bool:
        return any(
            # High bit: currently down. Low bit: pressed at least once since
            # the previous query, which catches a fast click completed
            # between two 30 ms polling ticks.
            bool(self._user32.GetAsyncKeyState(button) & 0x8001)
            for button in self._MOUSE_BUTTONS
        )

    def _monitor_mouse_clicks(self) -> None:
        while not self._mouse_monitor_stop.wait(0.03):
            with self._mouse_state_lock:
                visible = self._menu_visible
                was_down = self._mouse_was_down
            if not visible:
                continue
            is_down = self._any_mouse_button_down()
            if is_down and not was_down:
                if self._click_hits_overlay_process():
                    with self._mouse_state_lock:
                        if self._menu_visible:
                            self._mouse_was_down = is_down
                    continue
                self.hide()
                callback = self._dismiss_callback
                if callback is not None:
                    callback()
                continue
            with self._mouse_state_lock:
                if self._menu_visible:
                    self._mouse_was_down = is_down

    def _click_hits_overlay_process(self) -> bool:
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            return False
        hwnd = self._user32.WindowFromPoint(point)
        if not hwnd:
            return False
        clicked_pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(clicked_pid))
        with self._process_lock:
            process = self._process
            process_pid = process.pid if process is not None else 0
        return bool(process_pid and int(clicked_pid.value) == process_pid)

    def _launch_locked(self) -> subprocess.Popen:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            _overlay_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        self._process = process
        event_thread = threading.Thread(
            target=self._read_child_events,
            args=(process,),
            name="RC003TextMenuSelections",
            daemon=True,
        )
        self._event_threads.append(event_thread)
        event_thread.start()
        return process

    def _read_child_events(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                message = _parse_overlay_message_line(line)
                if message is None or "selectedIndex" not in message:
                    continue
                try:
                    selected_index = int(message["selectedIndex"])
                except (TypeError, ValueError):
                    continue
                callback = self._select_callback
                if callback is not None:
                    callback(selected_index)
        except (OSError, ValueError):
            return

    def _send(self, message: dict, *, launch: bool = True) -> None:
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is not None:
                self._process = None
                process = None
            if process is None:
                if not launch:
                    return
                process = self._launch_locked()
            if self._write_locked(process, message):
                return
            self._process = None
            if launch:
                retry = self._launch_locked()
                self._write_locked(retry, message)

    @staticmethod
    def _write_locked(process: subprocess.Popen, message: dict) -> bool:
        stream = process.stdin
        if stream is None:
            return False
        try:
            stream.write(json.dumps(message, ensure_ascii=False) + "\n")
            stream.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False


class _NullMenuBackend:
    def foreground_window(self) -> int:
        return 0

    def show(
        self,
        items: Sequence[key_mapping.TextMenuItem],
        selected_index: int,
    ) -> None:
        del items, selected_index

    def hide(self) -> None:
        pass

    def restore_foreground(self, hwnd: int) -> None:
        del hwnd

    def shutdown(self) -> None:
        pass


class TextMenuOverlay:
    """Thread-safe text-menu state plus an optional display backend."""

    def __init__(self, backend=None, *, on_select=None) -> None:
        self._backend = backend
        self._on_select = on_select
        self._lock = threading.RLock()
        self._items: Tuple[key_mapping.TextMenuItem, ...] = ()
        self._selected = 0
        self._is_open = False
        self._target_hwnd = 0
        self._visible_page_start = 0
        self._bind_backend_dismiss_callback()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._is_open

    @property
    def selected_index(self) -> int:
        with self._lock:
            return self._selected

    def toggle(self, items: Sequence[key_mapping.TextMenuItem]) -> bool:
        with self._lock:
            if self._is_open:
                self._close_locked()
                return False
            enabled = tuple(item for item in items if item.enabled)
            if not enabled:
                return False
            backend = self._ensure_backend()
            self._items = enabled
            self._selected = 0
            self._target_hwnd = backend.foreground_window()
            self._is_open = True
            self._render_locked()
            return True

    def move(self, delta: int) -> None:
        with self._lock:
            if not self._is_open or not self._items:
                return
            self._selected = (self._selected + delta) % len(self._items)
            self._render_locked()

    def confirm(self) -> Optional[str]:
        with self._lock:
            if not self._is_open or not self._items:
                return None
            text = self._items[self._selected].text
            target = self._target_hwnd
            self._close_locked()
            self._backend.restore_foreground(target)
            return text

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def shutdown(self) -> None:
        with self._lock:
            self._close_locked()
        if self._backend is not None:
            self._backend.shutdown()

    def _close_locked(self) -> None:
        if self._backend is not None:
            self._backend.hide()
        self._is_open = False
        self._items = ()
        self._selected = 0
        self._target_hwnd = 0
        self._visible_page_start = 0

    def _dismiss_from_backend(self) -> None:
        """Synchronize a mouse-dismissed helper with the bridge state."""

        with self._lock:
            self._is_open = False
            self._items = ()
            self._selected = 0
            self._target_hwnd = 0
            self._visible_page_start = 0

    def _select_from_backend(self, visible_index: int) -> None:
        """Confirm a QML item click through the same text-selection state."""

        with self._lock:
            absolute_index = self._visible_page_start + visible_index
            if not self._is_open or not (0 <= absolute_index < len(self._items)):
                return
            text = self._items[absolute_index].text
            target = self._target_hwnd
            self._close_locked()
            self._backend.restore_foreground(target)
        if self._on_select is not None:
            self._on_select(text)

    def _bind_backend_dismiss_callback(self) -> None:
        setter = getattr(self._backend, "set_dismiss_callback", None)
        if setter is not None:
            setter(self._dismiss_from_backend)
        select_setter = getattr(self._backend, "set_select_callback", None)
        if select_setter is not None:
            select_setter(self._select_from_backend)

    def _render_locked(self) -> None:
        total = len(self._items)
        page_start = max(
            0,
            min(
                self._selected - _MAX_VISIBLE_ITEMS // 2,
                total - _MAX_VISIBLE_ITEMS,
            ),
        )
        visible = self._items[page_start : page_start + _MAX_VISIBLE_ITEMS]
        self._visible_page_start = page_start
        backend = self._ensure_backend()
        backend.show(visible, self._selected - page_start)

    def _ensure_backend(self):
        if self._backend is None:
            self._backend = (
                _TextMenuProcessBackend(
                    self._dismiss_from_backend,
                    self._select_from_backend,
                )
                if sys.platform == "win32"
                else _NullMenuBackend()
            )
        return self._backend


def run_qt_text_menu_overlay() -> int:
    """Run the hidden stdin-controlled QML overlay child process."""

    try:
        from PySide6.QtCore import QObject, QPoint, Property, QUrl, Signal, Slot
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
    except ImportError:
        return 2

    class MenuController(QObject):
        messageReceived = Signal(object)
        visibleChanged = Signal()
        itemsChanged = Signal()
        selectedIndexChanged = Signal()
        positionChanged = Signal()

        def __init__(self) -> None:
            super().__init__()
            self._visible = False
            self._items: list[dict[str, str]] = []
            self._selected_index = 0
            self._panel_x = 0
            self._panel_y = 0
            self.messageReceived.connect(self._apply_message)

        @Property(bool, notify=visibleChanged)
        def visible(self) -> bool:
            return self._visible

        @Property("QVariantList", notify=itemsChanged)
        def items(self) -> list[dict[str, str]]:
            return self._items

        @Property(int, notify=selectedIndexChanged)
        def selectedIndex(self) -> int:
            return self._selected_index

        @Property(int, notify=positionChanged)
        def panelX(self) -> int:
            return self._panel_x

        @Property(int, notify=positionChanged)
        def panelY(self) -> int:
            return self._panel_y

        @Slot(object)
        def _apply_message(self, message: object) -> None:
            if not isinstance(message, dict):
                return
            if message.get("quit"):
                QGuiApplication.quit()
                return
            if message.get("visible") is False:
                if self._visible:
                    self._visible = False
                    self.visibleChanged.emit()
                return
            raw_items = message.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                return
            items = []
            for raw_item in raw_items[:_MAX_VISIBLE_ITEMS]:
                if not isinstance(raw_item, dict):
                    continue
                items.append(
                    {
                        "label": str(raw_item.get("label", "")),
                        "text": str(raw_item.get("text", "")),
                    }
                )
            if not items:
                return
            try:
                selected = int(message.get("selectedIndex", 0))
            except (TypeError, ValueError):
                selected = 0
            selected = max(0, min(selected, len(items) - 1))
            self._items = items
            self._selected_index = selected
            self.itemsChanged.emit()
            self.selectedIndexChanged.emit()
            if not self._visible:
                self._reposition(message, len(items))
                self._visible = True
                self.visibleChanged.emit()

        @Slot(int)
        def selectItem(self, index: int) -> None:
            if not self._visible or not (0 <= index < len(self._items)):
                return
            self._visible = False
            self.visibleChanged.emit()
            _write_overlay_event({"selectedIndex": index})

        def _reposition(self, message: dict, item_count: int) -> None:
            try:
                caret_x = int(message["caretX"])
                caret_y = int(message["caretY"])
                has_caret = True
            except (KeyError, TypeError, ValueError):
                caret_x = 0
                caret_y = 0
                has_caret = False

            screen = (
                QGuiApplication.screenAt(QPoint(caret_x, caret_y))
                if has_caret
                else QGuiApplication.primaryScreen()
            )
            if screen is None:
                screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            area = screen.availableGeometry()
            width = 96
            height = min(236, 12 + item_count * 28)
            if not has_caret:
                # Some secure/non-editable controls expose no insertion
                # caret. Keep this fallback deterministic and independent of
                # the mouse; ordinary editable controls use the caret branch.
                caret_x = area.x() + area.width() // 2
                caret_y = area.y() + area.height() // 2
            self._panel_x, self._panel_y = _panel_position_above_cursor(
                caret_x,
                caret_y,
                area.x(),
                area.y(),
                area.width(),
                area.height(),
                width,
                height,
            )
            self.positionChanged.emit()

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    app.setApplicationName("Remote Mic Text Menu")
    controller = MenuController()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("menuController", controller)
    engine.load(QUrl.fromLocalFile(str(_qml_file())))
    if not engine.rootObjects():
        return 3

    def read_messages() -> None:
        try:
            text_stdin = sys.stdin
            input_stream = getattr(text_stdin, "buffer", None) or text_stdin
            if input_stream is None:
                return
            for line in input_stream:
                message = _parse_overlay_message_line(line)
                if message is None:
                    continue
                controller.messageReceived.emit(message)
                if message.get("quit"):
                    return
        finally:
            controller.messageReceived.emit({"quit": True})

    reader = threading.Thread(
        target=read_messages,
        name="RC003TextMenuMessages",
        daemon=True,
    )
    reader.start()
    return int(app.exec())
