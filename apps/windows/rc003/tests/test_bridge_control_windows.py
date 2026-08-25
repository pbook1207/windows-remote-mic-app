import unittest

from ovb_rc003 import bridge_control_windows as control


class BridgeStopEventOwnerTests(unittest.TestCase):
    def test_owner_reports_unsignaled_then_signaled_and_closes(self):
        waits = iter([control._WAIT_TIMEOUT, control._WAIT_OBJECT_0])
        closed = []
        owner = control.BridgeStopEventOwner(
            _create_event=lambda name: control.EventCreationResult(41, 0),
            _wait_event=lambda handle, timeout: next(waits),
            _close_handle=lambda handle: closed.append(handle) or True,
        )

        with owner:
            self.assertFalse(owner.stop_requested())
            self.assertTrue(owner.stop_requested())

        self.assertEqual(closed, [41])

    def test_owner_rejects_an_existing_event(self):
        closed = []
        owner = control.BridgeStopEventOwner(
            _create_event=lambda name: control.EventCreationResult(
                52, control._ERROR_ALREADY_EXISTS
            ),
            _close_handle=lambda handle: closed.append(handle) or True,
        )

        with self.assertRaises(control.BridgeControlUnavailableError):
            owner.__enter__()
        self.assertEqual(closed, [52])


class BridgeControlClientTests(unittest.TestCase):
    def test_status_false_when_named_event_is_absent(self):
        self.assertFalse(
            control.is_bridge_running(
                _open_event=lambda access, name: control.EventOpenResult(
                    0, control._ERROR_FILE_NOT_FOUND
                )
            )
        )

    def test_status_true_closes_the_opened_handle(self):
        closed = []
        self.assertTrue(
            control.is_bridge_running(
                _open_event=lambda access, name: control.EventOpenResult(63, 0),
                _close_handle=lambda handle: closed.append(handle) or True,
            )
        )
        self.assertEqual(closed, [63])

    def test_stop_signals_and_closes_the_opened_handle(self):
        signaled = []
        closed = []
        self.assertTrue(
            control.request_bridge_stop(
                _open_event=lambda access, name: control.EventOpenResult(74, 0),
                _set_event=lambda handle: signaled.append(handle) or True,
                _close_handle=lambda handle: closed.append(handle) or True,
            )
        )
        self.assertEqual(signaled, [74])
        self.assertEqual(closed, [74])

    def test_stop_returns_false_when_no_bridge_exists(self):
        self.assertFalse(
            control.request_bridge_stop(
                _open_event=lambda access, name: control.EventOpenResult(
                    0, control._ERROR_FILE_NOT_FOUND
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
