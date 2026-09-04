import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ovb_rc003 import system_microphone_gain


class AdaptiveMicrophoneGainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.h180 = ("H180 Plus Microphone", "Windows WASAPI")

    @staticmethod
    def _weak_speech_block():
        # Roughly RMS 59 with speech-like peaks above the digital-noise gate.
        return np.concatenate(
            [np.full(42, 200, dtype="int16"), np.zeros(438, dtype="int16")]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_weak_microphone_is_raised_quickly_without_fixed_device_name(self):
        gain = system_microphone_gain.AdaptiveMicrophoneGain(self.root)
        weak = self._weak_speech_block().reshape(-1, 1)
        output = weak
        for _ in range(12):
            output = gain.process(self.h180, weak)
        self.assertGreater(gain.gain_db_for(self.h180), 25.0)
        self.assertGreater(float(np.sqrt(np.mean(output.astype(float) ** 2))), 1000.0)

    def test_silence_does_not_create_an_amplified_profile(self):
        gain = system_microphone_gain.AdaptiveMicrophoneGain(self.root)
        output = gain.process(self.h180, np.zeros((480, 1), dtype="int16"))
        self.assertEqual(gain.gain_db_for(self.h180), 0.0)
        self.assertTrue(np.array_equal(output, np.zeros((480, 1), dtype="int16")))

    def test_loud_source_reduces_a_previous_gain_and_limits_peak(self):
        path = self.root / system_microphone_gain.PROFILE_FILENAME
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": [
                        {
                            "name": self.h180[0],
                            "host_api": self.h180[1],
                            "gain_db": 30.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        gain = system_microphone_gain.AdaptiveMicrophoneGain(self.root)
        output = gain.process(
            self.h180, np.asarray([20000, -20000], dtype="int16")
        )
        self.assertLess(gain.gain_db_for(self.h180), 5.0)
        self.assertLessEqual(int(np.max(np.abs(output.astype("int32")))), 30000)

    def test_each_microphone_profile_is_independent_and_persistent(self):
        other = ("Webcam Microphone", "Windows WASAPI")
        gain = system_microphone_gain.AdaptiveMicrophoneGain(self.root)
        for _ in range(10):
            gain.process(self.h180, self._weak_speech_block())
            gain.process(other, np.full(480, 2500, dtype="int16"))
        h180_gain = gain.gain_db_for(self.h180)
        other_gain = gain.gain_db_for(other)
        gain.close()

        restored = system_microphone_gain.AdaptiveMicrophoneGain(self.root)
        self.assertAlmostEqual(restored.gain_db_for(self.h180), h180_gain, places=2)
        self.assertAlmostEqual(restored.gain_db_for(other), other_gain, places=2)
        self.assertGreater(restored.gain_db_for(self.h180), other_gain + 20.0)

    def test_disabled_mode_preserves_pcm_and_does_not_learn(self):
        gain = system_microphone_gain.AdaptiveMicrophoneGain(
            self.root, enabled=False
        )
        samples = np.asarray([59, -59], dtype="int16")
        output = gain.process(self.h180, samples)
        self.assertTrue(np.array_equal(output, samples))
        self.assertEqual(gain.gain_db_for(self.h180), 0.0)


if __name__ == "__main__":
    unittest.main()
