import unittest

from ovb_rc003 import autostart_windows


class _FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.closed = []

    def OpenKey(self, root, path, reserved, access):
        del root, reserved, access
        if path != autostart_windows.RUN_KEY:
            raise FileNotFoundError(path)
        return path

    def CreateKeyEx(self, root, path, reserved, access):
        del root, reserved, access
        return path

    def QueryValueEx(self, key, name):
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, value_type, value):
        del key, reserved, value_type
        self.values[name] = value

    def DeleteValue(self, key, name):
        del key
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, key):
        self.closed.append(key)


class AutostartWindowsTests(unittest.TestCase):
    def test_command_starts_only_bridge_mode_and_quotes_executable(self):
        command = autostart_windows.build_autostart_command(
            frozen=True,
            executable=r"C:\Program Files\Remote Mic\RemoteMicRC003.exe",
        )

        self.assertEqual(
            command,
            r'"C:\Program Files\Remote Mic\RemoteMicRC003.exe" --bridge',
        )
        self.assertNotIn("--settings", command)

    def test_enable_and_disable_round_trip_the_current_user_value(self):
        registry = _FakeRegistry()
        executable = r"C:\RemoteMic\RemoteMicRC003.exe"

        self.assertFalse(
            autostart_windows.is_enabled(
                frozen=True, executable=executable, registry=registry
            )
        )
        autostart_windows.set_enabled(
            True, frozen=True, executable=executable, registry=registry
        )
        self.assertTrue(
            autostart_windows.is_enabled(
                frozen=True, executable=executable, registry=registry
            )
        )
        self.assertEqual(
            registry.values[autostart_windows.VALUE_NAME],
            r"C:\RemoteMic\RemoteMicRC003.exe --bridge",
        )

        autostart_windows.set_enabled(False, registry=registry)
        self.assertFalse(
            autostart_windows.is_enabled(
                frozen=True, executable=executable, registry=registry
            )
        )

    def test_different_build_path_is_not_reported_as_enabled(self):
        registry = _FakeRegistry()
        registry.values[autostart_windows.VALUE_NAME] = (
            r"C:\OldFolder\RemoteMicRC003.exe --bridge"
        )

        self.assertFalse(
            autostart_windows.is_enabled(
                frozen=True,
                executable=r"C:\NewFolder\RemoteMicRC003.exe",
                registry=registry,
            )
        )


if __name__ == "__main__":
    unittest.main()
