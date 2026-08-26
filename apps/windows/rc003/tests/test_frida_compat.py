import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_compat
from ovb_rc003 import frida_hid_tap_injector


class AssetDescriptorTests(unittest.TestCase):
    def test_uses_official_release_url(self):
        self.assertTrue(
            frida_compat.FRIDA_GADGET.url.startswith(
                "https://github.com/frida/frida/releases/download/"
            )
        )

    def test_sha256_is_pinned_and_well_formed(self):
        self.assertEqual(len(frida_compat.FRIDA_GADGET.sha256), 64)
        int(frida_compat.FRIDA_GADGET.sha256, 16)


class VerifyAssetTests(unittest.TestCase):
    def test_false_when_missing(self):
        missing = Path("/nonexistent/frida-gadget.dll.xz")
        self.assertFalse(frida_compat.verify_asset(missing, frida_compat.FRIDA_GADGET))

    def test_false_when_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            path.write_bytes(b"not the real gadget")
            self.assertFalse(frida_compat.verify_asset(path, frida_compat.FRIDA_GADGET))

    def test_true_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=digest,
                license_name="x",
                license_url="https://example.invalid/license",
            )
            self.assertTrue(frida_compat.verify_asset(path, asset))


class ReportDecodeTests(unittest.TestCase):
    def test_decodes_verified_hidogatt_buffer(self):
        self.assertEqual(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("010000f10080008100")
            ),
            bytes.fromhex("f10080008100"),
        )

    def test_rejects_wrong_prefix_or_length(self):
        self.assertIsNone(frida_compat.decode_rc003_ioctl_output(b"\x01\x00\x00"))
        self.assertIsNone(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("020000f10080008100")
            )
        )

    def test_extracts_nonzero_little_endian_usages(self):
        self.assertEqual(
            frida_compat.payload_usages(bytes.fromhex("f10000008100")),
            {0xF1, 0x81},
        )
        self.assertEqual(frida_compat.payload_usages(b"short"), set())


class ReportTapTests(unittest.TestCase):
    def test_emits_only_edges_for_missing_usages(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10080008100"))
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        self.assertEqual(
            reports,
            [
                (1, bytes.fromhex("80008100f100")),
                (1, bytes.fromhex("f10000000000")),
            ],
        )

    def test_only_verified_report_shape_counts_as_io(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            enabled=False,
        )
        self.assertFalse(tap._handle_ioctl_output(b"wrong"))
        self.assertTrue(
            tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        )

    def test_releases_active_usages_when_stopped(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        tap._release_active()
        self.assertEqual(reports[-1], (1, b"\x00" * 6))

    def test_missing_gadget_degrades_without_starting(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            archive_path=Path("/nonexistent/frida-gadget.dll.xz"),
            enabled=True,
        )
        self.assertFalse(tap.available)
        self.assertIn("unavailable", tap.status)
        self.assertFalse(tap.start())

    def test_compatibility_name_accepts_custom_verified_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=hashlib.sha256(content).hexdigest(),
                license_name="x",
                license_url="https://example.invalid/license",
            )
            layer = frida_compat.BackKeyCompatLayer(gadget_path=path, asset=asset)
            self.assertTrue(layer.available)
            self.assertEqual(layer.status, "ready_gadget_verified")


class ElevatedInjectorLaunchTests(unittest.TestCase):
    def test_source_command_uses_hidden_module_entrypoint(self):
        executable, parameters, directory = frida_hid_tap_injector.injector_command(42)
        self.assertTrue(executable.lower().endswith("python.exe"))
        self.assertIn("-m ovb_rc003", parameters)
        self.assertIn("--rc003-hid-injector --pid 42", parameters)
        self.assertEqual(directory, str(Path(executable).parent))

    def test_frozen_command_reuses_packaged_executable(self):
        with mock.patch.object(
            frida_hid_tap_injector.sys, "frozen", True, create=True
        ):
            _executable, parameters, _directory = (
                frida_hid_tap_injector.injector_command(43)
            )
        self.assertNotIn("-m ovb_rc003", parameters)
        self.assertEqual(parameters, "--rc003-hid-injector --pid 43")

    def test_launch_waits_for_successful_helper_exit(self):
        calls = []

        def run_elevated(executable, parameters, directory, timeout_ms):
            calls.append((executable, parameters, directory, timeout_ms))
            return 0

        self.assertTrue(
            frida_hid_tap_injector.launch_elevated_injector(
                44, timeout_ms=1234, _run_elevated=run_elevated
            )
        )
        self.assertEqual(calls[0][3], 1234)
        self.assertIn("--pid 44", calls[0][1])

    def test_nonzero_helper_exit_is_not_reported_as_injected(self):
        with self.assertRaisesRegex(RuntimeError, "exit code 5"):
            frida_hid_tap_injector.launch_elevated_injector(
                45,
                _run_elevated=lambda *_args: 5,
            )

    def test_rejects_invalid_pid_before_uac(self):
        with self.assertRaises(ValueError):
            frida_hid_tap_injector.injector_command(0)

    def test_debug_privilege_precedes_protected_process_name_query(self):
        events = []

        with (
            mock.patch.object(
                frida_hid_tap_injector,
                "find_rc003_hidogatt_host_pid",
                return_value=46,
            ),
            mock.patch.object(
                frida_hid_tap_injector,
                "enable_debug_privilege",
                side_effect=lambda: events.append("debug_privilege"),
            ),
            mock.patch.object(
                frida_hid_tap_injector,
                "_target_process_name",
                side_effect=lambda _pid: events.append("process_name") or "wudfhost.exe",
            ),
            mock.patch.object(
                frida_hid_tap_injector,
                "prepare_secure_runtime",
                return_value=Path("verified-gadget.dll"),
            ),
            mock.patch.object(
                frida_hid_tap_injector,
                "sha256_file",
                return_value=frida_hid_tap_injector.GADGET_DLL_SHA256,
            ),
            mock.patch.object(frida_hid_tap_injector, "inject_library"),
        ):
            frida_hid_tap_injector.inject_current_process(46)

        self.assertEqual(events, ["debug_privilege", "process_name"])

    def test_hidden_helper_logs_failure_instead_of_raising_gui_exception(self):
        logger = mock.Mock()
        with (
            mock.patch.object(
                frida_hid_tap_injector,
                "inject_current_process",
                side_effect=PermissionError("denied"),
            ),
            mock.patch.object(
                frida_hid_tap_injector.logging_setup,
                "get_logger",
                return_value=logger,
            ),
        ):
            self.assertEqual(frida_hid_tap_injector.main(["--pid", "46"]), 1)
        logger.exception.assert_called_once()


if __name__ == "__main__":
    unittest.main()
