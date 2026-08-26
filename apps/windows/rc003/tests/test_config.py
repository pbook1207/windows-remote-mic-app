import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, key_mapping


class ConfigRootTests(unittest.TestCase):
    def test_uses_localappdata_when_set(self):
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": "/tmp/fake-appdata"}):
            root = config.config_root()
        self.assertEqual(root, Path("/tmp/fake-appdata") / "RemoteMic" / "RC003")

    def test_falls_back_to_home_without_localappdata(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LOCALAPPDATA", None)
            root = config.config_root()
        self.assertEqual(root, Path.home() / "RemoteMic" / "RC003")


class DefaultConfigPrivacyTests(unittest.TestCase):
    def test_default_config_preserves_existing_users_on_rc003(self):
        self.assertEqual(config.default_config()["selected_device_profile"], "xiaomi-rc003")
        self.assertEqual(config.default_config()["voice_hotkey"], "ralt+space")
        self.assertEqual(
            config.default_config()["voice_secondary_hotkey"], "ralt+space"
        )
        self.assertFalse(
            config.default_config()["voice_secondary_gesture_enabled"]
        )
        self.assertEqual(config.default_config()["gain_db"], 10.0)

    def test_default_config_contains_no_forbidden_identity_fields(self):
        defaults = config.default_config()
        self.assertFalse(config.FORBIDDEN_KEYS.intersection(defaults.keys()))

    def test_default_key_bindings_contains_no_forbidden_identity_fields(self):
        defaults = config.default_key_bindings()
        self.assertFalse(config.FORBIDDEN_KEYS.intersection(defaults.keys()))

    def test_output_endpoint_defaults_to_empty_so_voice_fails_closed(self):
        self.assertEqual(config.default_config()["output_endpoint_name"], "")

    def test_load_preserves_a_user_selected_toggle_right_alt_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "toggle", "voice_hotkey": "ralt"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "toggle")
        self.assertEqual(loaded["voice_hotkey"], "ralt")

    def test_save_preserves_a_user_selected_hold_right_alt_space_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config.default_config()
            data.update({"voice_trigger_mode": "hold", "voice_hotkey": "ralt+space"})
            config.save_config(path, data)
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "hold")
        self.assertEqual(loaded["voice_hotkey"], "ralt+space")

    def test_save_preserves_the_typeless_right_alt_edge_tap_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config.default_config()
            data.update({"voice_trigger_mode": "typeless", "voice_hotkey": "ralt"})
            config.save_config(path, data)
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "typeless")
        self.assertEqual(loaded["voice_hotkey"], "ralt")

    def test_save_preserves_typeless_start_only_diagnostic_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config.default_config()
            data.update(
                {
                    "voice_trigger_mode": "typeless_start_only",
                    "voice_hotkey": "ralt",
                }
            )
            config.save_config(path, data)
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "typeless_start_only")
        self.assertEqual(loaded["voice_hotkey"], "ralt")

    def test_load_preserves_a_user_custom_voice_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "toggle", "voice_hotkey": "win+h"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "win+h")

    def test_load_preserves_recorded_left_ctrl_win_without_inferring_a_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {"voice_trigger_mode": "toggle", "voice_hotkey": "lctrl+lwin"}
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "toggle")
        self.assertEqual(loaded["voice_hotkey"], "lctrl+lwin")

    def test_load_preserves_left_alt_when_the_user_selected_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "hold", "voice_hotkey": "lalt"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "hold")
        self.assertEqual(loaded["voice_hotkey"], "lalt")


class SaveConfigPrivacyGuardTests(unittest.TestCase):
    def test_save_config_rejects_forbidden_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["address"] = "AA:BB:CC:DD:EE:FF"
            with self.assertRaises(config.ConfigPrivacyError):
                config.save_config(path, bad_config)
            self.assertFalse(path.exists())

    def test_save_key_bindings_rejects_device_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            bad_bindings = config.default_key_bindings()
            bad_bindings["device_token"] = "aabbccddeeff"
            with self.assertRaises(config.ConfigPrivacyError):
                config.save_key_bindings(path, bad_bindings)

    def test_load_config_rejects_a_forbidden_key_found_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"interface_id": "abc"}), encoding="utf-8")
            with self.assertRaises(config.ConfigPrivacyError):
                config.load_config(path)

    # -- recursive guard (XRBM-014 review RETRY P1 #6): a forbidden key must
    #    be refused no matter how deeply it is nested inside dicts and
    #    dicts-inside-lists, not just at the top level. --------------------

    def test_rejects_forbidden_key_nested_two_levels_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            bad_bindings = config.default_key_bindings()
            bad_bindings["bindings"]["menu"] = {
                "kind": "key_combo",
                "keys": ["a"],
                "metadata": {"address": "AA:BB:CC:DD:EE:FF"},
            }
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_key_bindings(path, bad_bindings)
            self.assertIn("bindings.menu.metadata.address", str(ctx.exception))
            self.assertFalse(path.exists())

    def test_rejects_forbidden_key_nested_inside_a_list_of_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["history"] = [
                {"note": "fine"},
                {"device_token": "aabbccddeeff"},
            ]
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_config(path, bad_config)
            self.assertIn("history[1].device_token", str(ctx.exception))

    def test_rejects_forbidden_key_nested_three_levels_deep_in_mixed_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["profiles"] = [
                {"devices": [{"bt_address": "AA:BB:CC:DD:EE:FF"}]},
            ]
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_config(path, bad_config)
            self.assertIn("profiles[0].devices[0].bt_address", str(ctx.exception))

    def test_deeply_nested_forbidden_key_found_on_load_from_disk_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"a": {"b": [{"mac_address": "AA:BB:CC:DD:EE:FF"}]}}),
                encoding="utf-8",
            )
            with self.assertRaises(config.ConfigPrivacyError):
                config.load_config(path)

    def test_deeply_nested_structure_without_a_forbidden_key_saves_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            good_config = config.default_config()
            good_config["profiles"] = [{"devices": [{"friendly_name": "Speakers"}]}]
            config.save_config(path, good_config)  # must not raise
            self.assertTrue(path.exists())


class RoundTripTests(unittest.TestCase):
    def test_save_config_replaces_an_existing_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            updated = config.default_config()
            updated["gain_db"] = 4.0

            config.save_config(path, updated)

            self.assertEqual(config.load_config(path)["gain_db"], 4.0)
            self.assertEqual(list(path.parent.glob(".config.json.*.tmp")), [])

    def test_failed_atomic_replace_does_not_leave_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            with mock.patch.object(config.os, "replace", side_effect=OSError("locked")):
                with self.assertRaisesRegex(OSError, "locked"):
                    config.save_config(path, config.default_config())

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(path.parent.glob(".config.json.*.tmp")), [])

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = config.default_config()
            original["gain_db"] = 3.5
            config.save_config(path, original)
            loaded = config.load_config(path)
            self.assertEqual(loaded["gain_db"], 3.5)

    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.json"
            loaded = config.load_config(path)
            self.assertEqual(loaded, config.default_config())

    def test_key_bindings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            original = config.default_key_bindings()
            config.save_key_bindings(path, original)
            loaded = config.load_key_bindings(path)
            self.assertEqual(loaded["bindings"], original["bindings"])

    def test_legacy_reference_chords_are_migrated_to_semantic_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "bindings": {
                            "up": {"kind": "key_combo", "keys": ["up"]},
                            "home": {
                                "kind": "key_combo",
                                "keys": ["win", "d"],
                            },
                            "tv": {
                                "kind": "key_combo",
                                "keys": ["alt", "esc"],
                            },
                            "power": {
                                "kind": "key_combo",
                                "keys": ["ctrl", "shift", "p"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            loaded["bindings"]["up"]["kind"],
            key_mapping.ActionKind.ARROW_UP.value,
        )
        self.assertEqual(
            loaded["bindings"]["home"]["kind"],
            key_mapping.ActionKind.SHOW_DESKTOP.value,
        )
        self.assertEqual(
            loaded["bindings"]["tv"]["kind"],
            key_mapping.ActionKind.APP_SWITCHER.value,
        )
        self.assertEqual(
            loaded["bindings"]["power"],
            {"kind": "key_combo", "keys": ["ctrl", "shift", "p"]},
        )

    def test_old_text_submit_actions_migrate_to_input_without_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "bindings": {
                            "power": {
                                "kind": "type_text_and_submit",
                                "keys": [],
                                "text": "继续",
                            },
                            "menu": {
                                "kind": "type_execute_and_submit",
                                "keys": [],
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            loaded["bindings"]["power"],
            {"kind": "type_text", "keys": [], "text": "继续"},
        )
        self.assertEqual(
            loaded["bindings"]["menu"],
            {"kind": "type_text", "keys": [], "text": "执行"},
        )

    def test_text_menu_items_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            original = config.default_key_bindings()
            original = config.create_mapping_profile(original, "工作方案")
            original["text_menu_items"] = [
                {"label": "总结", "text": "请总结上述内容", "enabled": True},
                {"label": "继续", "text": "继续", "enabled": False},
            ]
            original = config.update_active_mapping_profile(original)
            config.save_key_bindings(path, original)

            loaded = config.load_key_bindings(path)

        self.assertEqual(loaded["text_menu_items"], original["text_menu_items"])

    def test_legacy_mapping_file_is_preserved_beside_the_system_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "bindings": {
                            "power": {"kind": "key_combo", "keys": ["f8"]}
                        },
                        "secondary_bindings": {},
                        "text_menu_items": [
                            {"label": "继续", "text": "继续", "enabled": True}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(loaded["active_mapping_profile_id"], "legacy_default")
        self.assertEqual(
            [profile["name"] for profile in loaded["mapping_profiles"]],
            ["系统默认方案", "原有配置"],
        )
        self.assertEqual(
            loaded["mapping_profiles"][1]["bindings"]["power"],
            {"kind": "key_combo", "keys": ["f8"]},
        )
        self.assertEqual(
            loaded["mapping_profiles"][1]["text_menu_items"],
            loaded["text_menu_items"],
        )

    def test_modified_old_default_profile_is_preserved_as_original_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            old = config._factory_mapping_profile_snapshot()
            old["bindings"]["power"] = {"kind": "key_combo", "keys": ["f9"]}
            path.write_text(
                json.dumps(
                    {
                        **old,
                        "active_mapping_profile_id": "default",
                        "mapping_profiles": [
                            {"id": "default", "name": "默认方案", **old}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            [profile["name"] for profile in loaded["mapping_profiles"]],
            ["系统默认方案", "原有配置"],
        )
        self.assertEqual(loaded["bindings"]["power"]["keys"], ["f9"])
        self.assertEqual(loaded["active_mapping_profile_id"], "default")

    def test_mapping_profiles_support_save_as_switch_rename_and_delete(self):
        original = config.default_key_bindings()
        original_power = original["bindings"]["power"]
        original["physical_bindings"] = {"keyboard:vkey=0x70": "power"}
        first_user = config.create_mapping_profile(original, "基础")
        edited = json.loads(json.dumps(first_user))
        edited["bindings"]["power"] = {"kind": "key_combo", "keys": ["f8"]}
        edited["text_menu_items"] = [
            {"label": "审查", "text": "请审查以上改动", "enabled": True}
        ]

        created = config.create_mapping_profile(edited, "代码审查")

        self.assertEqual(
            created["mapping_profiles"][0]["bindings"]["power"], original_power
        )
        self.assertEqual(
            created["mapping_profiles"][1]["bindings"]["power"], original_power
        )
        self.assertEqual(created["bindings"]["power"]["keys"], ["f8"])
        self.assertEqual(
            created["physical_bindings"], {"keyboard:vkey=0x70": "power"}
        )
        new_id = created["active_mapping_profile_id"]
        renamed = config.rename_mapping_profile(created, new_id, "Vibe Coding")
        self.assertEqual(
            [profile["name"] for profile in config.mapping_profile_summaries(renamed)],
            ["系统默认方案", "基础", "Vibe Coding"],
        )

        switched = config.activate_mapping_profile(
            renamed, config.SYSTEM_MAPPING_PROFILE_ID
        )
        self.assertEqual(switched["bindings"]["power"], original_power)
        self.assertEqual(
            switched["text_menu_items"], original["text_menu_items"]
        )
        self.assertEqual(
            switched["physical_bindings"], {"keyboard:vkey=0x70": "power"}
        )

        deleted = config.delete_mapping_profile(switched, new_id)
        self.assertEqual(
            [profile["name"] for profile in config.mapping_profile_summaries(deleted)],
            ["系统默认方案", "基础"],
        )

    def test_system_profile_cannot_be_duplicated_renamed_or_deleted(self):
        bindings = config.default_key_bindings()
        with self.assertRaisesRegex(config.MappingProfileError, "同名"):
            config.create_mapping_profile(bindings, " 系统默认方案 ")
        with self.assertRaisesRegex(config.MappingProfileError, "不能重命名"):
            config.rename_mapping_profile(
                bindings, config.SYSTEM_MAPPING_PROFILE_ID, "其他名称"
            )
        with self.assertRaisesRegex(config.MappingProfileError, "不能删除"):
            config.delete_mapping_profile(
                bindings, config.SYSTEM_MAPPING_PROFILE_ID
            )

    def test_system_profile_content_cannot_be_overwritten(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["power"] = {
            "kind": "key_combo",
            "keys": ["f8"],
        }
        bindings["mapping_profiles"][0]["bindings"]["power"] = {
            "kind": "key_combo",
            "keys": ["f9"],
        }

        normalized = config.update_active_mapping_profile(bindings)

        expected = config.default_key_bindings()["bindings"]["power"]
        self.assertEqual(normalized["bindings"]["power"], expected)
        self.assertEqual(
            normalized["mapping_profiles"][0]["bindings"]["power"], expected
        )

    def test_last_user_profile_can_be_deleted_and_falls_back_to_system_default(self):
        bindings = config.create_mapping_profile(
            config.default_key_bindings(), "临时方案"
        )
        user_id = bindings["active_mapping_profile_id"]
        deleted = config.delete_mapping_profile(bindings, user_id)

        self.assertEqual(deleted["active_mapping_profile_id"], "system_default")
        self.assertEqual(
            [profile["name"] for profile in config.mapping_profile_summaries(deleted)],
            ["系统默认方案"],
        )


class MicBindingTruthfulnessTests(unittest.TestCase):
    """XRBM-019 In-scope item 6: the physical mic button is always driven
    directly by the ATVV voice lifecycle - the runtime never consults a
    stored "mic" binding. load_key_bindings() must normalize a stale
    non-voice "mic" entry back to voice, not silently keep it around
    looking like it does something (XRBM-018's independent review
    round 2 product-contract follow-up).
    """

    def test_a_stale_non_voice_mic_binding_on_disk_is_normalized_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stale = config.default_key_bindings()
            # Simulates a legacy file (or a hand-edited one) that saved an
            # ordinary key-combo for "mic" - something the runtime has
            # never actually honored.
            stale["bindings"]["mic"] = {"kind": "key_combo", "keys": ["a"]}
            path.write_text(json.dumps(stale), encoding="utf-8")

            loaded = config.load_key_bindings(path)

            self.assertEqual(loaded["bindings"]["mic"], {"kind": "voice", "keys": []})

    def test_a_missing_mic_binding_on_disk_is_filled_in_as_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stale = config.default_key_bindings()
            del stale["bindings"]["mic"]
            path.write_text(json.dumps(stale), encoding="utf-8")

            loaded = config.load_key_bindings(path)

            self.assertEqual(loaded["bindings"]["mic"], {"kind": "voice", "keys": []})

    def test_default_key_bindings_mic_is_already_voice(self):
        self.assertEqual(
            config.default_key_bindings()["bindings"]["mic"], {"kind": "voice", "keys": []}
        )


if __name__ == "__main__":
    unittest.main()
