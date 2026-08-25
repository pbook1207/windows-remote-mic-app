import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from ovb_rc003 import app as app_module
from ovb_rc003 import audio_output, config, unified_audio_router
from ovb_rc003.atvv_session import PcmStats


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class FakeSink:
    instances = []

    def __init__(self, name, host_api):
        self.name = name
        self.host_api = host_api
        self.opened = False
        self.closed = False
        self.writes = []
        self.fades = 0
        self.resets = 0
        type(self).instances.append(self)

    def open(self):
        self.opened = True

    def write_pcm(self, samples, rate, channels):
        self.writes.append((np.asarray(samples).copy(), rate, channels))

    def write_fade_to_silence(self):
        self.fades += 1

    def reset_conversion(self):
        self.resets += 1

    def close(self):
        self.closed = True


class FakeInputStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.stopped = True
        self.active = False

    def close(self):
        self.closed = True

    def emit(self, values):
        array = np.asarray(values, dtype="int16").reshape(-1, 1)
        self.callback(array, len(array), None, None)


class FakeSoundDevice:
    def __init__(self):
        self.streams = []

    def query_hostapis(self):
        return [{"name": "Windows WASAPI"}]

    def query_devices(self):
        return [
            {
                "name": "Built-in Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            }
        ]

    def check_input_settings(self, **kwargs):
        return None

    def InputStream(self, **kwargs):
        stream = FakeInputStream(kwargs["callback"])
        self.streams.append(stream)
        return stream


class FakeActivityDetector:
    def __init__(self):
        self.active = False
        self.error = None
        self.closed = False

    def is_active(self):
        if self.error is not None:
            raise self.error
        return self.active

    def close(self):
        self.closed = True


class UnifiedAudioRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sd = FakeSoundDevice()
        FakeSink.instances = []
        self.output_endpoint = audio_output.AudioEndpoint(
            "CABLE Input", "Windows WASAPI"
        )
        self.input_endpoint = audio_output.AudioEndpoint(
            "Built-in Mic", "Windows WASAPI"
        )
        self.output_patch = mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[self.output_endpoint]
        )
        self.input_patch = mock.patch.object(
            audio_output, "enumerate_input_endpoints", return_value=[self.input_endpoint]
        )
        self.output_patch.start()
        self.input_patch.start()
        self.failures = []
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name="Built-in Mic",
            system_input_host_api="Windows WASAPI",
            logger=logging.getLogger("unified-audio-test"),
            on_remote_failure=lambda: self.failures.append(True),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
        )

    def tearDown(self):
        try:
            self.router.close()
        finally:
            self.input_patch.stop()
            self.output_patch.stop()
            self.temp.cleanup()

    def test_system_is_transparently_forwarded_then_remote_is_exclusive(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        first_input = self.sd.streams[0]
        sink = FakeSink.instances[0]

        first_input.emit([100, 200, 300])
        self.assertTrue(_wait_until(lambda: len(sink.writes) == 1))
        self.assertEqual(sink.writes[0][1:], (48000, 1))

        self.assertTrue(self.router.begin_remote())
        self.assertTrue(first_input.stopped)
        self.assertTrue(first_input.closed)
        # A callback already in flight after the switch cannot enter output.
        first_input.emit([999, 999])
        self.assertTrue(self.router.write_remote([10, 20, 30]))
        self.assertTrue(_wait_until(lambda: len(sink.writes) == 2))
        self.assertEqual(sink.writes[-1][1:], (16000, 1))
        self.assertEqual(len(sink.writes[-1][0]), 3)
        self.assertEqual(sink.writes[-1][0][-1], 30)

        self.router.end_remote()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        self.assertGreaterEqual(len(self.sd.streams), 2)

    def test_remote_cannot_start_until_virtual_output_is_ready(self):
        self.assertFalse(self.router.begin_remote())
        self.assertFalse(self.router.write_remote([1, 2]))
        self.assertEqual(self.failures, [])

    def test_custom_virtual_output_is_allowed_for_continuous_forwarding(self):
        self.router.close()
        custom_output = audio_output.AudioEndpoint(
            "VoiceMeeter Input", "Windows WASAPI"
        )
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name=custom_output.name,
            output_host_api=custom_output.host_api,
            system_input_name="Built-in Mic",
            system_input_host_api="Windows WASAPI",
            logger=logging.getLogger("unified-audio-custom-output-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
        )
        with mock.patch.object(
            audio_output,
            "enumerate_output_endpoints",
            return_value=[custom_output],
        ):
            self.router.start()
            self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        self.assertEqual(FakeSink.instances[-1].name, "VoiceMeeter Input")

    def test_remote_tail_blocks_are_drained_before_system_is_restored(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        sink = FakeSink.instances[0]
        self.assertTrue(self.router.begin_remote())
        for value in range(20):
            self.assertTrue(self.router.write_remote([value, value + 1]))
        self.router.end_remote()
        remote_writes = [write for write in sink.writes if write[1] == 16000]
        self.assertEqual(len(remote_writes), 20)
        self.assertFalse(self.router.remote_active)

    def test_stop_closes_capture_and_reports_no_collection(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        stream = self.sd.streams[0]
        self.router.close()
        self.assertTrue(stream.closed)
        self.assertEqual(self.router.status_code, "stopped")
        self.assertIn("不会采集系统麦克风", unified_audio_router.describe_status(self.root, False))

    def _replace_with_on_demand_router(self):
        self.router.close()
        self.detector = FakeActivityDetector()
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name="Built-in Mic",
            system_input_host_api="Windows WASAPI",
            logger=logging.getLogger("unified-audio-on-demand-test"),
            on_remote_failure=lambda: self.failures.append(True),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            on_demand_system_input=True,
            activity_detector_factory=lambda: self.detector,
            activity_poll_seconds=0.01,
            idle_release_seconds=0.05,
        )

    def test_on_demand_starts_idle_and_opens_only_for_an_active_consumer(self):
        self._replace_with_on_demand_router()
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))
        self.assertEqual(self.sd.streams, [])

        self.detector.active = True
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        self.assertEqual(len(self.sd.streams), 1)
        stream = self.sd.streams[0]

        self.detector.active = False
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)

    def test_on_demand_detector_failure_falls_back_to_continuous_forwarding(self):
        self._replace_with_on_demand_router()
        self.detector.error = RuntimeError("session API unavailable")
        self.router.start()
        self.assertTrue(
            _wait_until(
                lambda: self.router.status_code == "demand_detection_failed"
            )
        )
        self.assertEqual(len(self.sd.streams), 1)
        self.assertTrue(self.sd.streams[0].active)

    def test_remote_from_idle_never_opens_the_system_microphone(self):
        self._replace_with_on_demand_router()
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))
        self.assertTrue(self.router.begin_remote())
        self.assertEqual(self.router.status_code, "remote")
        self.assertTrue(self.router.write_remote([10, 20]))
        self.router.end_remote()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))
        self.assertEqual(self.sd.streams, [])


class UnifiedAudioConfigTests(unittest.TestCase):
    def test_feature_is_opt_in_and_has_no_saved_system_device_by_default(self):
        value = config.default_config()
        self.assertFalse(value["unified_virtual_input_enabled"])
        self.assertTrue(value["unified_on_demand_enabled"])
        self.assertEqual(value["system_input_endpoint_name"], "")
        self.assertEqual(value["system_input_endpoint_host_api"], "")


class _AppRouter:
    def __init__(self):
        self.begins = 0
        self.ends = 0
        self.blocks = []
        self.remote_active = False

    def begin_remote(self):
        self.begins += 1
        self.remote_active = True
        return True

    def write_remote(self, samples):
        self.blocks.append(list(samples))
        return self.remote_active

    def end_remote(self):
        self.ends += 1
        self.remote_active = False


class _Supervisor:
    def __init__(self):
        self.reconnects = 0

    def request_reconnect(self):
        self.reconnects += 1


class UnifiedAudioAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = app_module.RC003App.__new__(app_module.RC003App)
        self.app._unified_audio_router = _AppRouter()
        self.app._playback = None
        self.app._voice_pcm_stats = PcmStats()
        self.app._logger = logging.getLogger("unified-audio-app-test")
        self.app._supervisor = _Supervisor()

    def test_rc003_pcm_uses_router_instead_of_legacy_sink(self):
        self.assertTrue(self.app._open_playback_for_new_session())
        self.app._on_pcm_frame([1, 2, 3])
        self.assertEqual(self.app._unified_audio_router.begins, 1)
        self.assertEqual(self.app._unified_audio_router.blocks, [[1, 2, 3]])

    def test_disconnect_restores_system_source_before_reconnect(self):
        self.app._unified_audio_router.begin_remote()
        self.app._on_disconnected()
        self.assertFalse(self.app._unified_audio_router.remote_active)
        self.assertEqual(self.app._supervisor.reconnects, 1)


if __name__ == "__main__":
    unittest.main()
