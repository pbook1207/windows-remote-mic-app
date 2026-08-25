"""RC003 -> Windows *semantic* action mapping.

The reference app stores behavior actions (``arrowUp``, ``showDesktop``,
``openCodex``), not presentation strings such as ``"up"`` or
``"win+d"``.  Windows keeps that same separation: this module is pure action
data, while :mod:`app` and :mod:`win32_input` execute each action through its
own platform operation.  ``KEY_COMBO`` remains available for a genuinely
custom user shortcut and for backward-compatible hand-edited files.

Default table matches the reference app's action choices:

| RC003 按键 | Windows 候选动作 |
| --- | --- |
| 麦克风 | 专用语音生命周期 |
| 电源 | Escape |
| 上 / 下 / 左 / 右 | 对应方向动作 |
| 确定 | Return |
| 返回 | Delete（退格） |
| 音量 + / − | 系统音量 + / − |
| 主页 | 显示桌面 |
| 菜单 | 上下文菜单 |
| TV | 应用切换 |

The RC003 HID usage table also defines a "volume_mute" usage (see
device_profile.BUTTON_USAGE_IDS), but this Windows client documents that the
physical remote has no dedicated mute key - "系统静音" is only an optional
assignable action, never a default. This module mirrors that: mute is a valid,
bindable logical button but intentionally has no default entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class ActionKind(str, Enum):
    DISABLED = "disabled"
    KEY_COMBO = "key_combo"
    ESCAPE = "escape"
    RETURN = "return"
    ARROW_UP = "arrow_up"
    ARROW_DOWN = "arrow_down"
    ARROW_LEFT = "arrow_left"
    ARROW_RIGHT = "arrow_right"
    DELETE_BACKWARD = "delete_backward"
    SHOW_DESKTOP = "show_desktop"
    CONTEXT_MENU = "context_menu"
    APP_SWITCHER = "app_switcher"
    SYSTEM_VOLUME_UP = "system_volume_up"
    SYSTEM_VOLUME_DOWN = "system_volume_down"
    SYSTEM_VOLUME_MUTE = "system_volume_mute"
    PLAY_PAUSE = "play_pause"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    VIRTUAL_DESKTOP_LEFT = "virtual_desktop_left"
    VIRTUAL_DESKTOP_RIGHT = "virtual_desktop_right"
    TASK_VIEW = "task_view"
    CLIPBOARD_HISTORY = "clipboard_history"
    PREVIOUS_TAB = "previous_tab"
    NEXT_TAB = "next_tab"
    BROWSER_BACK = "browser_back"
    BROWSER_FORWARD = "browser_forward"
    SNAP_WINDOW_LEFT = "snap_window_left"
    SNAP_WINDOW_RIGHT = "snap_window_right"
    MAXIMIZE_WINDOW = "maximize_window"
    RESTORE_MINIMIZE_WINDOW = "restore_minimize_window"
    TYPE_TEXT = "type_text"
    OPEN_TEXT_MENU = "open_text_menu"
    # Compatibility-only values from earlier prototypes. Config migration
    # converts both to TYPE_TEXT, which never presses Enter.
    TYPE_TEXT_AND_SUBMIT = "type_text_and_submit"
    # Kept only so bindings saved by the first fixed-text prototype continue
    # to load. New saves use TYPE_TEXT with an explicit text field.
    TYPE_EXECUTE_AND_SUBMIT = "type_execute_and_submit"
    VOICE = "voice"
    OPEN_REMOTE_MIC = "open_remote_mic"
    OPEN_CODEX = "open_codex"
    OPEN_CLAUDE = "open_claude"
    OPEN_CMUX = "open_cmux"
    OPEN_WECHAT = "open_wechat"
    OPEN_CURSOR = "open_cursor"
    OPEN_SLACK = "open_slack"
    OPEN_WECOM = "open_wecom"
    OPEN_NETEASE_MUSIC = "open_netease_music"
    OPEN_CHROME = "open_chrome"
    OPEN_EDGE = "open_edge"
    OPEN_ZED = "open_zed"


class ButtonTrigger(str, Enum):
    """The three gestures available for an ordinary RC003 button."""

    SINGLE_CLICK = "single_click"
    DOUBLE_CLICK = "double_click"
    LONG_PRESS = "long_press"


class VoiceTriggerMode(str, Enum):
    TOGGLE = "toggle"
    HOLD = "hold"
    # Typeless for Windows uses Right Alt as a toggle: one completed key tap
    # starts dictation and a second completed tap finishes it.  Keep this
    # distinct from HOLD even though both presets use the same physical key.
    TYPELESS = "typeless"
    # Diagnostic-only mode: emit the opening Right-Alt tap but never emit the
    # automatic closing tap.
    TYPELESS_START_ONLY = "typeless_start_only"


VOICE_HOTKEY_PRESETS = {
    VoiceTriggerMode.TOGGLE: "ralt+space",
    # Doubao's long-press mode is configured as the physical right Alt key.
    # The Windows bridge emits a right-Alt virtual-key edge after swallowing
    # RC003's leaked F5, so the host never sees F5 or a left-side modifier.
    VoiceTriggerMode.HOLD: "ralt",
    VoiceTriggerMode.TYPELESS: "ralt",
    VoiceTriggerMode.TYPELESS_START_ONLY: "ralt",
}

# These values were shipped by earlier Windows builds. They are reserved
# built-ins rather than user customizations, so config migration may replace
# either spelling with the current physical shortcut.
LEGACY_VOICE_HOTKEYS = frozenset(
    {"ralt", "ralt+space", "lctrl+win", "lctrl+lwin"}
)


# Exact old Windows chords that were previously presented as the reference
# action labels.  These are migrations, not the representation used for new
# saves.  ``alt+esc`` is included because the first Windows build shipped it
# as the TV default before it was corrected to the native app-switch action.
LEGACY_SEMANTIC_ACTIONS = {
    ("escape",): ActionKind.ESCAPE,
    ("enter",): ActionKind.RETURN,
    ("up",): ActionKind.ARROW_UP,
    ("down",): ActionKind.ARROW_DOWN,
    ("left",): ActionKind.ARROW_LEFT,
    ("right",): ActionKind.ARROW_RIGHT,
    ("backspace",): ActionKind.DELETE_BACKWARD,
    ("win", "d"): ActionKind.SHOW_DESKTOP,
    ("shift", "f10"): ActionKind.CONTEXT_MENU,
    ("alt", "tab"): ActionKind.APP_SWITCHER,
    ("alt", "esc"): ActionKind.APP_SWITCHER,
    ("page_up",): ActionKind.PAGE_UP,
    ("page_down",): ActionKind.PAGE_DOWN,
    ("win", "ctrl", "left"): ActionKind.VIRTUAL_DESKTOP_LEFT,
    ("win", "ctrl", "right"): ActionKind.VIRTUAL_DESKTOP_RIGHT,
    ("ctrl", "win", "left"): ActionKind.VIRTUAL_DESKTOP_LEFT,
    ("ctrl", "win", "right"): ActionKind.VIRTUAL_DESKTOP_RIGHT,
    ("win", "tab"): ActionKind.TASK_VIEW,
    ("win", "v"): ActionKind.CLIPBOARD_HISTORY,
    ("ctrl", "shift", "tab"): ActionKind.PREVIOUS_TAB,
    ("ctrl", "tab"): ActionKind.NEXT_TAB,
    ("browser_back",): ActionKind.BROWSER_BACK,
    ("browser_forward",): ActionKind.BROWSER_FORWARD,
    ("win", "left"): ActionKind.SNAP_WINDOW_LEFT,
    ("win", "right"): ActionKind.SNAP_WINDOW_RIGHT,
    ("win", "up"): ActionKind.MAXIMIZE_WINDOW,
    ("win", "down"): ActionKind.RESTORE_MINIMIZE_WINDOW,
}


APPLICATION_ACTIONS = frozenset(
    {
        ActionKind.OPEN_REMOTE_MIC,
        ActionKind.OPEN_CODEX,
        ActionKind.OPEN_CLAUDE,
        ActionKind.OPEN_CMUX,
        ActionKind.OPEN_WECHAT,
        ActionKind.OPEN_CURSOR,
        ActionKind.OPEN_SLACK,
        ActionKind.OPEN_WECOM,
        ActionKind.OPEN_NETEASE_MUSIC,
        ActionKind.OPEN_CHROME,
        ActionKind.OPEN_EDGE,
        ActionKind.OPEN_ZED,
    }
)


ONE_SHOT_ACTIONS = APPLICATION_ACTIONS | {
    ActionKind.TYPE_TEXT,
    ActionKind.OPEN_TEXT_MENU,
    ActionKind.TYPE_TEXT_AND_SUBMIT,
    ActionKind.TYPE_EXECUTE_AND_SUBMIT,
    ActionKind.VIRTUAL_DESKTOP_LEFT,
    ActionKind.VIRTUAL_DESKTOP_RIGHT,
    ActionKind.TASK_VIEW,
    ActionKind.CLIPBOARD_HISTORY,
    ActionKind.PREVIOUS_TAB,
    ActionKind.NEXT_TAB,
    ActionKind.BROWSER_BACK,
    ActionKind.BROWSER_FORWARD,
    ActionKind.SNAP_WINDOW_LEFT,
    ActionKind.SNAP_WINDOW_RIGHT,
    ActionKind.MAXIMIZE_WINDOW,
    ActionKind.RESTORE_MINIMIZE_WINDOW,
}


MAX_TEXT_SUBMIT_LENGTH = 200
MAX_TEXT_MENU_ITEMS = 30
MAX_TEXT_MENU_LABEL_LENGTH = 40


def normalize_text_submit(value: str) -> str:
    """Validate one single-line text-submission payload."""

    if not isinstance(value, str):
        raise ValueError("text submission payload must be a string")
    text = value.strip()
    if not text:
        raise ValueError("text submission payload must not be empty")
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValueError("text submission payload must be one line")
    if len(text) > MAX_TEXT_SUBMIT_LENGTH:
        raise ValueError(
            f"text submission payload must not exceed {MAX_TEXT_SUBMIT_LENGTH} characters"
        )
    return text


@dataclass(frozen=True)
class TextMenuItem:
    label: str
    text: str
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "text": self.text,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TextMenuItem":
        if not isinstance(data, dict):
            raise TypeError("text menu item must be a mapping")
        text = normalize_text_submit(data.get("text", ""))
        raw_label = data.get("label", "")
        if not isinstance(raw_label, str):
            raise ValueError("text menu label must be a string")
        label = raw_label.strip() or text[:MAX_TEXT_MENU_LABEL_LENGTH]
        if any(character in label for character in ("\r", "\n", "\x00")):
            raise ValueError("text menu label must be one line")
        if len(label) > MAX_TEXT_MENU_LABEL_LENGTH:
            raise ValueError(
                f"text menu label must not exceed {MAX_TEXT_MENU_LABEL_LENGTH} characters"
            )
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("text menu enabled flag must be boolean")
        return cls(label=label, text=text, enabled=enabled)


def normalize_text_menu_items(raw_items: object) -> Tuple[TextMenuItem, ...]:
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("text menu items must be a list")
    if len(raw_items) > MAX_TEXT_MENU_ITEMS:
        raise ValueError(
            f"text menu must not exceed {MAX_TEXT_MENU_ITEMS} items"
        )
    return tuple(TextMenuItem.from_dict(item) for item in raw_items)


def default_text_menu_items() -> Tuple[TextMenuItem, ...]:
    return (
        TextMenuItem(label="执行", text="执行"),
        TextMenuItem(label="继续", text="继续"),
        TextMenuItem(label="总结", text="请总结上述内容"),
    )


def semantic_action_for_keys(keys: Tuple[str, ...]) -> Optional["ButtonAction"]:
    """Return the semantic action represented by one legacy key tuple."""

    action_kind = LEGACY_SEMANTIC_ACTIONS.get(tuple(keys))
    if action_kind is None:
        return None
    return ButtonAction(action_kind)


def action_allows_repeat(action: "ButtonAction") -> bool:
    """Match the reference app's ``allowsRepeat`` behavior.

    Opening an application and submitting text are one-shot operations.
    Ordinary keyboard/system actions can repeat when the physical button
    itself is a repeatable control.
    """

    return action.kind not in ONE_SHOT_ACTIONS


def voice_trigger_mode_for_hotkey(hotkey_text: str) -> Optional[VoiceTriggerMode]:
    """Infer the built-in voice trigger semantics from a recorded chord.

    The two current Doubao voice modes are not interchangeable: ``ralt+space``
    is a toggle, while ``ralt`` is held for the duration of speech. The old
    Ctrl+Win chord is still recognized so legacy settings and recordings can
    be migrated, but it is not the current HOLD preset. A physical recorder
    can return either generic or directional Win, and users may press the
    keys in either order, so compare the normalized token set. Return ``None``
    for a genuinely custom shortcut and leave its selected mode under user
    control.
    """

    tokens = frozenset(
        token.strip().lower()
        for token in str(hotkey_text).split("+")
        if token.strip()
    )
    if tokens == frozenset({"ralt"}):
        return VoiceTriggerMode.HOLD
    if tokens == frozenset({"ralt", "space"}):
        return VoiceTriggerMode.TOGGLE
    if (
        len(tokens) == 2
        and "lctrl" in tokens
        and bool(tokens & {"win", "lwin", "rwin"})
    ):
        return VoiceTriggerMode.HOLD
    return None


def voice_hotkey_for_trigger_mode(trigger_mode: VoiceTriggerMode) -> str:
    """Return the host shortcut paired with a voice trigger mode."""

    return VOICE_HOTKEY_PRESETS[trigger_mode]


@dataclass(frozen=True)
class ButtonAction:
    kind: ActionKind
    keys: Tuple[str, ...] = field(default_factory=tuple)
    text: str = ""

    def to_dict(self) -> dict:
        data = {"kind": self.kind.value, "keys": list(self.keys)}
        if self.kind in {
            ActionKind.TYPE_TEXT,
            ActionKind.TYPE_TEXT_AND_SUBMIT,
        }:
            data["text"] = normalize_text_submit(self.text)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ButtonAction":
        if not isinstance(data, dict):
            raise TypeError("button action must be a mapping")
        kind = ActionKind(data["kind"])
        raw_keys = data.get("keys", ())
        if not isinstance(raw_keys, (list, tuple)) or not all(
            isinstance(key, str) and key.strip() for key in raw_keys
        ):
            raise ValueError("button action keys must be non-empty strings")
        keys = tuple(key.strip().lower() for key in raw_keys)
        raw_text = data.get("text", "")
        if not isinstance(raw_text, str):
            raise ValueError("button action text must be a string")
        if kind == ActionKind.KEY_COMBO and not keys:
            raise ValueError("key_combo action must contain at least one key")
        if kind != ActionKind.KEY_COMBO and keys:
            raise ValueError("non-key action must not contain keys")
        if kind in {
            ActionKind.TYPE_TEXT,
            ActionKind.TYPE_TEXT_AND_SUBMIT,
        }:
            text = normalize_text_submit(raw_text)
        else:
            if raw_text:
                raise ValueError("non-text action must not contain text")
            text = ""
        return cls(kind=kind, keys=keys, text=text)


def button_action_for(
    bindings: Dict[str, object], button_id: str, trigger: ButtonTrigger
) -> ButtonAction:
    """Read one gesture action from the versioned bindings document.

    The original Windows build stored the single action directly under each
    button.  Secondary actions use the reference project's separate map so
    every existing ``key_bindings.json`` remains valid and keeps its primary
    mapping unchanged.
    """

    button_bindings = bindings.get("bindings", {})
    if not isinstance(button_bindings, dict):
        return ButtonAction(ActionKind.DISABLED)
    if trigger == ButtonTrigger.SINGLE_CLICK:
        raw = button_bindings.get(button_id)
    else:
        secondary = bindings.get("secondary_bindings", {})
        raw = (
            secondary.get(button_id, {}).get(trigger.value)
            if isinstance(secondary, dict)
            and isinstance(secondary.get(button_id, {}), dict)
            else None
        )
    if not isinstance(raw, dict):
        return ButtonAction(ActionKind.DISABLED)
    try:
        return ButtonAction.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return ButtonAction(ActionKind.DISABLED)


def has_secondary_action(bindings: Dict[str, object], button_id: str) -> bool:
    return any(
        button_action_for(bindings, button_id, trigger).kind != ActionKind.DISABLED
        for trigger in (ButtonTrigger.DOUBLE_CLICK, ButtonTrigger.LONG_PRESS)
    )


# Buttons that have a defined default action out of the box. "volume_mute" is
# deliberately absent (see module docstring).
DEFAULT_BUTTON_IDS = frozenset(
    {
        "mic",
        "power",
        "up",
        "down",
        "left",
        "right",
        "ok",
        "back",
        "volume_up",
        "volume_down",
        "home",
        "menu",
        "tv",
    }
)


def default_button_actions() -> Dict[str, ButtonAction]:
    return {
        "mic": ButtonAction(ActionKind.VOICE),
        "power": ButtonAction(ActionKind.ESCAPE),
        "up": ButtonAction(ActionKind.ARROW_UP),
        "down": ButtonAction(ActionKind.ARROW_DOWN),
        "left": ButtonAction(ActionKind.ARROW_LEFT),
        "right": ButtonAction(ActionKind.ARROW_RIGHT),
        "ok": ButtonAction(ActionKind.RETURN),
        "back": ButtonAction(ActionKind.DELETE_BACKWARD),
        "volume_up": ButtonAction(ActionKind.SYSTEM_VOLUME_UP),
        "volume_down": ButtonAction(ActionKind.SYSTEM_VOLUME_DOWN),
        "home": ButtonAction(ActionKind.SHOW_DESKTOP),
        "menu": ButtonAction(ActionKind.CONTEXT_MENU),
        "tv": ButtonAction(ActionKind.APP_SWITCHER),
    }
