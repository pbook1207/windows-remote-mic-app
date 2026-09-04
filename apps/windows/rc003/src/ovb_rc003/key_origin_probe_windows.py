"""Short-lived Windows diagnostic for comparing two keyboard origins.

The settings UI uses this module only after an explicit user action.  It
observes one configured shortcut twice (normally once on the local keyboard
and once through remote-control software), combines two independent Windows
signals, and never persists a device path:

* ``WH_KEYBOARD_LL`` says whether Windows marked the event as injected.
* Raw Input identifies the keyboard device that produced a physical/HID
  event.  The path is immediately reduced to an anonymous process-local
  fingerprint before it leaves the listener thread.
* The Qt settings window can report a selected shortcut delivered only to
  the foreground window.  This last channel is diagnostic evidence only:
  the background bridge cannot use a foreground-only event for routing.

Only keys that belong to the selected diagnostic shortcut are swallowed.
This prevents the test from starting Typeless/Handy/another foreground
dictation app; unrelated typing is never captured or suppressed.  Importing
the module is cross-platform.  Only ``KeyOriginProbe.start`` requires Win32.
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import threading
import time
import uuid
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence, Tuple, Union

from . import hotkey_capture_windows, win32_keys


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_INPUT = 0x00FF
WM_TIMER = 0x0113
WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012

LLKHF_EXTENDED = 0x00000001
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_INJECTED = 0x00000010
LLKHF_UP = 0x00000080

RI_KEY_BREAK = 0x0001
RI_KEY_E0 = 0x0002
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
HWND_MESSAGE = -3

_START_TIMEOUT_SECONDS = 5.0
_STOP_TIMEOUT_SECONDS = 2.0
_ASYNC_POLL_INTERVAL_MS = 8
_ASYNC_TIMER_ID = 1
_GLOBAL_HOTKEY_ID = 2

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


class KeyOriginProbeUnavailableError(RuntimeError):
    """Raised when the temporary Win32 listener cannot start or stop."""


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


@dataclass(frozen=True)
class LowLevelOriginEvent:
    token: str
    injected: bool
    lower_integrity_injected: bool
    scan_code: int
    extended: bool
    observed_ns: int


@dataclass(frozen=True)
class RawOriginEvent:
    token: str
    device_fingerprint: str
    scan_code: int
    extended: bool
    observed_ns: int


@dataclass(frozen=True)
class AsyncOriginEvent:
    """One selected-shortcut edge visible only through Windows key state."""

    token: str
    observed_ns: int


@dataclass(frozen=True)
class ForegroundOriginEvent:
    """One selected-shortcut edge delivered only to the focused Qt window."""

    token: str
    observed_ns: int


@dataclass(frozen=True)
class HotkeyMessageOriginEvent:
    """One selected shortcut delivered through Windows WM_HOTKEY."""

    token: str
    observed_ns: int


OriginEvent = Union[
    LowLevelOriginEvent,
    RawOriginEvent,
    AsyncOriginEvent,
    ForegroundOriginEvent,
    HotkeyMessageOriginEvent,
]
OriginEventCallback = Callable[[OriginEvent], None]


@dataclass(frozen=True)
class OriginStageSamples:
    low_level: Tuple[LowLevelOriginEvent, ...] = ()
    raw: Tuple[RawOriginEvent, ...] = ()
    async_state: Tuple[AsyncOriginEvent, ...] = ()
    foreground: Tuple[ForegroundOriginEvent, ...] = ()
    hotkey_message: Tuple[HotkeyMessageOriginEvent, ...] = ()


class OriginComparisonStatus(str, Enum):
    DISTINGUISHABLE = "distinguishable"
    INDISTINGUISHABLE = "indistinguishable"
    INSUFFICIENT = "insufficient"
    FOREGROUND_ONLY = "foreground_only"


@dataclass(frozen=True)
class OriginComparison:
    status: OriginComparisonStatus
    title: str
    detail: str
    method: str = ""


def dominant_raw_device(events: Sequence[RawOriginEvent]) -> Tuple[str, float]:
    counts = Counter(event.device_fingerprint for event in events)
    if not counts:
        return "", 0.0
    device, count = counts.most_common(1)[0]
    return device, count / sum(counts.values())


def stage_press_count(samples: OriginStageSamples) -> int:
    """Return the best press count available from either Windows channel.

    A low-level hook can be hidden from a lower-integrity process by another
    keyboard utility even though Raw Input still reports the physical key.
    Conversely, remote-control software commonly produces a low-level
    injected event without a Raw Input device.  Progress must therefore use
    both channels instead of treating the low-level hook as mandatory.

    Raw Input occasionally exposes the same key through more than one
    keyboard collection.  Counting only its dominant device avoids turning
    one physical press into multiple samples.
    """

    raw_counts = Counter(event.device_fingerprint for event in samples.raw)
    dominant_raw_count = raw_counts.most_common(1)[0][1] if raw_counts else 0
    return max(
        len(samples.low_level),
        dominant_raw_count,
        len(samples.async_state),
        len(samples.foreground),
        len(samples.hotkey_message),
    )


def compare_origin_stages(
    local: OriginStageSamples,
    remote: OriginStageSamples,
    *,
    minimum_presses: int = 3,
) -> OriginComparison:
    """Compare two user-labelled rounds without guessing from one event.

    A strong injected/non-injected split wins first.  Otherwise two stable,
    different Raw Input devices are sufficient.  Missing Raw Input while
    both sides are non-injected is reported as insufficient rather than as a
    false "same source" conclusion.
    """

    local_count = stage_press_count(local)
    remote_count = stage_press_count(remote)
    if local_count < minimum_presses or remote_count < minimum_presses:
        return OriginComparison(
            OriginComparisonStatus.INSUFFICIENT,
            "样本不足",
            f"实体键盘记录 {local_count} 次，远程控制输入记录 "
            f"{remote_count} 次；每边至少需要 {minimum_presses} 次。",
        )

    local_injected = (
        sum(event.injected for event in local.low_level) / len(local.low_level)
        if local.low_level
        else None
    )
    remote_injected = (
        sum(event.injected for event in remote.low_level) / len(remote.low_level)
        if remote.low_level
        else None
    )
    if (
        local_injected is not None
        and remote_injected is not None
        and (
            (local_injected <= 0.2 and remote_injected >= 0.8)
            or (remote_injected <= 0.2 and local_injected >= 0.8)
        )
    ):
        return OriginComparison(
            OriginComparisonStatus.DISTINGUISHABLE,
            "可以区分实体键盘与远程控制输入",
            "两组按键的软件注入标记稳定不同，可以在按下快捷键时提前选择对应麦克风。",
            method="injection_flag",
        )

    local_device, local_stability = dominant_raw_device(local.raw)
    remote_device, remote_stability = dominant_raw_device(remote.raw)
    local_raw_ready = (
        len(local.raw) >= minimum_presses and local_stability >= 0.8
    )
    remote_raw_ready = (
        len(remote.raw) >= minimum_presses and remote_stability >= 0.8
    )
    remote_async_ready = len(remote.async_state) >= minimum_presses
    remote_foreground_ready = len(remote.foreground) >= minimum_presses
    remote_hotkey_ready = len(remote.hotkey_message) >= minimum_presses

    # This is the common fallback when another keyboard utility prevents the
    # settings process from seeing physical low-level hook events: Raw Input
    # still proves the first round came from hardware, while the second round
    # retains Windows' injected marker.
    if (
        local_raw_ready
        and remote_injected is not None
        and remote_injected >= 0.8
    ):
        return OriginComparison(
            OriginComparisonStatus.DISTINGUISHABLE,
            "可以区分实体键盘与远程控制输入",
            "实体键盘由硬件输入通道识别，远程控制输入带有稳定的软件注入标记，可以按来源选择对应麦克风。",
            method="raw_local_injected_remote",
        )

    if local_raw_ready and remote_async_ready and not remote_raw_ready:
        return OriginComparison(
            OriginComparisonStatus.DISTINGUISHABLE,
            "可以区分实体键盘与远程控制输入",
            "实体键盘具有稳定的硬件设备来源，而远程控制输入只出现在 Windows 按键状态通道，可以按来源选择对应麦克风。",
            method="raw_local_async_remote",
        )

    if local_raw_ready and remote_hotkey_ready and not remote_raw_ready:
        return OriginComparison(
            OriginComparisonStatus.DISTINGUISHABLE,
            "可以通过 Windows 全局热键识别远程输入",
            "实体键盘具有稳定的硬件来源，远程控制输入能触发后台全局热键消息；当前组合可用于按键触发时选择对应麦克风。",
            method="raw_local_hotkey_remote",
        )

    if (
        local_raw_ready
        and remote_foreground_ready
        and not remote.low_level
        and not remote_raw_ready
        and not remote_async_ready
    ):
        return OriginComparison(
            OriginComparisonStatus.FOREGROUND_ONLY,
            "远程按键只对前台窗口可见",
            "远程控制输入没有进入 Windows 的后台键盘通道，只送到了当前获得焦点的窗口；后台桥接无法据此提前选择麦克风，应改用声音活动检测作为自动切换依据。",
            method="raw_local_foreground_remote",
        )

    if (
        local_raw_ready
        and remote_raw_ready
        and local_device != remote_device
    ):
        return OriginComparison(
            OriginComparisonStatus.DISTINGUISHABLE,
            "可以区分实体键盘与远程控制输入",
            "两组按键来自不同且稳定的键盘设备，可以按设备来源选择对应麦克风。",
            method="raw_input_device",
        )

    if (
        local_raw_ready
        and remote_raw_ready
        and local_device == remote_device
        and (
            local_injected is None
            or remote_injected is None
            or abs(local_injected - remote_injected) < 0.25
        )
    ):
        return OriginComparison(
            OriginComparisonStatus.INDISTINGUISHABLE,
            "仅靠按键无法可靠区分",
            "Windows 将实体键盘与当前远程控制输入报告为同一来源；自动选择需要改用麦克风声音活动作为兜底。",
            method="same_windows_origin",
        )

    return OriginComparison(
        OriginComparisonStatus.INSUFFICIENT,
        "检测结果暂不确定",
        "按键事件已收到，但设备信息不够稳定。请关闭其他键盘工具后重新检测，或采用声音活动兜底。",
    )


def anonymous_device_fingerprint(device_path: str, *, salt: str = "") -> str:
    normalized = (salt + "\0" + device_path.strip().casefold()).encode(
        "utf-8", "replace"
    )
    return hashlib.sha256(normalized).hexdigest()[:12]


def _target_tokens(chord_text: str) -> Tuple[Tuple[str, ...], str]:
    from . import hotkey

    spec = hotkey.HotkeySpec.parse(chord_text)
    ordered = tuple(spec.modifiers) + (spec.key,)
    # Preserve order while removing a duplicated modifier/trigger token.
    tokens = tuple(dict.fromkeys(token.casefold() for token in ordered))
    win32_keys.resolve_vk_codes(tokens)
    return tokens, spec.key.casefold()


def _token_aliases(token: str) -> Tuple[str, ...]:
    """Directional/generic modifier spellings worth observing in diagnostics."""

    families = {
        "lalt": ("lalt", "ralt", "alt", "vk_12"),
        "ralt": ("ralt", "lalt", "alt", "vk_12"),
        "lctrl": ("lctrl", "rctrl", "ctrl", "vk_11"),
        "rctrl": ("rctrl", "lctrl", "ctrl", "vk_11"),
        "lshift": ("lshift", "rshift", "shift", "vk_10"),
        "rshift": ("rshift", "lshift", "shift", "vk_10"),
        "lwin": ("lwin", "rwin", "win"),
        "rwin": ("rwin", "lwin", "win"),
    }
    return families.get(token, (token,))


def _async_vk_candidates(token: str) -> Tuple[int, ...]:
    candidates = []
    for alias in _token_aliases(token):
        try:
            vk = win32_keys.resolve_vk_codes((alias,))[0]
        except win32_keys.UnknownKeyTokenError:
            continue
        if vk not in candidates:
            candidates.append(vk)
    return tuple(candidates)


def _register_hotkey_components(
    tokens: Sequence[str], trigger_token: str
) -> Tuple[int, int]:
    modifier_bits = 0
    for token in tokens:
        if token == trigger_token:
            continue
        aliases = set(_token_aliases(token))
        if aliases & {"alt", "lalt", "ralt", "vk_12"}:
            modifier_bits |= MOD_ALT
        elif aliases & {"ctrl", "lctrl", "rctrl", "vk_11"}:
            modifier_bits |= MOD_CONTROL
        elif aliases & {"shift", "lshift", "rshift", "vk_10"}:
            modifier_bits |= MOD_SHIFT
        elif aliases & {"win", "lwin", "rwin"}:
            modifier_bits |= MOD_WIN
    trigger_vk = win32_keys.resolve_vk_codes((trigger_token,))[0]
    return modifier_bits | MOD_NOREPEAT, trigger_vk


class KeyOriginProbe:
    """Own a temporary low-level hook and all-keyboard Raw Input window."""

    def __init__(
        self,
        chord_text: str,
        on_event: OriginEventCallback,
        *,
        device_salt: Optional[str] = None,
    ) -> None:
        self._tokens, self._trigger_token = _target_tokens(chord_text)
        self._vk_codes = win32_keys.resolve_vk_codes(self._tokens)
        self._token_set = frozenset(self._tokens)
        self._observed_token_set = frozenset(
            alias for token in self._tokens for alias in _token_aliases(token)
        )
        self._async_vk_groups = tuple(
            _async_vk_candidates(token) for token in self._tokens
        )
        self._hotkey_modifiers, self._hotkey_vk = _register_hotkey_components(
            self._tokens, self._trigger_token
        )
        self._global_hotkey_available = False
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._hwnd = None
        self._hook = None
        self._hookproc_keepalive = None
        self._wndproc_keepalive = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._pressed_tokens: set[str] = set()
        self._raw_pressed: set[Tuple[str, int, int]] = set()
        self._async_chord_down = False
        self._foreground_pressed: set[str] = set()
        self._class_name = f"RemoteMicKeyOriginProbe-{uuid.uuid4().hex}"
        # Diagnostics use a new salt by default.  The settings controller
        # may explicitly supply its install-local salt when the user wants
        # the learned fingerprints to be usable by the running bridge.
        self._device_salt = device_salt or uuid.uuid4().hex

    @property
    def trigger_token(self) -> str:
        return self._trigger_token

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def global_hotkey_available(self) -> bool:
        return self._global_hotkey_available

    def start(self, *, start_timeout: float = _START_TIMEOUT_SECONDS) -> None:
        if sys.platform != "win32":
            raise KeyOriginProbeUnavailableError(
                "按键来源检测仅支持 Windows"
            )
        if self.is_running:
            raise KeyOriginProbeUnavailableError("按键来源检测已经在运行")
        self._ready.clear()
        self._stop.clear()
        self._start_error = None
        self._pressed_tokens.clear()
        self._raw_pressed.clear()
        self._async_chord_down = False
        self._foreground_pressed.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(start_timeout):
            self.stop()
            raise KeyOriginProbeUnavailableError("按键来源检测启动超时")
        if self._start_error is not None:
            error = self._start_error
            thread = self._thread
            if thread is not None:
                thread.join(_STOP_TIMEOUT_SECONDS)
            self._thread = None
            raise KeyOriginProbeUnavailableError(str(error)) from error

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            if self._hwnd:
                user32.PostMessageW.argtypes = (
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostMessageW.restype = wintypes.BOOL
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            elif self._thread_id:
                user32.PostThreadMessageW.argtypes = (
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostThreadMessageW.restype = wintypes.BOOL
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        thread.join(_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise KeyOriginProbeUnavailableError("按键来源检测没有正常停止")
        self._thread = None
        self._thread_id = 0

    def _emit(self, event: OriginEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            # The GUI may already be closing. The listener must still unwind.
            pass

    def _handle_low_level_event(
        self, message: int, data: KBDLLHOOKSTRUCT
    ) -> bool:
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP) or bool(
            int(data.flags) & LLKHF_UP
        )
        if not (is_down or is_up):
            return False
        token = hotkey_capture_windows.token_for_keyboard_event(
            int(data.vkCode), int(data.scanCode), int(data.flags)
        )
        if token not in self._observed_token_set:
            return False
        first_down = is_down and token not in self._pressed_tokens
        if is_down:
            self._pressed_tokens.add(token)
        else:
            self._pressed_tokens.discard(token)
        if first_down and token in _token_aliases(self._trigger_token):
            flags = int(data.flags)
            self._emit(
                LowLevelOriginEvent(
                    token=token,
                    injected=bool(flags & LLKHF_INJECTED),
                    lower_integrity_injected=bool(
                        flags & LLKHF_LOWER_IL_INJECTED
                    ),
                    scan_code=int(data.scanCode),
                    extended=bool(flags & LLKHF_EXTENDED),
                    observed_ns=time.monotonic_ns(),
                )
            )
        # Suppress every edge in the selected chord so the diagnostic never
        # starts/stops a foreground dictation app.
        return True

    def _handle_raw_keyboard(
        self,
        *,
        device_path: str,
        vkey: int,
        make_code: int,
        flags: int,
        message: int,
    ) -> None:
        ll_flags = LLKHF_EXTENDED if flags & RI_KEY_E0 else 0
        token = hotkey_capture_windows.token_for_keyboard_event(
            vkey, make_code, ll_flags
        )
        if token not in _token_aliases(self._trigger_token):
            return
        device_id = anonymous_device_fingerprint(
            device_path, salt=self._device_salt
        )
        identity = (device_id, int(vkey), int(make_code))
        is_up = bool(flags & RI_KEY_BREAK) or message in (
            WM_KEYUP,
            WM_SYSKEYUP,
        )
        if is_up:
            self._raw_pressed.discard(identity)
            return
        if identity in self._raw_pressed:
            return
        self._raw_pressed.add(identity)
        self._emit(
            RawOriginEvent(
                token=token,
                device_fingerprint=device_id,
                scan_code=int(make_code),
                extended=bool(flags & RI_KEY_E0),
                observed_ns=time.monotonic_ns(),
            )
        )

    def _poll_async_key_state(self, user32) -> None:
        """Sample a selected chord when remote software exposes no hook/raw event.

        ``GetAsyncKeyState`` carries no trustworthy device identity, so this
        channel is used only for progress and as a labelled fallback when the
        local round already supplied a stable Raw Input device.
        """

        chord_down = all(
            any(
                bool(int(user32.GetAsyncKeyState(vk)) & 0x8000)
                for vk in group
            )
            for group in self._async_vk_groups
        )
        if chord_down and not self._async_chord_down:
            self._emit(
                AsyncOriginEvent(
                    token=self._trigger_token,
                    observed_ns=time.monotonic_ns(),
                )
            )
        self._async_chord_down = chord_down

    def _foreground_token(
        self, native_vk: int, native_scan_code: int, qt_key: int
    ) -> str:
        """Reduce one QKeyEvent to the same token vocabulary as Win32 input.

        Some remote clients provide no native VK at all for a modifier-only
        key.  In that narrow case the generic Qt modifier is mapped to the
        matching directional modifier configured for this explicit test.
        It is still labelled foreground-only and is never treated as a
        background-capable origin signal.
        """

        vk = int(native_vk) & 0xFF
        scan = int(native_scan_code)
        if vk:
            flags = LLKHF_EXTENDED if scan > 0xFF else 0
            return hotkey_capture_windows.token_for_keyboard_event(
                vk, scan, flags
            )

        qt_fallbacks = {
            0x01000020: ("lshift", "rshift"),
            0x01000021: ("lctrl", "rctrl"),
            0x01000022: ("lwin", "rwin"),
            0x01000023: ("lalt", "ralt"),
            0x20: ("space",),
        }
        candidates = qt_fallbacks.get(int(qt_key), ())
        configured = [token for token in candidates if token in self._token_set]
        if len(configured) == 1:
            return configured[0]
        return candidates[0] if len(candidates) == 1 else ""

    def _observed_chord_is_down(self, pressed: set[str]) -> bool:
        return all(
            any(alias in pressed for alias in _token_aliases(configured))
            for configured in self._tokens
        )

    def handle_foreground_key_event(
        self,
        *,
        native_vk: int,
        native_scan_code: int,
        qt_key: int,
        is_press: bool,
        is_auto_repeat: bool = False,
    ) -> bool:
        """Observe a selected shortcut delivered to the focused Qt window.

        Returns true only for keys belonging to the selected diagnostic
        chord, allowing the Qt event filter to swallow those keys just like
        the global hook does.  Unrelated typing is neither recorded nor
        suppressed.
        """

        token = self._foreground_token(native_vk, native_scan_code, qt_key)
        if token not in self._observed_token_set:
            return False
        if is_press:
            first_down = token not in self._foreground_pressed
            self._foreground_pressed.add(token)
            if (
                first_down
                and not is_auto_repeat
                and token in _token_aliases(self._trigger_token)
                and self._observed_chord_is_down(self._foreground_pressed)
            ):
                self._emit(
                    ForegroundOriginEvent(
                        token=token,
                        observed_ns=time.monotonic_ns(),
                    )
                )
        else:
            self._foreground_pressed.discard(token)
        return True

    @staticmethod
    def _device_path(user32, handle) -> str:
        size = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(
            handle, RIDI_DEVICENAME, None, ctypes.byref(size)
        )
        if not size.value:
            return "unresolved"
        buffer = ctypes.create_unicode_buffer(size.value)
        written = user32.GetRawInputDeviceInfoW(
            handle, RIDI_DEVICENAME, buffer, ctypes.byref(size)
        )
        if written in (0, 0xFFFFFFFF):
            return "unresolved"
        return buffer.value or "unresolved"

    def _read_raw_input(self, user32, lparam: int) -> None:
        size = wintypes.UINT(0)
        user32.GetRawInputData(
            lparam,
            RID_INPUT,
            None,
            ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if not size.value:
            return
        buffer = ctypes.create_string_buffer(size.value)
        written = user32.GetRawInputData(
            lparam,
            RID_INPUT,
            buffer,
            ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        if written != size.value:
            return
        header = RAWINPUTHEADER.from_buffer_copy(buffer, 0)
        if int(header.dwType) != RIM_TYPEKEYBOARD:
            return
        offset = ctypes.sizeof(RAWINPUTHEADER)
        if size.value < offset + ctypes.sizeof(RAWKEYBOARD):
            return
        keyboard = RAWKEYBOARD.from_buffer_copy(buffer, offset)
        self._handle_raw_keyboard(
            device_path=self._device_path(user32, header.hDevice),
            vkey=int(keyboard.VKey),
            make_code=int(keyboard.MakeCode),
            flags=int(keyboard.Flags),
            message=int(keyboard.Message),
        )

    def _run(self) -> None:  # noqa: C901 - one contained Win32 lifecycle
        user32 = None
        kernel32 = None
        hook = None
        hwnd = None
        hinstance = None
        class_registered = False
        raw_registered = False
        timer_registered = False
        hotkey_registered = False
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            lresult = ctypes.c_ssize_t
            hookproc_type = ctypes.WINFUNCTYPE(
                lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
            )
            wndproc_type = ctypes.WINFUNCTYPE(
                lresult,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", wndproc_type),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class RAWINPUTDEVICE(ctypes.Structure):
                _fields_ = [
                    ("usUsagePage", wintypes.USHORT),
                    ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD),
                    ("hwndTarget", wintypes.HWND),
                ]

            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE

            user32.RegisterClassW.argtypes = (
                ctypes.POINTER(WNDCLASSW),
            )
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.UnregisterClassW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.HINSTANCE,
            )
            user32.UnregisterClassW.restype = wintypes.BOOL
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.DefWindowProcW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.DefWindowProcW.restype = lresult
            user32.PostQuitMessage.argtypes = (ctypes.c_int,)
            user32.PostMessageW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostMessageW.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            user32.TranslateMessage.argtypes = (
                ctypes.POINTER(wintypes.MSG),
            )
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
            )
            user32.DispatchMessageW.restype = lresult
            user32.RegisterRawInputDevices.argtypes = (
                ctypes.POINTER(RAWINPUTDEVICE),
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.RegisterRawInputDevices.restype = wintypes.BOOL
            user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
            user32.GetAsyncKeyState.restype = wintypes.SHORT
            user32.SetTimer.argtypes = (
                wintypes.HWND,
                ctypes.c_size_t,
                wintypes.UINT,
                wintypes.LPVOID,
            )
            user32.SetTimer.restype = ctypes.c_size_t
            user32.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
            user32.KillTimer.restype = wintypes.BOOL
            user32.RegisterHotKey.argtypes = (
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.UnregisterHotKey.restype = wintypes.BOOL
            user32.GetRawInputData.argtypes = (
                wintypes.HANDLE,
                wintypes.UINT,
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.UINT),
                wintypes.UINT,
            )
            user32.GetRawInputData.restype = wintypes.UINT
            user32.GetRawInputDeviceInfoW.argtypes = (
                wintypes.HANDLE,
                wintypes.UINT,
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.UINT),
            )
            user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hookproc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = (
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.CallNextHookEx.restype = lresult
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            self._thread_id = int(kernel32.GetCurrentThreadId())

            def hook_proc(n_code, w_param, l_param):
                if n_code < 0:
                    return user32.CallNextHookEx(
                        hook, n_code, w_param, l_param
                    )
                data = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                if self._handle_low_level_event(int(w_param), data):
                    return 1
                return user32.CallNextHookEx(hook, n_code, w_param, l_param)

            def wnd_proc(window, message, w_param, l_param):
                if message == WM_INPUT:
                    self._read_raw_input(user32, int(l_param))
                    return 0
                if message == WM_TIMER and int(w_param) == _ASYNC_TIMER_ID:
                    self._poll_async_key_state(user32)
                    return 0
                if message == WM_HOTKEY and int(w_param) == _GLOBAL_HOTKEY_ID:
                    self._emit(
                        HotkeyMessageOriginEvent(
                            token=self._trigger_token,
                            observed_ns=time.monotonic_ns(),
                        )
                    )
                    return 0
                if message == WM_CLOSE:
                    user32.DestroyWindow(window)
                    return 0
                if message == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(
                    window, message, w_param, l_param
                )

            self._hookproc_keepalive = hookproc_type(hook_proc)
            self._wndproc_keepalive = wndproc_type(wnd_proc)
            hinstance = kernel32.GetModuleHandleW(None)
            window_class = WNDCLASSW(
                0,
                self._wndproc_keepalive,
                0,
                0,
                hinstance,
                None,
                None,
                None,
                None,
                self._class_name,
            )
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            class_registered = True
            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                self._class_name,
                0,
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self._hwnd = hwnd

            hotkey_registered = bool(
                user32.RegisterHotKey(
                    hwnd,
                    _GLOBAL_HOTKEY_ID,
                    self._hotkey_modifiers,
                    self._hotkey_vk,
                )
            )
            self._global_hotkey_available = hotkey_registered

            raw_device = RAWINPUTDEVICE(
                0x01, 0x06, RIDEV_INPUTSINK, hwnd
            )
            if not user32.RegisterRawInputDevices(
                ctypes.byref(raw_device),
                1,
                ctypes.sizeof(RAWINPUTDEVICE),
            ):
                raise ctypes.WinError()
            raw_registered = True

            timer_id = user32.SetTimer(
                hwnd,
                _ASYNC_TIMER_ID,
                _ASYNC_POLL_INTERVAL_MS,
                None,
            )
            if timer_id != _ASYNC_TIMER_ID:
                raise ctypes.WinError()
            timer_registered = True

            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._hookproc_keepalive,
                hinstance,
                0,
            )
            if not hook:
                raise ctypes.WinError()
            self._hook = hook
            self._ready.set()

            message = wintypes.MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(
                    ctypes.byref(message), None, 0, 0
                )
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:  # noqa: BLE001 - surfaced by start()
            self._start_error = exc
            self._ready.set()
        finally:
            if user32 is not None and hotkey_registered and hwnd:
                try:
                    user32.UnregisterHotKey(hwnd, _GLOBAL_HOTKEY_ID)
                except Exception:
                    pass
            if user32 is not None and timer_registered and hwnd:
                try:
                    user32.KillTimer(hwnd, _ASYNC_TIMER_ID)
                except Exception:
                    pass
            if user32 is not None and hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    pass
            if user32 is not None and raw_registered:
                try:
                    # RIDEV_REMOVE requires a null target.
                    remove = RAWINPUTDEVICE(0x01, 0x06, RIDEV_REMOVE, None)
                    user32.RegisterRawInputDevices(
                        ctypes.byref(remove),
                        1,
                        ctypes.sizeof(RAWINPUTDEVICE),
                    )
                except Exception:
                    pass
            if user32 is not None and hwnd:
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
            if user32 is not None and class_registered:
                try:
                    user32.UnregisterClassW(self._class_name, hinstance)
                except Exception:
                    pass
            self._hook = None
            self._global_hotkey_available = False
            self._hwnd = None
            self._hookproc_keepalive = None
            self._wndproc_keepalive = None
            self._ready.set()
