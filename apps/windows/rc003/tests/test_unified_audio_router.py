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
    def __init__(self, callback, device=None):
        self.callback = callback
        self.device = device
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
        stream = FakeInputStream(kwargs["callback"], kwargs.get("device"))
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

    def test_system_status_names_the_actual_microphone(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        self.assertEqual(
            unified_audio_router.describe_status(self.root, True),
            "当前音源：Built-in Mic",
        )
        status = unified_audio_router.read_status(self.root)
        self.assertEqual(status["system_input_name"], "Built-in Mic")
        self.assertEqual(status["system_input_host_api"], "Windows WASAPI")

    def test_system_microphone_auto_gain_is_applied_but_rc003_is_unchanged(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        system_stream = self.sd.streams[0]
        sink = FakeSink.instances[0]

        weak_speech = [200] * 14 + [0] * 146
        for _ in range(12):
            system_stream.emit(weak_speech)
        self.assertTrue(_wait_until(lambda: len(sink.writes) >= 12))
        system_peak = int(np.max(np.abs(sink.writes[-1][0].astype("int32"))))
        self.assertGreater(system_peak, 1000)

        self.assertTrue(self.router.begin_remote())
        self.assertTrue(self.router.write_remote([59] * 160))
        self.assertTrue(_wait_until(lambda: len(sink.writes) >= 13))
        self.assertEqual(int(np.max(sink.writes[-1][0])), 59)

    def test_system_input_can_switch_without_replacing_virtual_output(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        first_input = self.sd.streams[0]
        sink = FakeSink.instances[0]

        self.assertTrue(
            self.router.select_system_input("UU Remote Mic", "Windows WASAPI")
        )

        self.assertEqual(
            self.router.system_input_selection,
            ("UU Remote Mic", "Windows WASAPI"),
        )
        self.assertTrue(first_input.stopped)
        self.assertTrue(first_input.closed)
        self.assertIs(FakeSink.instances[0], sink)

    def test_automatic_selection_accepts_any_number_of_source_ids(self):
        candidates = [self.input_endpoint]
        self.assertTrue(
            self.router.request_automatic_system_input("source-a", candidates)
        )
        self.assertTrue(
            self.router.request_automatic_system_input("source-b", candidates)
        )
        self.assertEqual(
            self.router._automatic_source_last,
            {
                "source-a": ("Built-in Mic", "Windows WASAPI"),
                "source-b": ("Built-in Mic", "Windows WASAPI"),
            },
        )

    def test_unclassified_shortcut_never_reuses_a_previous_microphone(self):
        source_id = "shortcut:unknown:ralt"
        stale_remote = ("ToDesk Virtual Audio", "Windows WASAPI")
        self.router._automatic_source_last[source_id] = stale_remote

        self.assertTrue(
            self.router.request_automatic_system_input(
                source_id,
                [self.input_endpoint],
                fallback=self.input_endpoint,
            )
        )

        self.assertNotIn(source_id, self.router._automatic_source_last)
        self.assertEqual(
            self.router.system_input_selection,
            ("Built-in Mic", "Windows WASAPI"),
        )
        self.router.finish_automatic_system_input()

    def test_unclassified_shortcut_keeps_current_live_microphone_without_fallback(self):
        remote = audio_output.AudioEndpoint(
            "UU Remote Virtual Mic", "Windows WASAPI"
        )
        self.assertTrue(
            self.router.select_system_input(remote.name, remote.host_api)
        )

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:unknown:ralt",
                [self.input_endpoint, remote],
                fallback=None,
            )
        )

        self.assertEqual(
            self.router.system_input_selection,
            (remote.name, remote.host_api),
        )
        self.router.finish_automatic_system_input()

    def test_capture_demand_never_reuses_a_stale_microphone_binding(self):
        remote = audio_output.AudioEndpoint(
            "UU Remote Virtual Mic", "Windows WASAPI"
        )
        source_id = unified_audio_router.AUTOMATIC_DEMAND_SOURCE_ID
        self.router._automatic_source_last[source_id] = (
            remote.name,
            remote.host_api,
        )

        self.assertTrue(
            self.router.request_automatic_system_input(
                source_id,
                [self.input_endpoint, remote],
                fallback=self.input_endpoint,
            )
        )

        self.assertNotIn(source_id, self.router._automatic_source_last)
        self.assertEqual(
            self.router.system_input_selection,
            (self.input_endpoint.name, self.input_endpoint.host_api),
        )
        self.router.finish_automatic_system_input()

    def test_learned_gain_allows_a_quiet_microphone_to_win_detection(self):
        self.router.close()
        remote = audio_output.AudioEndpoint(
            "ToDesk Virtual Audio", "Windows WASAPI"
        )
        h180 = audio_output.AudioEndpoint(
            "H180 Plus Microphone", "Windows WASAPI"
        )
        self.sd.query_devices = lambda: [
            {
                "name": remote.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
            {
                "name": h180.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        ]
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name=remote.name,
            system_input_host_api=remote.host_api,
            logger=logging.getLogger("unified-audio-quiet-h180-selection-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            automatic_probe_seconds=0.8,
            automatic_evaluation_seconds=0.04,
            automatic_baseline_seconds=0.0,
            automatic_confirm_windows=2,
            automatic_alternative_confirm_windows=5,
            automatic_alternative_min_seconds=0.1,
        )
        with self.router._system_microphone_gain._lock:
            self.router._system_microphone_gain._profiles[
                (h180.name, h180.host_api)
            ] = 32.0

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:physical:ralt",
                [remote, h180],
                fallback=remote,
            )
        )
        self.assertTrue(_wait_until(lambda: len(self.sd.streams) >= 1))
        # Both probes are open because the forwarding worker itself was not
        # started. Select H180 by its fake PortAudio device index.
        h180_probe = next(stream for stream in self.sd.streams if stream.device == 1)
        # Raw RMS 8 is below the detector's minimum of 24. The learned H180
        # gain raises it only for endpoint comparison, not by retaining PCM.
        quiet_speech = (6, 9, 7, 12, 8, 14, 9)
        for index in range(56):
            level = quiet_speech[index % len(quiet_speech)]
            h180_probe.emit([level] * 160)
            time.sleep(0.01)

        self.assertTrue(
            _wait_until(
                lambda: self.router.system_input_selection
                == (h180.name, h180.host_api)
            )
        )

    def test_repeated_confirmed_misroutes_temporarily_demote_fallback(self):
        h180 = audio_output.AudioEndpoint(
            "H180 Plus Microphone", "Windows WASAPI"
        )
        remote = audio_output.AudioEndpoint(
            "ToDesk Virtual Audio", "Windows WASAPI"
        )
        h180_key = (h180.name, h180.host_api)
        self.router._note_automatic_endpoint_failure(h180_key)
        self.router._note_automatic_endpoint_failure(h180_key)

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:physical:ralt",
                [h180, remote],
                fallback=h180,
            )
        )

        self.assertEqual(
            self.router.system_input_selection,
            (remote.name, remote.host_api),
        )
        self.router.finish_automatic_system_input()

    def test_one_hard_open_failure_immediately_tries_another_endpoint(self):
        h180 = audio_output.AudioEndpoint(
            "H180 Plus Microphone", "Windows WASAPI"
        )
        remote = audio_output.AudioEndpoint(
            "UU Remote Virtual Mic", "Windows WASAPI"
        )
        self.router._note_automatic_endpoint_failure(
            (h180.name, h180.host_api), hard=True
        )

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:physical:ralt",
                [h180, remote],
                fallback=h180,
            )
        )

        self.assertEqual(
            self.router.system_input_selection,
            (remote.name, remote.host_api),
        )
        self.router.finish_automatic_system_input()

    def test_confirmed_signal_restores_a_temporarily_demoted_microphone(self):
        h180 = ("H180 Plus Microphone", "Windows WASAPI")
        self.router._note_automatic_endpoint_failure(h180)
        self.router._note_automatic_endpoint_failure(h180)
        self.assertIn(h180, self.router._automatic_endpoint_degraded_until)

        self.router._note_automatic_endpoint_success(h180)

        self.assertNotIn(h180, self.router._automatic_endpoint_failures)
        self.assertNotIn(h180, self.router._automatic_endpoint_degraded_until)

    def test_explicit_fallback_overrides_current_input_for_first_syllable(self):
        h180 = audio_output.AudioEndpoint(
            "H180 Plus Microphone", "Windows WASAPI"
        )
        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:physical:ralt",
                [self.input_endpoint, h180],
                fallback=h180,
            )
        )
        self.assertEqual(
            self.router.system_input_selection,
            ("H180 Plus Microphone", "Windows WASAPI"),
        )

    def test_automatic_selection_rejects_only_feedback_loop_candidate(self):
        self.assertFalse(
            self.router.request_automatic_system_input(
                "source-a",
                [audio_output.AudioEndpoint("CABLE Output", "Windows WASAPI")],
            )
        )

    def test_automatic_selection_never_opens_system_mics_during_rc003_voice(self):
        with self.router._lock:
            self.router._remote_active = True
        self.assertFalse(
            self.router.request_automatic_system_input(
                "shortcut:ralt", [self.input_endpoint]
            )
        )
        self.assertEqual(self.sd.streams, [])

    def test_automatic_selection_waits_for_speech_after_shortcut(self):
        self.router.close()
        second = audio_output.AudioEndpoint("Local Mic", "Windows WASAPI")
        self.sd.query_devices = lambda: [
            {
                "name": "Built-in Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
            {
                "name": "Local Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        ]
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name="Built-in Mic",
            system_input_host_api="Windows WASAPI",
            logger=logging.getLogger("unified-audio-delayed-speech-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            automatic_probe_seconds=1.2,
            automatic_evaluation_seconds=0.05,
            automatic_alternative_confirm_windows=3,
            automatic_alternative_min_seconds=0.25,
        )

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:ralt", [self.input_endpoint, second]
            )
        )
        self.assertTrue(_wait_until(lambda: len(self.sd.streams) >= 2))
        time.sleep(0.35)  # longer than the old 0.28-second one-shot probe
        for level in (1800, 2600, 2100, 3200, 2400, 3500):
            self.sd.streams[-1].emit([level] * 160)
            time.sleep(0.05)

        self.assertTrue(
            _wait_until(
                lambda: self.router.system_input_selection
                == ("Local Mic", "Windows WASAPI")
            )
        )
        self.assertEqual(
            self.router._automatic_endpoint_failures.get(
                ("Built-in Mic", "Windows WASAPI")
            ),
            1,
        )

    def test_louder_alternative_cannot_steal_while_preferred_has_speech(self):
        self.router.close()
        alternative = audio_output.AudioEndpoint(
            "Remote Virtual Mic", "Windows WASAPI"
        )
        self.sd.query_devices = lambda: [
            {
                "name": self.input_endpoint.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
            {
                "name": alternative.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        ]
        logger_name = "unified-audio-no-volume-steal-test"
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name=self.input_endpoint.name,
            system_input_host_api=self.input_endpoint.host_api,
            logger=logging.getLogger(logger_name),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            automatic_probe_seconds=0.7,
            automatic_evaluation_seconds=0.03,
            automatic_baseline_seconds=0.0,
            automatic_alternative_confirm_windows=5,
            automatic_alternative_min_seconds=0.1,
        )

        with self.assertLogs(logger_name, level="INFO") as captured:
            self.assertTrue(
                self.router.request_automatic_system_input(
                    "shortcut:unknown:ralt",
                    [self.input_endpoint, alternative],
                    fallback=self.input_endpoint,
                )
            )
            self.assertTrue(_wait_until(lambda: len(self.sd.streams) >= 2))
            preferred_stream = next(
                stream for stream in self.sd.streams if stream.device == 0
            )
            alternative_stream = next(
                stream for stream in self.sd.streams if stream.device == 1
            )
            preferred_shape = (180, 260, 210, 320, 240)
            alternative_shape = (2400, 4200, 3100, 5000, 3600)
            for index in range(75):
                preferred_stream.emit(
                    [preferred_shape[index % len(preferred_shape)]] * 160
                )
                alternative_stream.emit(
                    [alternative_shape[index % len(alternative_shape)]] * 160
                )
                time.sleep(0.01)
            self.assertTrue(
                _wait_until(
                    lambda: not self.router._automatic_probe_threads,
                    timeout=2.0,
                )
            )

        preferred_key = (
            self.input_endpoint.name,
            self.input_endpoint.host_api,
        )
        self.assertEqual(self.router.system_input_selection, preferred_key)
        self.assertNotIn(preferred_key, self.router._automatic_endpoint_failures)
        decision_log = "\n".join(captured.output)
        self.assertIn("retain Built-in Mic", decision_log)
        self.assertIn("preferred_confirmed_for_press", decision_log)
        self.assertIn("Remote Virtual Mic", decision_log)

    def test_sensitive_pre_roll_is_zeroed_after_output(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        sink = FakeSink.instances[0]
        pre_roll = np.asarray([[100], [-200], [300]], dtype="int16")

        self.router._write_item(("system", pre_roll, 48000, 1, True))

        self.assertEqual(sink.writes[-1][0].reshape(-1).tolist(), [0, -100, 300])
        self.assertEqual(pre_roll.reshape(-1).tolist(), [0, 0, 0])

    def test_rejected_pre_roll_is_also_zeroed(self):
        pre_roll = np.asarray([[91], [-92]], dtype="int16")
        self.assertFalse(
            self.router.select_system_input(
                "CABLE Output",
                "Windows WASAPI",
                pre_roll=(pre_roll, 48000, 1),
            )
        )
        self.assertEqual(pre_roll.reshape(-1).tolist(), [0, 0])

    def test_switch_replays_speech_captured_before_winner_confirmation(self):
        self.router.close()
        second = audio_output.AudioEndpoint("Remote Virtual Mic", "Windows WASAPI")
        self.input_patch.stop()
        self.input_patch = mock.patch.object(
            audio_output,
            "enumerate_input_endpoints",
            return_value=[self.input_endpoint, second],
        )
        self.input_patch.start()
        self.sd.query_devices = lambda: [
            {
                "name": "Built-in Mic",
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
            {
                "name": second.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        ]
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name="Built-in Mic",
            system_input_host_api="Windows WASAPI",
            logger=logging.getLogger("unified-audio-preroll-replay-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            automatic_probe_seconds=1.2,
            automatic_evaluation_seconds=0.05,
            automatic_baseline_seconds=0.0,
            automatic_confirm_windows=2,
            automatic_alternative_confirm_windows=3,
            automatic_alternative_min_seconds=0.1,
        )
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        sink = FakeSink.instances[-1]

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:unknown:ralt", [self.input_endpoint, second]
            )
        )
        self.assertTrue(_wait_until(lambda: len(self.sd.streams) >= 2))
        probe = self.sd.streams[-1]
        speech_shape = (1800, 2600, 2100, 3200, 2400, 3500)
        # Hold each speech-envelope level across several evaluation intervals.
        # This models real syllable-scale variation and remains deterministic
        # even when the full-suite runner schedules the probe less frequently.
        for level in speech_shape:
            level_deadline = time.monotonic() + 0.15
            while time.monotonic() < level_deadline:
                probe.emit([level] * 160)
                time.sleep(0.01)

        self.assertTrue(
            _wait_until(
                lambda: self.router.system_input_selection
                == (second.name, second.host_api),
                timeout=2.0,
            )
        )
        self.assertTrue(
            _wait_until(
                lambda: any(int(np.max(write[0])) > 1000 for write in sink.writes),
                timeout=2.0,
            )
        )

    def test_finish_shortcut_zeroes_pending_pre_roll(self):
        ring = unified_audio_router.microphone_auto_select.PcmPreRollBuffer(1.5)
        ring.add(np.asarray([700, -700], dtype="int16"), 16000, 1)
        key = ("Built-in Mic", "Windows WASAPI")
        with self.router._lock:
            self.router._automatic_buffers = {key: ring}

        self.router.finish_automatic_system_input()

        self.assertEqual(ring.buffered_frames, 0)
        self.assertEqual(self.router._automatic_buffers, {})
        self.assertEqual(self.router._automatic_arm_until, 0.0)

    def test_finish_erases_pre_roll_but_preserves_live_tail(self):
        live = np.asarray([[11], [12]], dtype="int16")
        pre_roll = np.asarray([[21], [22]], dtype="int16")
        self.router._queue.put_nowait(("system", live, 48000, 1))
        self.router._queue.put_nowait(("system", pre_roll, 48000, 1, True))

        self.router.finish_automatic_system_input()

        retained = self.router._queue.get_nowait()
        self.assertIs(retained[1], live)
        self.assertEqual(pre_roll.reshape(-1).tolist(), [0, 0])

    def test_rc003_priority_zeroes_automatic_pre_roll(self):
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        ring = unified_audio_router.microphone_auto_select.PcmPreRollBuffer(1.5)
        ring.add(np.asarray([800, -800], dtype="int16"), 16000, 1)
        key = ("Built-in Mic", "Windows WASAPI")
        with self.router._lock:
            self.router._automatic_buffers = {key: ring}

        self.assertTrue(self.router.begin_remote())

        self.assertEqual(ring.buffered_frames, 0)
        self.assertEqual(self.router._automatic_buffers, {})

    def test_system_input_switch_rejects_virtual_cable_recording_endpoint(self):
        self.assertFalse(
            self.router.select_system_input("CABLE Output", "Windows WASAPI")
        )
        self.assertEqual(
            self.router.system_input_selection,
            ("Built-in Mic", "Windows WASAPI"),
        )

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

    def test_shortcut_prewarms_on_demand_input_before_consumer_poll(self):
        self._replace_with_on_demand_router()
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))
        self.assertFalse(self.detector.active)

        self.assertTrue(
            self.router.request_automatic_system_input(
                "shortcut:unknown:ralt", [self.input_endpoint]
            )
        )

        self.assertTrue(_wait_until(lambda: self.router.status_code == "system"))
        self.assertTrue(any(stream.active for stream in self.sd.streams))

    def test_consumer_demand_starts_selection_without_a_visible_shortcut(self):
        self.router.close()
        remote = audio_output.AudioEndpoint(
            "UU Remote Virtual Mic", "Windows WASAPI"
        )
        self.input_patch.stop()
        self.input_patch = mock.patch.object(
            audio_output,
            "enumerate_input_endpoints",
            return_value=[self.input_endpoint, remote],
        )
        self.input_patch.start()
        self.sd.query_devices = lambda: [
            {
                "name": self.input_endpoint.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
            {
                "name": remote.name,
                "hostapi": 0,
                "max_input_channels": 1,
                "default_samplerate": 48000.0,
            },
        ]
        self.detector = FakeActivityDetector()
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name=self.input_endpoint.name,
            system_input_host_api=self.input_endpoint.host_api,
            logger=logging.getLogger("unified-audio-demand-selection-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            on_demand_system_input=True,
            activity_detector_factory=lambda: self.detector,
            activity_poll_seconds=0.01,
            idle_release_seconds=0.05,
            automatic_candidates=[self.input_endpoint, remote],
            automatic_probe_seconds=1.0,
            automatic_evaluation_seconds=0.04,
            automatic_baseline_seconds=0.0,
            automatic_alternative_confirm_windows=3,
            automatic_alternative_min_seconds=0.1,
            automatic_demand_recheck_seconds=2.0,
        )
        self.router.start()
        self.assertTrue(_wait_until(lambda: self.router.status_code == "idle"))

        self.detector.active = True
        self.assertTrue(_wait_until(lambda: len(self.sd.streams) >= 2))
        remote_probe = next(stream for stream in self.sd.streams if stream.device == 1)
        speech_shape = (1800, 3200, 2300, 4100, 2700, 4600)
        for index in range(60):
            remote_probe.emit([speech_shape[index % len(speech_shape)]] * 160)
            time.sleep(0.01)

        self.assertTrue(
            _wait_until(
                lambda: self.router.system_input_selection
                == (remote.name, remote.host_api),
                timeout=2.0,
            )
        )

    def test_long_lived_consumer_is_rechecked_without_more_key_events(self):
        self.router.close()
        self.detector = FakeActivityDetector()
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name=self.input_endpoint.name,
            system_input_host_api=self.input_endpoint.host_api,
            logger=logging.getLogger("unified-audio-demand-recheck-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            on_demand_system_input=True,
            activity_detector_factory=lambda: self.detector,
            activity_poll_seconds=0.01,
            automatic_candidates=[self.input_endpoint],
            automatic_probe_seconds=0.08,
            automatic_evaluation_seconds=0.02,
            automatic_baseline_seconds=0.0,
            automatic_demand_recheck_seconds=0.12,
        )
        self.detector.active = True
        self.router.start()

        self.assertTrue(
            _wait_until(lambda: self.router._automatic_generation >= 2, timeout=1.0)
        )
        self.assertTrue(self.detector.active)

    def test_detector_failure_does_not_invent_an_automatic_selection_trigger(self):
        self.router.close()
        self.detector = FakeActivityDetector()
        self.detector.error = RuntimeError("session API unavailable")
        self.router = unified_audio_router.UnifiedAudioRouter(
            config_root=self.root,
            output_name="CABLE Input",
            output_host_api="Windows WASAPI",
            system_input_name=self.input_endpoint.name,
            system_input_host_api=self.input_endpoint.host_api,
            logger=logging.getLogger("unified-audio-failed-demand-test"),
            sink_factory=FakeSink,
            sounddevice_loader=lambda: self.sd,
            retry_seconds=0.02,
            on_demand_system_input=True,
            activity_detector_factory=lambda: self.detector,
            activity_poll_seconds=0.01,
            automatic_candidates=[self.input_endpoint],
            automatic_probe_seconds=0.08,
            automatic_demand_recheck_seconds=0.12,
        )
        self.router.start()

        self.assertTrue(
            _wait_until(
                lambda: self.router.status_code == "demand_detection_failed"
            )
        )
        time.sleep(0.2)
        self.assertEqual(self.router._automatic_generation, 0)

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
