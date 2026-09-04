"""Pure tests for the short-lived Windows keyboard-origin diagnostic."""

import unittest

from ovb_rc003 import key_origin_probe_windows as probe


def _low(*, injected: bool = False, observed_ns: int = 0):
    return probe.LowLevelOriginEvent(
        token="ralt",
        injected=injected,
        lower_integrity_injected=False,
        scan_code=0x38,
        extended=True,
        observed_ns=observed_ns,
    )


def _raw(device: str, observed_ns: int = 0):
    return probe.RawOriginEvent(
        token="ralt",
        device_fingerprint=device,
        scan_code=0x38,
        extended=True,
        observed_ns=observed_ns,
    )


def _async(observed_ns: int = 0):
    return probe.AsyncOriginEvent(token="ralt", observed_ns=observed_ns)


def _foreground(observed_ns: int = 0):
    return probe.ForegroundOriginEvent(token="ralt", observed_ns=observed_ns)


def _hotkey(observed_ns: int = 0):
    return probe.HotkeyMessageOriginEvent(token="ralt", observed_ns=observed_ns)


class OriginComparisonTests(unittest.TestCase):
    def test_stage_press_count_uses_raw_input_when_hook_is_unavailable(self):
        samples = probe.OriginStageSamples(
            (), tuple(_raw("local", i) for i in range(5))
        )
        self.assertEqual(probe.stage_press_count(samples), 5)

    def test_stage_press_count_uses_async_state_for_remote_input(self):
        samples = probe.OriginStageSamples(
            (), (), tuple(_async(i) for i in range(5))
        )
        self.assertEqual(probe.stage_press_count(samples), 5)

    def test_stage_press_count_uses_foreground_window_for_diagnostic_progress(self):
        samples = probe.OriginStageSamples(
            (), (), (), tuple(_foreground(i) for i in range(5))
        )
        self.assertEqual(probe.stage_press_count(samples), 5)

    def test_stage_press_count_uses_global_hotkey_messages(self):
        samples = probe.OriginStageSamples(
            (), (), (), (), tuple(_hotkey(i) for i in range(5))
        )
        self.assertEqual(probe.stage_press_count(samples), 5)

    def test_stage_press_count_does_not_sum_duplicate_raw_collections(self):
        samples = probe.OriginStageSamples(
            (),
            tuple(_raw("primary", i) for i in range(5))
            + tuple(_raw("duplicate", i) for i in range(2)),
        )
        self.assertEqual(probe.stage_press_count(samples), 5)

    def test_injection_flag_can_distinguish_remote_from_local(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(tuple(_low(observed_ns=i) for i in range(5))),
            probe.OriginStageSamples(
                tuple(_low(injected=True, observed_ns=i) for i in range(5))
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.DISTINGUISHABLE)
        self.assertEqual(result.method, "injection_flag")

    def test_raw_only_local_and_injected_remote_are_distinguishable(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                (), tuple(_raw("local", i) for i in range(5))
            ),
            probe.OriginStageSamples(
                tuple(_low(injected=True, observed_ns=i) for i in range(5))
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.DISTINGUISHABLE)
        self.assertEqual(result.method, "raw_local_injected_remote")

    def test_raw_local_and_async_only_remote_are_distinguishable(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                (), tuple(_raw("local", i) for i in range(5))
            ),
            probe.OriginStageSamples(
                (), (), tuple(_async(i) for i in range(5))
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.DISTINGUISHABLE)
        self.assertEqual(result.method, "raw_local_async_remote")

    def test_foreground_only_remote_is_not_claimed_background_capable(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                (), tuple(_raw("local", i) for i in range(5))
            ),
            probe.OriginStageSamples(
                (), (), (), tuple(_foreground(i) for i in range(5))
            ),
        )
        self.assertEqual(
            result.status, probe.OriginComparisonStatus.FOREGROUND_ONLY
        )
        self.assertEqual(result.method, "raw_local_foreground_remote")
        self.assertIn("后台桥接无法", result.detail)

    def test_global_hotkey_remote_is_background_capable(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                (), tuple(_raw("local", i) for i in range(5))
            ),
            probe.OriginStageSamples(
                (), (), (), (), tuple(_hotkey(i) for i in range(5))
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.DISTINGUISHABLE)
        self.assertEqual(result.method, "raw_local_hotkey_remote")

    def test_same_raw_only_origin_is_reported_as_indistinguishable(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                (), tuple(_raw("same", i) for i in range(5))
            ),
            probe.OriginStageSamples(
                (), tuple(_raw("same", i) for i in range(5))
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.INDISTINGUISHABLE)

    def test_stable_different_raw_devices_can_distinguish_sources(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                tuple(_low(observed_ns=i) for i in range(5)),
                tuple(_raw("local", i) for i in range(5)),
            ),
            probe.OriginStageSamples(
                tuple(_low(observed_ns=i) for i in range(5)),
                tuple(_raw("remote", i) for i in range(5)),
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.DISTINGUISHABLE)
        self.assertEqual(result.method, "raw_input_device")

    def test_same_stable_windows_origin_is_reported_honestly(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples(
                tuple(_low(observed_ns=i) for i in range(5)),
                tuple(_raw("same", i) for i in range(5)),
            ),
            probe.OriginStageSamples(
                tuple(_low(observed_ns=i) for i in range(5)),
                tuple(_raw("same", i) for i in range(5)),
            ),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.INDISTINGUISHABLE)

    def test_too_few_presses_are_not_overinterpreted(self):
        result = probe.compare_origin_stages(
            probe.OriginStageSamples((_low(), _low())),
            probe.OriginStageSamples((_low(injected=True), _low(injected=True))),
        )
        self.assertEqual(result.status, probe.OriginComparisonStatus.INSUFFICIENT)


class EventHandlingTests(unittest.TestCase):
    def test_chord_parser_uses_final_key_as_trigger(self):
        tokens, trigger = probe._target_tokens("ralt+space")
        self.assertEqual(tokens, ("ralt", "space"))
        self.assertEqual(trigger, "space")

    def test_low_level_handler_suppresses_only_selected_chord_and_deduplicates(self):
        events = []
        listener = probe.KeyOriginProbe("ralt", events.append)
        ralt = probe.KBDLLHOOKSTRUCT(0xA5, 0x38, probe.LLKHF_EXTENDED, 0, 0)
        letter_a = probe.KBDLLHOOKSTRUCT(0x41, 0x1E, 0, 0, 0)

        self.assertTrue(listener._handle_low_level_event(probe.WM_SYSKEYDOWN, ralt))
        self.assertTrue(listener._handle_low_level_event(probe.WM_SYSKEYDOWN, ralt))
        self.assertFalse(listener._handle_low_level_event(probe.WM_KEYDOWN, letter_a))
        self.assertEqual(len(events), 1)

        self.assertTrue(listener._handle_low_level_event(probe.WM_SYSKEYUP, ralt))
        injected = probe.KBDLLHOOKSTRUCT(
            0xA5,
            0x38,
            probe.LLKHF_EXTENDED | probe.LLKHF_INJECTED,
            0,
            0,
        )
        self.assertTrue(listener._handle_low_level_event(probe.WM_SYSKEYDOWN, injected))
        self.assertEqual(len(events), 2)
        self.assertTrue(events[-1].injected)

    def test_directional_modifier_alias_is_observed_during_diagnostic(self):
        events = []
        listener = probe.KeyOriginProbe("ralt", events.append)
        generic_alt = probe.KBDLLHOOKSTRUCT(0x12, 0, 0, 0, 0)
        self.assertTrue(
            listener._handle_low_level_event(probe.WM_SYSKEYDOWN, generic_alt)
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].token, "alt")

    def test_ralt_async_poll_includes_generic_and_opposite_alt_variants(self):
        candidates = probe._async_vk_candidates("ralt")
        self.assertIn(0xA5, candidates)
        self.assertIn(0x12, candidates)
        self.assertIn(0xA4, candidates)

    def test_raw_device_path_is_salted_anonymized_and_deduplicated(self):
        events = []
        first = probe.KeyOriginProbe("ralt", events.append)
        path = r"\\?\HID#VID_1234&PID_5678"
        first._handle_raw_keyboard(
            device_path=path,
            vkey=0xA5,
            make_code=0x38,
            flags=probe.RI_KEY_E0,
            message=probe.WM_SYSKEYDOWN,
        )
        first._handle_raw_keyboard(
            device_path=path,
            vkey=0xA5,
            make_code=0x38,
            flags=probe.RI_KEY_E0,
            message=probe.WM_SYSKEYDOWN,
        )
        self.assertEqual(len(events), 1)
        self.assertNotIn("VID_", events[0].device_fingerprint)

        other_events = []
        second = probe.KeyOriginProbe("ralt", other_events.append)
        second._handle_raw_keyboard(
            device_path=path,
            vkey=0xA5,
            make_code=0x38,
            flags=probe.RI_KEY_E0,
            message=probe.WM_SYSKEYDOWN,
        )
        self.assertNotEqual(
            events[0].device_fingerprint, other_events[0].device_fingerprint
        )

    def test_async_state_emits_only_on_chord_down_edge(self):
        events = []
        listener = probe.KeyOriginProbe("ralt", events.append)

        class FakeUser32:
            pressed = False

            def GetAsyncKeyState(self, _vk):
                return 0x8000 if self.pressed else 0

        user32 = FakeUser32()
        listener._poll_async_key_state(user32)
        user32.pressed = True
        listener._poll_async_key_state(user32)
        listener._poll_async_key_state(user32)
        user32.pressed = False
        listener._poll_async_key_state(user32)
        user32.pressed = True
        listener._poll_async_key_state(user32)

        self.assertEqual(len(events), 2)
        self.assertTrue(
            all(isinstance(event, probe.AsyncOriginEvent) for event in events)
        )

    def test_foreground_ralt_emits_once_per_press_and_ignores_other_keys(self):
        events = []
        listener = probe.KeyOriginProbe("ralt", events.append)

        self.assertTrue(
            listener.handle_foreground_key_event(
                native_vk=0xA5,
                native_scan_code=0x38,
                qt_key=0x01000023,
                is_press=True,
            )
        )
        self.assertTrue(
            listener.handle_foreground_key_event(
                native_vk=0xA5,
                native_scan_code=0x38,
                qt_key=0x01000023,
                is_press=True,
                is_auto_repeat=True,
            )
        )
        self.assertFalse(
            listener.handle_foreground_key_event(
                native_vk=0x41,
                native_scan_code=0x1E,
                qt_key=0x41,
                is_press=True,
            )
        )
        self.assertEqual(len(events), 1)
        listener.handle_foreground_key_event(
            native_vk=0xA5,
            native_scan_code=0x38,
            qt_key=0x01000023,
            is_press=False,
        )
        listener.handle_foreground_key_event(
            native_vk=0,
            native_scan_code=0,
            qt_key=0x01000023,
            is_press=True,
        )
        self.assertEqual(len(events), 2)
        self.assertTrue(
            all(isinstance(event, probe.ForegroundOriginEvent) for event in events)
        )


if __name__ == "__main__":
    unittest.main()
