import unittest

import numpy as np

from ovb_rc003 import audio_output, microphone_auto_select


class RecommendedCandidateTests(unittest.TestCase):
    def test_prefers_one_wasapi_interface_and_excludes_cable_output(self):
        endpoints = [
            audio_output.AudioEndpoint("Desk Mic", "MME"),
            audio_output.AudioEndpoint("Desk Mic", "Windows WASAPI"),
            audio_output.AudioEndpoint("Remote Mic", "Windows WASAPI"),
            audio_output.AudioEndpoint("CABLE Output", "Windows WASAPI"),
        ]
        self.assertEqual(
            microphone_auto_select.recommended_candidates(endpoints),
            [
                audio_output.AudioEndpoint("Desk Mic", "Windows WASAPI"),
                audio_output.AudioEndpoint("Remote Mic", "Windows WASAPI"),
            ],
        )

    def test_config_candidate_shape_is_deduplicated_and_safe(self):
        value = [
            {"name": "Desk Mic", "host_api": "Windows WASAPI"},
            {"name": "Desk Mic", "host_api": "Windows WASAPI"},
            {"name": "CABLE Output", "host_api": "Windows WASAPI"},
            {"wrong": "shape"},
        ]
        self.assertEqual(
            microphone_auto_select.normalize_configured_candidates(value),
            [audio_output.AudioEndpoint("Desk Mic", "Windows WASAPI")],
        )

    def test_legacy_exclusive_interfaces_are_dropped_when_wasapi_exists(self):
        endpoints = [
            audio_output.AudioEndpoint("Realtek raw mic", "Windows WDM-KS"),
            audio_output.AudioEndpoint("Headset Microphone", "Windows WASAPI"),
        ]
        self.assertEqual(
            microphone_auto_select.recommended_candidates(endpoints),
            [
                audio_output.AudioEndpoint(
                    "Headset Microphone", "Windows WASAPI"
                )
            ],
        )

    def test_physical_keyboard_prefers_h180_but_retains_virtual_fallbacks(self):
        endpoints = [
            audio_output.AudioEndpoint(
                "麦克风阵列 (UU远程虚拟音频设备)", "Windows WASAPI"
            ),
            audio_output.AudioEndpoint(
                "耳机式麦克风 (H180 Plus (Type C))", "Windows WASAPI"
            ),
            audio_output.AudioEndpoint(
                "麦克风 (ToDesk Virtual Audio)", "Windows WASAPI"
            ),
        ]
        ordered = microphone_auto_select.candidates_for_keyboard_origin(
            endpoints, injected=False
        )
        self.assertEqual(ordered[0].name, "耳机式麦克风 (H180 Plus (Type C))")
        self.assertEqual(len(ordered), 3)
        self.assertTrue(any("UU" in item.name for item in ordered[1:]))
        self.assertTrue(any("ToDesk" in item.name for item in ordered[1:]))

    def test_injected_keyboard_prefers_virtual_mics_but_keeps_h180(self):
        endpoints = [
            audio_output.AudioEndpoint(
                "耳机式麦克风 (H180 Plus (Type C))", "Windows WASAPI"
            ),
            audio_output.AudioEndpoint(
                "麦克风阵列 (UU远程虚拟音频设备)", "Windows WASAPI"
            ),
        ]
        ordered = microphone_auto_select.candidates_for_keyboard_origin(
            endpoints, injected=True
        )
        self.assertIn("UU", ordered[0].name)
        self.assertEqual(len(ordered), 2)
        self.assertIn("H180", ordered[1].name)

    def test_unclassified_remote_control_probes_all_microphones(self):
        endpoints = [
            audio_output.AudioEndpoint(
                "耳机式麦克风 (H180 Plus (Type C))", "Windows WASAPI"
            ),
            audio_output.AudioEndpoint(
                "麦克风阵列 (UU远程虚拟音频设备)", "Windows WASAPI"
            ),
            audio_output.AudioEndpoint(
                "麦克风 (ToDesk Virtual Audio)", "Windows WASAPI"
            ),
        ]
        candidates = microphone_auto_select.candidates_for_keyboard_origin(
            endpoints, injected=None
        )
        self.assertEqual(len(candidates), 3)
        self.assertTrue(any("H180" in item.name for item in candidates))
        self.assertTrue(any("UU" in item.name for item in candidates))
        self.assertTrue(any("ToDesk" in item.name for item in candidates))


class ActivitySelectionTests(unittest.TestCase):
    def test_accumulator_retains_only_aggregate_level(self):
        meter = microphone_auto_select.ActivityAccumulator()
        meter.add(np.asarray([-300, 400], dtype="int16"))
        level = meter.snapshot()
        self.assertEqual(level.samples, 2)
        self.assertEqual(level.peak, 400)
        self.assertAlmostEqual(level.rms, 353.553, places=2)
        self.assertFalse(hasattr(meter, "samples"))

    def test_interval_snapshot_resets_without_losing_total(self):
        meter = microphone_auto_select.ActivityAccumulator()
        meter.add(np.asarray([300, 400], dtype="int16"))
        first = meter.take_interval()
        self.assertEqual(first.samples, 2)
        self.assertEqual(meter.take_interval().samples, 0)
        self.assertEqual(meter.snapshot().samples, 2)

    def test_clear_live_microphone_wins(self):
        quiet = ("Desk Mic", "Windows WASAPI")
        speaking = ("Remote Mic", "Windows WASAPI")
        levels = {
            quiet: microphone_auto_select.ActivityLevel(150.0, 300, 100),
            speaking: microphone_auto_select.ActivityLevel(900.0, 1500, 100),
        }
        self.assertEqual(
            microphone_auto_select.choose_active_endpoint(levels), speaking
        )

    def test_similar_room_sound_keeps_last_used_microphone(self):
        previous = ("Desk Mic", "Windows WASAPI")
        other = ("Webcam Mic", "Windows WASAPI")
        levels = {
            previous: microphone_auto_select.ActivityLevel(700.0, 1200, 100),
            other: microphone_auto_select.ActivityLevel(850.0, 1300, 100),
        }
        self.assertEqual(
            microphone_auto_select.choose_active_endpoint(
                levels, preferred=previous
            ),
            previous,
        )

    def test_silence_does_not_change_the_fallback(self):
        endpoint = ("Desk Mic", "Windows WASAPI")
        levels = {
            endpoint: microphone_auto_select.ActivityLevel(30.0, 60, 100)
        }
        self.assertIsNone(
            microphone_auto_select.choose_active_endpoint(levels)
        )

    def test_recent_preferred_speech_blocks_a_louder_alternative(self):
        preferred = ("Headset Mic", "Windows WASAPI")
        alternative = ("Remote Virtual Mic", "Windows WASAPI")
        winner, reason = microphone_auto_select.choose_sustained_alternative(
            {
                preferred: [0.0, 30.0, 48.0, 36.0, 41.0],
                alternative: [0.0, 400.0, 900.0, 600.0, 1100.0, 700.0],
            },
            preferred=preferred,
            minimum_rms=24.0,
        )
        self.assertIsNone(winner)
        self.assertEqual(reason, "preferred_recently_active")

    def test_dynamic_alternative_wins_after_preferred_goes_quiet(self):
        preferred = ("Headset Mic", "Windows WASAPI")
        alternative = ("Remote Virtual Mic", "Windows WASAPI")
        winner, reason = microphone_auto_select.choose_sustained_alternative(
            {
                preferred: [45.0, 38.0, 0.0, 0.0, 0.0, 0.0],
                alternative: [0.0, 180.0, 260.0, 210.0, 330.0, 240.0],
            },
            preferred=preferred,
            minimum_rms=24.0,
        )
        self.assertEqual(winner, alternative)
        self.assertEqual(reason, "alternative_sustained_after_preferred_quiet")

    def test_constant_virtual_startup_signal_is_not_treated_as_speech(self):
        preferred = ("Headset Mic", "Windows WASAPI")
        alternative = ("Remote Virtual Mic", "Windows WASAPI")
        winner, reason = microphone_auto_select.choose_sustained_alternative(
            {
                preferred: [0.0] * 8,
                alternative: [0.0, 900.0, 900.0, 900.0, 900.0, 900.0, 900.0],
            },
            preferred=preferred,
            minimum_rms=24.0,
        )
        self.assertIsNone(winner)
        self.assertEqual(reason, "alternative_steady_background")

    def test_two_similarly_active_alternatives_remain_ambiguous(self):
        preferred = ("Headset Mic", "Windows WASAPI")
        first = ("Remote A", "Windows WASAPI")
        second = ("Remote B", "Windows WASAPI")
        winner, reason = microphone_auto_select.choose_sustained_alternative(
            {
                preferred: [0.0] * 7,
                first: [100.0, 150.0, 120.0, 180.0, 140.0],
                second: [110.0, 145.0, 125.0, 170.0, 150.0],
            },
            preferred=preferred,
            minimum_rms=24.0,
        )
        self.assertIsNone(winner)
        self.assertEqual(reason, "alternative_ambiguous")


class PcmPreRollBufferTests(unittest.TestCase):
    def test_ring_is_bounded_and_zeroes_evicted_storage(self):
        ring = microphone_auto_select.PcmPreRollBuffer(0.05)
        first = np.asarray([[1], [2], [3]], dtype="int16")
        ring.add(first, 40, 1)
        ring.add(np.asarray([[4], [5]], dtype="int16"), 40, 1)
        drained, rate, channels = ring.drain()
        self.assertEqual((rate, channels), (40, 1))
        self.assertEqual(drained.reshape(-1).tolist(), [4, 5])
        self.assertEqual(ring.buffered_frames, 0)

    def test_clear_is_idempotent_and_removes_all_audio(self):
        ring = microphone_auto_select.PcmPreRollBuffer(1.5)
        ring.add(np.asarray([100, -100], dtype="int16"), 16000, 1)
        self.assertGreater(ring.buffered_frames, 0)
        ring.clear()
        ring.clear()
        self.assertEqual(ring.buffered_frames, 0)
        self.assertIsNone(ring.drain())


if __name__ == "__main__":
    unittest.main()
