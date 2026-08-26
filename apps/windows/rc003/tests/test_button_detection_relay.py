import threading
import time
import unittest

from ovb_rc003 import button_detection_relay


class _SharedBytes:
    def __init__(self, size):
        self.data = bytearray(size)
        self.lock = threading.Lock()

    def open(self):
        return _FakeMapping(self)


class _FakeMapping:
    def __init__(self, shared):
        self._shared = shared
        self._position = 0
        self._closed = False

    def seek(self, offset):
        self._position = offset

    def read(self, length):
        with self._shared.lock:
            result = bytes(
                self._shared.data[self._position : self._position + length]
            )
        self._position += len(result)
        return result

    def write(self, data):
        with self._shared.lock:
            self._shared.data[self._position : self._position + len(data)] = data
        self._position += len(data)
        return len(data)

    def close(self):
        self._closed = True


class ButtonDetectionRelayTests(unittest.TestCase):
    def _components(self):
        shared = _SharedBytes(button_detection_relay._MAPPING_SIZE)
        publisher = button_detection_relay.ButtonDetectionPublisher(
            _open_mapping=shared.open
        )
        return shared, publisher

    def test_listener_receives_ordered_press_and_release_edges(self):
        shared, publisher = self._components()
        received = []
        ready = threading.Event()

        def callback(button_id, is_pressed):
            received.append((button_id, is_pressed))
            if len(received) == 3:
                ready.set()

        listener = button_detection_relay.ButtonDetectionListener(
            callback,
            _open_mapping=shared.open,
            poll_interval_seconds=0.001,
        )
        listener.start()
        try:
            self.assertTrue(publisher.publish("volume_up", True))
            self.assertTrue(publisher.publish("volume_up", False))
            self.assertTrue(publisher.publish("back", True))
            self.assertTrue(ready.wait(2.0))
            self.assertEqual(
                received,
                [("volume_up", True), ("volume_up", False), ("back", True)],
            )
        finally:
            listener.stop()
            publisher.close()

    def test_listener_ignores_edges_written_before_detection_started(self):
        shared, publisher = self._components()
        self.assertTrue(publisher.publish("left", True))
        received = []
        listener = button_detection_relay.ButtonDetectionListener(
            lambda button_id, pressed: received.append((button_id, pressed)),
            _open_mapping=shared.open,
            poll_interval_seconds=0.001,
        )
        listener.start()
        try:
            time.sleep(0.02)
            self.assertEqual(received, [])
            self.assertTrue(publisher.publish("right", True))
            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(received, [("right", True)])
        finally:
            listener.stop()
            publisher.close()

    def test_unknown_button_is_not_published(self):
        _shared, publisher = self._components()
        try:
            self.assertFalse(publisher.publish("not-an-rc003-button", True))
        finally:
            publisher.close()


if __name__ == "__main__":
    unittest.main()
