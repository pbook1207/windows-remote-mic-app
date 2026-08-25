import unittest

from ovb_rc003 import audio_capture_activity_windows


class _Session:
    def __init__(self, state):
        self._state = state

    def GetState(self):
        return self._state


class _Enumerator:
    def __init__(self, states):
        self._sessions = [_Session(state) for state in states]

    def GetCount(self):
        return len(self._sessions)

    def GetSession(self, index):
        return self._sessions[index]


class _Manager:
    def __init__(self, states):
        self._states = states

    def GetSessionEnumerator(self):
        if isinstance(self._states, Exception):
            raise self._states
        return _Enumerator(self._states)


class _Device:
    def __init__(self, name, states):
        self.FriendlyName = name
        self.AudioSessionManager = _Manager(states)


class _AudioUtilities:
    devices = []

    @classmethod
    def GetAllDevices(cls, _flow, _state):
        return list(cls.devices)


class _EnumValue:
    value = 1


class _DataFlow:
    eCapture = _EnumValue()


class _DeviceState:
    ACTIVE = _EnumValue()


class CableCaptureActivityDetectorTests(unittest.TestCase):
    def _detector(self, devices):
        _AudioUtilities.devices = devices
        detector = audio_capture_activity_windows.CableCaptureActivityDetector()
        detector._audio_utilities = _AudioUtilities
        detector._data_flow = _DataFlow
        detector._device_state = _DeviceState
        return detector

    def test_active_session_on_cable_output_is_detected(self):
        detector = self._detector(
            [_Device("CABLE Output (VB-Audio Virtual Cable)", [0, 1])]
        )
        self.assertTrue(detector.is_active())

    def test_inactive_sessions_do_not_request_system_microphone_capture(self):
        detector = self._detector([_Device("CABLE Output", [0, 0])])
        self.assertFalse(detector.is_active())

    def test_missing_or_ambiguous_cable_output_fails_closed(self):
        for devices in (
            [],
            [_Device("CABLE Output", []), _Device("CABLE Output", [])],
        ):
            with self.subTest(count=len(devices)):
                detector = self._detector(devices)
                with self.assertRaises(
                    audio_capture_activity_windows.CaptureActivityUnavailableError
                ):
                    detector.is_active()

    def test_hot_unplug_failure_forces_device_resolution_on_next_poll(self):
        detector = self._detector([_Device("CABLE Output", RuntimeError("gone"))])
        with self.assertRaises(
            audio_capture_activity_windows.CaptureActivityUnavailableError
        ):
            detector.is_active()
        self.assertIsNone(detector._device)


if __name__ == "__main__":
    unittest.main()
