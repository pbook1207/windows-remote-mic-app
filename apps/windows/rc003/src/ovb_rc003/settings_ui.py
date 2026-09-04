"""Settings-window pure logic: button mapping list, voice hotkey, output
endpoint, bridge-launch and log-location status text.

This module is deliberately Tk/Qt-free (XRBM-030 replaced the previous Tk
view with a PySide6-Essentials + Qt Quick/QML one - see
``qt_settings_app.py`` and ``qml/`` - but every piece of validation/save/
launch/log-status logic below is unchanged and stays here so it keeps being
directly unit-testable without constructing any window at all, matching the
contract fixed after XRBM-014 review RETRY P1 #7): every piece of
validation/save logic is a plain function (``_action_to_display``,
``_display_to_action``, ``build_save_model``, ``_endpoint_display``,
``_parse_endpoint_display``, ``describe_launch_result``,
``describe_log_open_result``) that tests call directly - see
tests/test_settings_ui_helpers.py. The previous bug (the default "mic"
mapping's display string had no reverse mapping back to
``ActionKind.VOICE``, so a user who changed nothing - or clicked "restore
defaults" - could not save) is covered by an explicit round-trip test on
``_VOICE_DISPLAY``.

``main()`` at the bottom of this module is the only place that touches Qt at
all, and does so via a lazy import inside the function body - importing this
module (e.g. from ``__main__.py``'s ``--dry-run`` smoke check) never
requires PySide6 to be installed, the same optional-dependency convention
this package already uses for ``sounddevice``/``numpy``/``winrt`` (see
``qt_settings_app.py``'s module docstring for the exact error raised when
Qt is missing).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from . import (
    audio_output,
    bridge_launcher,
    device_catalog,
    device_profile,
    hotkey,
    key_mapping,
    logging_setup,
    microphone_auto_select,
    win32_keys,
)

# Reference action names map to semantic values.  The Windows implementation
# is intentionally behind these values; the dropdown must never turn
# ``方向上`` back into a generic ``key_combo`` just because the platform uses
# a key event to deliver it.
_REFERENCE_ACTION_LABELS: Dict[key_mapping.ActionKind, str] = {
    key_mapping.ActionKind.ESCAPE: "Escape",
    key_mapping.ActionKind.RETURN: "Return",
    key_mapping.ActionKind.ARROW_UP: "方向上",
    key_mapping.ActionKind.ARROW_DOWN: "方向下",
    key_mapping.ActionKind.ARROW_LEFT: "方向左",
    key_mapping.ActionKind.ARROW_RIGHT: "方向右",
    key_mapping.ActionKind.DELETE_BACKWARD: "Delete（退格）",
    key_mapping.ActionKind.SHOW_DESKTOP: "显示桌面",
    key_mapping.ActionKind.CONTEXT_MENU: "上下文菜单",
    key_mapping.ActionKind.APP_SWITCHER: "应用切换",
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: "系统音量 +",
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: "系统音量 −",
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: "系统静音",
    key_mapping.ActionKind.PLAY_PAUSE: "播放 / 暂停",
    key_mapping.ActionKind.SCROLL_UP: "鼠标所在区域向上滚动",
    key_mapping.ActionKind.SCROLL_DOWN: "鼠标所在区域向下滚动",
    key_mapping.ActionKind.PAGE_UP: "当前页面向上翻页",
    key_mapping.ActionKind.PAGE_DOWN: "当前页面向下翻页",
    key_mapping.ActionKind.VIRTUAL_DESKTOP_LEFT: "切换到左侧虚拟桌面",
    key_mapping.ActionKind.VIRTUAL_DESKTOP_RIGHT: "切换到右侧虚拟桌面",
    key_mapping.ActionKind.TASK_VIEW: "打开任务视图",
    key_mapping.ActionKind.CLIPBOARD_HISTORY: "打开剪贴板历史",
    key_mapping.ActionKind.PREVIOUS_TAB: "上一个标签页",
    key_mapping.ActionKind.NEXT_TAB: "下一个标签页",
    key_mapping.ActionKind.BROWSER_BACK: "后退",
    key_mapping.ActionKind.BROWSER_FORWARD: "前进",
    key_mapping.ActionKind.SNAP_WINDOW_LEFT: "窗口贴靠左侧",
    key_mapping.ActionKind.SNAP_WINDOW_RIGHT: "窗口贴靠右侧",
    key_mapping.ActionKind.MAXIMIZE_WINDOW: "窗口最大化",
    key_mapping.ActionKind.RESTORE_MINIMIZE_WINDOW: "窗口还原 / 最小化",
    key_mapping.ActionKind.OPEN_TEXT_MENU: "打开文本菜单",
    key_mapping.ActionKind.OPEN_REMOTE_MIC: "打开无线麦",
    key_mapping.ActionKind.OPEN_CODEX: "打开 Codex",
    key_mapping.ActionKind.OPEN_CLAUDE: "打开 Claude",
    key_mapping.ActionKind.OPEN_CMUX: "打开 cmux",
    key_mapping.ActionKind.OPEN_WECHAT: "打开微信",
    key_mapping.ActionKind.OPEN_CURSOR: "打开 Cursor",
    key_mapping.ActionKind.OPEN_SLACK: "打开 Slack",
    key_mapping.ActionKind.OPEN_WECOM: "打开企业微信",
    key_mapping.ActionKind.OPEN_NETEASE_MUSIC: "打开网易云音乐",
    key_mapping.ActionKind.OPEN_CHROME: "打开 Chrome",
    key_mapping.ActionKind.OPEN_EDGE: "打开 Edge",
    key_mapping.ActionKind.OPEN_ZED: "打开 Zed",
}
_REFERENCE_ACTION_KINDS_BY_LABEL: Dict[str, key_mapping.ActionKind] = {
    label: action_kind for action_kind, label in _REFERENCE_ACTION_LABELS.items()
}

# Preset choices shown in the mapping dropdown. Any other
# "mod+mod+key" text is still accepted as a custom shortcut through
# hotkey.HotkeySpec.parse.
_ACTION_CATEGORY_PREFIX = "── "
_PRESET_KEY_COMBOS = (
    "── 常用输入 ──",
    "Escape", "Return", "Delete（退格）", "方向上", "方向下", "方向左", "方向右",
    "输入文本…", "打开文本菜单",
    "── 滚动与导航 ──",
    "鼠标所在区域向上滚动", "鼠标所在区域向下滚动",
    "当前页面向上翻页", "当前页面向下翻页", "后退", "前进",
    "上一个标签页", "下一个标签页",
    "── Windows 工作区 ──",
    "切换到左侧虚拟桌面", "切换到右侧虚拟桌面", "打开任务视图",
    "打开剪贴板历史", "窗口贴靠左侧", "窗口贴靠右侧", "窗口最大化",
    "窗口还原 / 最小化", "显示桌面", "上下文菜单", "应用切换",
    "── 系统与媒体 ──",
    "系统音量 +", "系统音量 −", "系统静音", "播放 / 暂停",
    "── 应用 ──",
    "打开无线麦", "打开 Codex", "打开 Claude", "打开 cmux", "打开微信",
    "打开 Cursor", "打开 Slack", "打开企业微信", "打开网易云音乐",
    "打开 Chrome", "打开 Edge", "打开 Zed",
    "── 自定义快捷键 ──",
    "lctrl+win", "ralt", "ralt+space", "tab", "space", "f5", "禁用",
)

_TRIGGER_MODE_LABELS = {
    key_mapping.VoiceTriggerMode.TYPELESS: "Typeless 推荐（防抖优化）",
    key_mapping.VoiceTriggerMode.TOGGLE: "点按快捷键（开始、结束各一次）",
    key_mapping.VoiceTriggerMode.HOLD: "按住快捷键（说话期间保持按下）",
    key_mapping.VoiceTriggerMode.TYPELESS_START_ONLY: "Typeless 排障（只启动，不自动结束）",
}

def voice_hotkey_for_trigger_mode(trigger_mode: key_mapping.VoiceTriggerMode) -> str:
    """Return the physical host shortcut paired with a voice trigger mode."""

    return key_mapping.voice_hotkey_for_trigger_mode(trigger_mode)

# The exact display string for a "mic" (ActionKind.VOICE) button mapping.
# _display_to_action must recognize this literal string and round-trip it
# back to ActionKind.VOICE - it must NOT be handed to HotkeySpec.parse.
_VOICE_DISPLAY = "语音（使用专用组合键）"

_TEXT_SUBMIT_PRESET = "输入文本…"
_TEXT_SUBMIT_PREFIX = "输入文本："
_LEGACY_EXECUTE_SUBMIT_DISPLAY = "输入“执行”并回车"
_LEGACY_TEXT_SUBMIT_PRESET = "输入自定义文本并回车…"
_LEGACY_TEXT_SUBMIT_PREFIX = "输入文本并回车："

# Secondary gestures are optional.  Keep an explicit display value in the
# editable ComboBox so Qt does not fall back to the first real preset (usually
# ``escape``) when an older key_bindings.json has no secondary_bindings map.
SECONDARY_UNCONFIGURED_DISPLAY = "未设置"

# The microphone button remains a VOICE lifecycle action (it cannot be changed
# into an unrelated normal-key mapping), but the host chord it emits is
# editable through SettingsController.hotkeyText in the same row.
_MIC_ROW_DISPLAY = "触发语音（豆包或 Typeless 专用模式）"

# device_profile.ALL_BUTTON_IDS also carries "volume_mute", a HID usage-table
# entry kept for protocol compatibility (see key_mapping.py's module
# docstring) even though the physical RC003 has no dedicated mute key - only
# Volume + and Volume -. The settings window must not offer a mapping row a
# real remote can never actually trigger (XRBM-019 review round 1 P2), so
# every button list this module builds for display uses this narrowed set
# instead of ALL_BUTTON_IDS directly.
_USER_FACING_BUTTON_IDS = frozenset(device_profile.ALL_BUTTON_IDS - {"volume_mute"})

_ENDPOINT_NAME_HOST_API_SEPARATOR = " — "

_VIRTUAL_AUDIO_NAME_HINTS = (
    "cable input",
    "vb-audio",
    "voicemeeter",
    "virtual audio",
    "virtual cable",
    "steelseries sonar",
    "wave link",
    "blackhole",
    "nvidia broadcast",
)


def is_likely_virtual_audio_endpoint(endpoint: audio_output.AudioEndpoint) -> bool:
    """Best-effort presentation filter; never an authorization boundary.

    Windows/PortAudio does not expose a reliable universal ``is_virtual``
    flag.  Known virtual-card names get the compact default treatment, while
    the UI's explicit "show all" control remains the lossless escape hatch for
    an unknown or newly released driver.
    """

    normalized = endpoint.name.casefold()
    return audio_output.is_cable_input_endpoint(endpoint.name) or any(
        hint in normalized for hint in _VIRTUAL_AUDIO_NAME_HINTS
    )


def compact_bridge_endpoint_options(
    endpoints: Sequence[audio_output.AudioEndpoint],
    *,
    current_display: str = "",
) -> List[str]:
    """Return one preferred interface per detected virtual playback device."""

    grouped: Dict[str, List[audio_output.AudioEndpoint]] = {}
    for endpoint in endpoints:
        if is_likely_virtual_audio_endpoint(endpoint):
            grouped.setdefault(endpoint.name.casefold(), []).append(endpoint)

    selected = []
    for candidates in grouped.values():
        preferred = min(
            candidates,
            key=lambda endpoint: (
                0 if "wasapi" in endpoint.host_api.casefold() else 1,
                endpoint.host_api.casefold(),
            ),
        )
        selected.append(preferred)
    selected.sort(
        key=lambda endpoint: (
            0 if audio_output.is_cable_input_endpoint(endpoint.name) else 1,
            endpoint.name.casefold(),
        )
    )
    options = [_endpoint_display(endpoint) for endpoint in selected]
    if current_display and current_display not in options:
        options.insert(0, current_display)
    return options


def compact_system_input_endpoint_options(
    endpoints: Sequence[audio_output.AudioEndpoint],
    *,
    current_display: str = "",
) -> list[str]:
    """Return one recommended Windows interface per microphone device.

    PortAudio commonly reports the same recording device through Windows
    WASAPI, DirectSound, MME and WDM-KS.  Showing every backend makes the
    microphone picker look like it contains many more physical devices than
    it really does.  The compact list keeps one entry per endpoint name,
    preferring WASAPI, while preserving the user's current exact selection.

    This is presentation only: it is not a fixed microphone priority and the
    explicit "show all" UI remains the lossless escape hatch.
    """

    selected = microphone_auto_select.recommended_candidates(endpoints)
    current_name, current_host_api = _parse_endpoint_display(current_display)
    current_endpoint = next(
        (
            endpoint
            for endpoint in endpoints
            if endpoint.name == current_name
            and endpoint.host_api == current_host_api
        ),
        None,
    )
    if current_endpoint is not None:
        selected = [
            current_endpoint
            if endpoint.name.casefold() == current_endpoint.name.casefold()
            else endpoint
            for endpoint in selected
        ]
    options = [_endpoint_display(endpoint) for endpoint in selected]
    if current_display and current_display not in options:
        options.insert(0, current_display)
    return options


def bridge_endpoint_help(display_text: str) -> str:
    """User-facing pairing guidance for the selected playback endpoint."""

    name, _host_api = _parse_endpoint_display(display_text)
    if not name:
        return "请选择一个虚拟声卡的播放端点。"
    if audio_output.is_cable_input_endpoint(name):
        return (
            "Remote Mic 把声音桥接到 CABLE Input；Codex、Typeless 等软件的"
            "麦克风请选择 CABLE Output。"
        )
    return (
        "Remote Mic 将声音桥接到所选设备；请在 Codex、Typeless 等软件中选择"
        "该虚拟声卡对应的麦克风端点。其他虚拟声卡暂不支持空闲麦克风检测。"
    )


class SettingsValidationError(Exception):
    """Raised by build_save_model on invalid input. ``button_id`` is None
    for a hotkey-level error, or the offending button's id for a mapping
    error.
    """

    def __init__(self, button_id: Optional[str], message: str) -> None:
        super().__init__(message)
        self.button_id = button_id
        self.message = message


def _action_to_display(action: key_mapping.ButtonAction) -> str:
    if action.kind == key_mapping.ActionKind.DISABLED:
        return "禁用"
    if action.kind == key_mapping.ActionKind.VOICE:
        return _VOICE_DISPLAY
    if action.kind == key_mapping.ActionKind.TYPE_TEXT:
        return _TEXT_SUBMIT_PREFIX + action.text
    if action.kind == key_mapping.ActionKind.TYPE_TEXT_AND_SUBMIT:
        return _TEXT_SUBMIT_PREFIX + action.text
    if action.kind == key_mapping.ActionKind.TYPE_EXECUTE_AND_SUBMIT:
        return _TEXT_SUBMIT_PREFIX + "执行"
    reference_label = _REFERENCE_ACTION_LABELS.get(action.kind)
    if reference_label is not None:
        return reference_label
    # Make old configs readable even before the loader has had a chance to
    # migrate them (e.g. a caller is rendering a raw document in a test).
    legacy_action = key_mapping.semantic_action_for_keys(action.keys)
    if legacy_action is not None:
        return _REFERENCE_ACTION_LABELS[legacy_action.kind]
    return "+".join(action.keys)


def _display_to_action(text: str) -> key_mapping.ButtonAction:
    text = text.strip()
    if text.startswith(_ACTION_CATEGORY_PREFIX):
        raise hotkey.HotkeyParseError("请选择分类下的具体动作")
    if text in ("禁用", "disabled", SECONDARY_UNCONFIGURED_DISPLAY):
        return key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
    if text == _VOICE_DISPLAY:
        return key_mapping.ButtonAction(key_mapping.ActionKind.VOICE)
    if text in (
        _TEXT_SUBMIT_PRESET,
        _LEGACY_TEXT_SUBMIT_PRESET,
        _LEGACY_EXECUTE_SUBMIT_DISPLAY,
    ):
        return key_mapping.ButtonAction(
            key_mapping.ActionKind.TYPE_TEXT,
            text="执行",
        )
    matching_prefix = next(
        (
            prefix
            for prefix in (_TEXT_SUBMIT_PREFIX, _LEGACY_TEXT_SUBMIT_PREFIX)
            if text.startswith(prefix)
        ),
        None,
    )
    if matching_prefix is not None:
        try:
            payload = key_mapping.normalize_text_submit(
                text[len(matching_prefix) :]
            )
        except ValueError as exc:
            raise hotkey.HotkeyParseError(str(exc)) from exc
        return key_mapping.ButtonAction(
            key_mapping.ActionKind.TYPE_TEXT,
            text=payload,
        )
    if text == "系统音量 -":
        text = "系统音量 −"
    reference_kind = _REFERENCE_ACTION_KINDS_BY_LABEL.get(text)
    if reference_kind is not None:
        return key_mapping.ButtonAction(reference_kind)
    # Keep the previous spelling accepted for users who copied the macOS
    # reference label into the Windows field.
    if text == "Command-Tab":
        return key_mapping.ButtonAction(key_mapping.ActionKind.APP_SWITCHER)
    parsed = hotkey.HotkeySpec.parse(text)
    try:
        win32_keys.resolve_vk_codes(tuple(parsed.modifiers) + (parsed.key,))
    except win32_keys.UnknownKeyTokenError as exc:
        raise hotkey.HotkeyParseError(str(exc)) from exc
    return key_mapping.ButtonAction(
        key_mapping.ActionKind.KEY_COMBO, tuple(parsed.modifiers) + (parsed.key,)
    )


def _endpoint_display(endpoint: audio_output.AudioEndpoint) -> str:
    if endpoint.host_api:
        return f"{endpoint.name}{_ENDPOINT_NAME_HOST_API_SEPARATOR}{endpoint.host_api}"
    return endpoint.name


def _parse_endpoint_display(text: str) -> Tuple[str, str]:
    """Inverse of _endpoint_display: returns (name, host_api), where
    host_api is "" if the text has no disambiguating suffix.
    """

    text = text.strip()
    if _ENDPOINT_NAME_HOST_API_SEPARATOR in text:
        name, host_api = text.rsplit(_ENDPOINT_NAME_HOST_API_SEPARATOR, 1)
        return name.strip(), host_api.strip()
    return text, ""


def build_save_model(
    *,
    button_display_map: Dict[str, str],
    secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
    hotkey_text: str,
    trigger_mode: key_mapping.VoiceTriggerMode,
    endpoint_display_text: str,
    base_config: dict,
    base_bindings: dict,
    selected_device_profile: str = device_catalog.RC003_ID,
    text_menu_items: Optional[Sequence[dict]] = None,
    unified_virtual_input_enabled: Optional[bool] = None,
    unified_on_demand_enabled: Optional[bool] = None,
    system_input_endpoint_display_text: Optional[str] = None,
    system_input_auto_select_enabled: Optional[bool] = None,
    system_input_auto_gain_enabled: Optional[bool] = None,
    system_input_candidate_display_texts: Optional[Sequence[str]] = None,
    local_system_input_endpoint_display_text: Optional[str] = None,
    remote_system_input_endpoint_display_text: Optional[str] = None,
    secondary_hotkey_text: Optional[str] = None,
    secondary_gesture_enabled: Optional[bool] = None,
) -> Tuple[dict, dict]:
    """Pure validation+build step for "Save"/"Restore defaults", with no Tk
    dependency at all - directly unit tested without constructing any
    window (see tests/test_settings_ui_helpers.py). Raises
    SettingsValidationError on invalid input; never raises a Tk exception.
    """

    try:
        parsed_hotkey = hotkey.HotkeySpec.parse(hotkey_text)
        win32_keys.resolve_vk_codes(tuple(parsed_hotkey.modifiers) + (parsed_hotkey.key,))
    except hotkey.HotkeyParseError as exc:
        raise SettingsValidationError(None, str(exc)) from exc
    except win32_keys.UnknownKeyTokenError as exc:
        raise SettingsValidationError(None, str(exc)) from exc

    resolved_secondary_hotkey = (
        str(base_config.get("voice_secondary_hotkey", ""))
        if secondary_hotkey_text is None
        else secondary_hotkey_text.strip()
    )
    if resolved_secondary_hotkey:
        try:
            parsed_secondary = hotkey.HotkeySpec.parse(resolved_secondary_hotkey)
            win32_keys.resolve_vk_codes(
                tuple(parsed_secondary.modifiers) + (parsed_secondary.key,)
            )
        except hotkey.HotkeyParseError as exc:
            raise SettingsValidationError(None, f"第二语音快捷键：{exc}") from exc
        except win32_keys.UnknownKeyTokenError as exc:
            raise SettingsValidationError(None, f"第二语音快捷键：{exc}") from exc
    requested_secondary_gesture = (
        bool(base_config.get("voice_secondary_gesture_enabled", False))
        if secondary_gesture_enabled is None
        else bool(secondary_gesture_enabled)
    )
    if requested_secondary_gesture and not resolved_secondary_hotkey:
        raise SettingsValidationError(None, "启用第二按键方式前，请填写第二语音快捷键。")

    bindings: Dict[str, dict] = {}
    for button_id, text in button_display_map.items():
        text = text.strip()
        if not text:
            continue
        try:
            action = _display_to_action(text)
        except hotkey.HotkeyParseError as exc:
            raise SettingsValidationError(button_id, str(exc)) from exc
        bindings[button_id] = action.to_dict()

    if secondary_display_map is None:
        raw_secondary = base_bindings.get("secondary_bindings", {})
        secondary_bindings = (
            copy.deepcopy(raw_secondary) if isinstance(raw_secondary, dict) else {}
        )
    else:
        secondary_bindings: Dict[str, Dict[str, dict]] = {}
        valid_triggers = {
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
            key_mapping.ButtonTrigger.LONG_PRESS.value,
        }
        for button_id, trigger_map in secondary_display_map.items():
            if button_id == "mic" or not isinstance(trigger_map, dict):
                continue
            for trigger_name, text in trigger_map.items():
                if trigger_name not in valid_triggers:
                    raise SettingsValidationError(
                        button_id, f"未知手势：{trigger_name}"
                    )
                text = str(text).strip()
                if not text or text in (
                    "禁用",
                    "disabled",
                    SECONDARY_UNCONFIGURED_DISPLAY,
                ):
                    continue
                try:
                    action = _display_to_action(text)
                except hotkey.HotkeyParseError as exc:
                    raise SettingsValidationError(button_id, str(exc)) from exc
                if action.kind == key_mapping.ActionKind.DISABLED:
                    continue
                secondary_bindings.setdefault(button_id, {})[trigger_name] = action.to_dict()

    # The physical mic button is always driven directly by the ATVV voice
    # lifecycle (see app.py) - the runtime never consults a stored "mic"
    # binding at all. Force it to VOICE unconditionally regardless of what
    # button_display_map contained: the settings window no longer offers an
    # editable mic row (see ButtonMappingModel in qt_settings_app.py), but
    # this is the authoritative, UI-independent guarantee that this save
    # path can never
    # persist a stale/misleading non-voice mic action (XRBM-019 In-scope
    # item 6, folded in from XRBM-018's independent review round 2's
    # product-contract follow-up).
    bindings["mic"] = key_mapping.ButtonAction(key_mapping.ActionKind.VOICE).to_dict()

    endpoint_name, endpoint_host_api = _parse_endpoint_display(endpoint_display_text)
    unified_enabled = (
        bool(base_config.get("unified_virtual_input_enabled", False))
        if unified_virtual_input_enabled is None
        else bool(unified_virtual_input_enabled)
    )
    on_demand_enabled = (
        bool(base_config.get("unified_on_demand_enabled", True))
        if unified_on_demand_enabled is None
        else bool(unified_on_demand_enabled)
    )
    if system_input_endpoint_display_text is None:
        system_input_name = str(base_config.get("system_input_endpoint_name", ""))
        system_input_host_api = str(
            base_config.get("system_input_endpoint_host_api", "")
        )
    else:
        system_input_name, system_input_host_api = _parse_endpoint_display(
            system_input_endpoint_display_text
        )
    auto_select_enabled = (
        bool(base_config.get("system_input_auto_select_enabled", False))
        if system_input_auto_select_enabled is None
        else bool(system_input_auto_select_enabled)
    )
    auto_gain_enabled = (
        bool(base_config.get("system_input_auto_gain_enabled", True))
        if system_input_auto_gain_enabled is None
        else bool(system_input_auto_gain_enabled)
    )

    def _source_endpoint(display_text: Optional[str], prefix: str):
        if display_text is None:
            return (
                str(base_config.get(f"{prefix}_system_input_endpoint_name", "")),
                str(base_config.get(f"{prefix}_system_input_endpoint_host_api", "")),
            )
        return _parse_endpoint_display(display_text)

    local_input_name, local_input_host_api = _source_endpoint(
        local_system_input_endpoint_display_text, "local"
    )
    remote_input_name, remote_input_host_api = _source_endpoint(
        remote_system_input_endpoint_display_text, "remote"
    )
    if system_input_candidate_display_texts is None:
        candidate_endpoints = microphone_auto_select.normalize_configured_candidates(
            base_config.get("system_input_candidate_endpoints", [])
        )
    else:
        parsed_candidates = [
            _parse_endpoint_display(str(display))
            for display in system_input_candidate_display_texts
        ]
        candidate_endpoints = microphone_auto_select.recommended_candidates(
            audio_output.AudioEndpoint(name, host_api)
            for name, host_api in parsed_candidates
            if name
        )
    if system_input_name and not any(
        endpoint.name == system_input_name
        and endpoint.host_api == system_input_host_api
        for endpoint in candidate_endpoints
    ):
        candidate_endpoints.insert(
            0, audio_output.AudioEndpoint(system_input_name, system_input_host_api)
        )
    if unified_enabled:
        if not endpoint_name:
            raise SettingsValidationError(None, "请先选择要桥接到的虚拟音频设备。")
        if not system_input_name:
            raise SettingsValidationError(None, "请先选择要透明转发的系统麦克风。")
        if audio_output.is_cable_output_endpoint(system_input_name):
            raise SettingsValidationError(
                None, "系统麦克风不能选择 CABLE Output，否则会形成音频回路。"
            )
        if auto_select_enabled and not candidate_endpoints:
            raise SettingsValidationError(
                None, "没有找到可用于自动选择的系统麦克风，请刷新设备后重试。"
            )

    new_config = dict(base_config)
    new_config["selected_device_profile"] = device_catalog.normalize_device_id(
        selected_device_profile
    )
    new_config["voice_hotkey"] = hotkey_text.strip()
    new_config["voice_secondary_hotkey"] = resolved_secondary_hotkey
    new_config["voice_secondary_gesture_enabled"] = requested_secondary_gesture
    new_config["voice_trigger_mode"] = trigger_mode.value
    new_config["output_endpoint_name"] = endpoint_name
    new_config["output_endpoint_host_api"] = endpoint_host_api
    new_config["unified_virtual_input_enabled"] = unified_enabled
    # The current Core Audio consumer detector can identify the paired
    # CABLE Output session for VB-CABLE.  Unknown virtual cards expose no
    # universal playback->recording endpoint relationship, so they use the
    # established continuous-forwarding behavior instead of polling an
    # unrelated CABLE Output and accidentally muting the selected route.
    new_config["unified_on_demand_enabled"] = bool(
        on_demand_enabled and audio_output.is_cable_input_endpoint(endpoint_name)
    )
    new_config["system_input_endpoint_name"] = system_input_name
    new_config["system_input_endpoint_host_api"] = system_input_host_api
    new_config["system_input_auto_select_enabled"] = auto_select_enabled
    new_config["system_input_auto_gain_enabled"] = auto_gain_enabled
    new_config["system_input_candidate_endpoints"] = (
        microphone_auto_select.serialize_candidates(candidate_endpoints)
    )
    new_config["local_system_input_endpoint_name"] = local_input_name
    new_config["local_system_input_endpoint_host_api"] = local_input_host_api
    new_config["remote_system_input_endpoint_name"] = remote_input_name
    new_config["remote_system_input_endpoint_host_api"] = remote_input_host_api

    new_bindings = dict(base_bindings)
    new_bindings["bindings"] = bindings
    new_bindings["secondary_bindings"] = secondary_bindings
    raw_text_menu_items = (
        base_bindings.get("text_menu_items", [])
        if text_menu_items is None
        else text_menu_items
    )
    try:
        normalized_text_menu_items = key_mapping.normalize_text_menu_items(
            raw_text_menu_items
        )
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(None, f"文本菜单无效：{exc}") from exc
    new_bindings["text_menu_items"] = [
        item.to_dict() for item in normalized_text_menu_items
    ]

    return new_config, new_bindings


@dataclass(frozen=True)
class DefaultDisplayState:
    """What "restore defaults" resets every widget to - a pure snapshot, so
    it can be asserted on directly in tests without touching a StringVar.
    """

    button_display_map: Dict[str, str]
    secondary_display_map: Dict[str, Dict[str, str]]
    hotkey_text: str
    trigger_mode_label: str


def default_display_state() -> DefaultDisplayState:
    defaults = key_mapping.default_button_actions()
    button_display_map = {
        button_id: _action_to_display(action) for button_id, action in defaults.items()
    }
    for button_id in _USER_FACING_BUTTON_IDS:
        button_display_map.setdefault(button_id, "")
    secondary_display_map = {
        button_id: {
            key_mapping.ButtonTrigger.DOUBLE_CLICK.value: "",
            key_mapping.ButtonTrigger.LONG_PRESS.value: "",
        }
        for button_id in _USER_FACING_BUTTON_IDS
        if button_id != "mic"
    }
    return DefaultDisplayState(
        button_display_map=button_display_map,
        secondary_display_map=secondary_display_map,
        hotkey_text=hotkey.DEFAULT_VOICE_HOTKEY.serialize(),
        trigger_mode_label=_TRIGGER_MODE_LABELS[key_mapping.VoiceTriggerMode.TOGGLE],
    )


# Bridge-control status text (XRBM-029). Kept as pure, Tk-free functions -
# same testability contract as the save-model helpers above (see
# tests/test_settings_ui_helpers.py) - so every one of the four required
# stable states (not-started / running / already-running / abnormal-quick-
# exit) is asserted on directly without constructing a window or a real
# subprocess.
#
# Wording contract: a STARTED result is deliberately never described as
# "RC003 已连接"/"RC003 connected" - only as the process itself still being
# alive. Whether the bridge actually reached a working BLE/HID/audio
# connection is only observable from app.log, which every branch below
# points the user at.
LAUNCH_NOT_STARTED_TEXT = "未启动（本次设置窗口打开后还没有尝试启动桥接）"


def describe_launch_result(result: bridge_launcher.LaunchResult) -> str:
    if result.outcome is bridge_launcher.LaunchOutcome.STARTED:
        pid_text = f"（PID {result.pid}）" if result.pid is not None else ""
        return (
            f"已启动桥接进程{pid_text}，目前仍在运行。这只说明进程本身存活，"
            "不代表已经与 RC003 建立连接——请用下方“打开日志目录”查看 app.log "
            "确认实际连接、按键与语音状态。"
        )
    if result.outcome is bridge_launcher.LaunchOutcome.ALREADY_RUNNING:
        return (
            "已经在运行：这次启动被单实例保护拒绝，进程立即退出（退出码 "
            f"{result.exit_code}）。设置页会自动刷新桥接状态；请等待按钮显示为"
            "“保存并重启桥接”后重试。"
        )
    if result.outcome is bridge_launcher.LaunchOutcome.QUICK_EXIT:
        return (
            f"启动异常：进程在短时间内退出（退出码 {result.exit_code}），可能没有成功"
            "建立 BLE/HID/音频连接。请用下方“打开日志目录”查看 app.log 了解具体原因。"
        )
    # LAUNCH_FAILED
    return f"启动或重启失败（{result.error}）。请用下方“打开日志目录”查看 app.log。"


def describe_log_open_result(result: logging_setup.LogOpenResult) -> str:
    if result.outcome is logging_setup.LogOpenOutcome.OPENED:
        note = ""
        if result.location.status is logging_setup.LogLocationStatus.FILE_MISSING:
            note = "（该目录存在，但 app.log 尚不存在——桥接可能还没有运行过一次，这不是错误。）"
        return f"已打开日志目录：{result.location.directory}{note}"
    if result.outcome is logging_setup.LogOpenOutcome.DIRECTORY_MISSING:
        return (
            f"日志目录尚不存在：{result.location.directory}。这通常表示桥接程序在这台"
            "电脑上还没有运行过；本程序不会为了显示而伪造日志。"
        )
    return f"无法打开日志目录（{result.error}）：{result.location.directory}"


def main() -> None:
    """Launches the Qt Quick/QML settings window (XRBM-030). Imports
    ``qt_settings_app`` lazily so importing THIS module (e.g. from
    ``__main__.py``'s ``--dry-run`` smoke check, or from any test that only
    needs the pure functions above) never requires PySide6 to be installed -
    see ``qt_settings_app.py``'s module docstring for the exact, clear error
    raised here if it is missing.
    """

    from . import qt_settings_app

    qt_settings_app.run_settings_window()


if __name__ == "__main__":
    main()
