"""Per-user Windows logon startup for the Remote Mic bridge.

The setting is opt-in and writes only the current user's standard Startup
Apps registry value.  It launches bridge mode directly, never the settings
window, and therefore needs no administrator permission.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from . import bridge_launcher


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RemoteMicRC003"


class AutostartUnavailableError(RuntimeError):
    """Raised when the current platform cannot manage Windows autostart."""


def build_autostart_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> str:
    """Return a correctly quoted command that starts bridge mode only."""

    return subprocess.list2cmdline(
        bridge_launcher.build_launch_command(
            frozen=frozen,
            executable=executable,
        )
    )


def _registry_module(registry=None):
    if registry is not None:
        return registry
    if sys.platform != "win32":
        raise AutostartUnavailableError("登录自启动仅支持 Windows。")
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - defensive on Windows
        raise AutostartUnavailableError("Windows 启动应用接口不可用。") from exc
    return winreg


def registered_command(*, registry=None) -> Optional[str]:
    """Read this app's current-user Startup Apps command, if present."""

    winreg = _registry_module(registry)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_QUERY_VALUE,
        )
    except OSError:
        return None
    try:
        value, _value_type = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)
    return value if isinstance(value, str) else None


def is_enabled(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    registry=None,
) -> bool:
    """Return true only when Startup Apps points at this exact build."""

    current = registered_command(registry=registry)
    if current is None:
        return False
    expected = build_autostart_command(frozen=frozen, executable=executable)
    return current.strip().casefold() == expected.strip().casefold()


def set_enabled(
    enabled: bool,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    registry=None,
) -> None:
    """Create or remove this user's Startup Apps entry immediately."""

    winreg = _registry_module(registry)
    if enabled:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.SetValueEx(
                key,
                VALUE_NAME,
                0,
                winreg.REG_SZ,
                build_autostart_command(frozen=frozen, executable=executable),
            )
        finally:
            winreg.CloseKey(key)
        return

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
    except OSError:
        return
    try:
        try:
            winreg.DeleteValue(key, VALUE_NAME)
        except OSError:
            pass
    finally:
        winreg.CloseKey(key)
