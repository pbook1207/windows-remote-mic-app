import unittest
from unittest import mock

from ovb_rc003 import key_mapping, text_menu_overlay


class _Backend:
    def __init__(self):
        self.shown = []
        self.hide_calls = 0
        self.restored = []
        self.shutdown_calls = 0
        self.dismiss_callback = None
        self.select_callback = None

    def set_dismiss_callback(self, callback):
        self.dismiss_callback = callback

    def set_select_callback(self, callback):
        self.select_callback = callback

    def foreground_window(self):
        return 42

    def show(self, items, selected_index):
        self.shown.append((tuple(items), selected_index))

    def hide(self):
        self.hide_calls += 1

    def restore_foreground(self, hwnd):
        self.restored.append(hwnd)

    def shutdown(self):
        self.shutdown_calls += 1

    def dismiss_from_mouse(self):
        self.dismiss_callback()

    def select_from_mouse(self, index):
        self.select_callback(index)


class TextMenuOverlayTests(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        self.menu = text_menu_overlay.TextMenuOverlay(self.backend)
        self.items = (
            key_mapping.TextMenuItem("执行", "执行"),
            key_mapping.TextMenuItem("禁用项", "不会出现", enabled=False),
            key_mapping.TextMenuItem("总结", "请总结上述内容"),
        )

    def test_toggle_opens_without_disabled_items_and_second_toggle_closes(self):
        self.assertTrue(self.menu.toggle(self.items))
        self.assertTrue(self.menu.is_open)
        rendered_items, selected = self.backend.shown[-1]
        self.assertEqual([item.label for item in rendered_items], ["执行", "总结"])
        self.assertEqual(selected, 0)

        self.assertFalse(self.menu.toggle(self.items))
        self.assertFalse(self.menu.is_open)

    def test_move_wraps_and_confirm_returns_selected_text(self):
        self.menu.toggle(self.items)
        self.menu.move(-1)

        self.assertEqual(self.menu.selected_index, 1)
        self.assertEqual(self.backend.shown[-1][1], 1)
        self.assertEqual(self.menu.confirm(), "请总结上述内容")
        self.assertFalse(self.menu.is_open)
        self.assertEqual(self.backend.restored, [42])

    def test_empty_or_fully_disabled_menu_does_not_open(self):
        self.assertFalse(self.menu.toggle(()))
        self.assertFalse(
            self.menu.toggle(
                (key_mapping.TextMenuItem("停用", "文本", enabled=False),)
            )
        )
        self.assertFalse(self.menu.is_open)

    def test_shutdown_closes_and_releases_backend(self):
        self.menu.toggle(self.items)
        self.menu.shutdown()

        self.assertFalse(self.menu.is_open)
        self.assertEqual(self.backend.shutdown_calls, 1)

    def test_mouse_dismiss_synchronizes_the_menu_state(self):
        self.menu.toggle(self.items)

        self.backend.dismiss_from_mouse()

        self.assertFalse(self.menu.is_open)
        self.assertIsNone(self.menu.confirm())

    def test_mouse_selection_returns_the_clicked_items_actual_text(self):
        selected_text = []
        menu = text_menu_overlay.TextMenuOverlay(
            self.backend,
            on_select=selected_text.append,
        )
        menu.toggle(self.items)

        self.backend.select_from_mouse(1)

        self.assertEqual(selected_text, ["请总结上述内容"])
        self.assertFalse(menu.is_open)
        self.assertEqual(self.backend.restored, [42])

    def test_mouse_selection_uses_the_visible_pages_absolute_index(self):
        selected_text = []
        menu = text_menu_overlay.TextMenuOverlay(
            self.backend,
            on_select=selected_text.append,
        )
        items = tuple(
            key_mapping.TextMenuItem(f"项目 {index}", f"文本 {index}")
            for index in range(12)
        )
        menu.toggle(items)
        for _ in range(10):
            menu.move(1)

        self.backend.select_from_mouse(6)

        self.assertEqual(selected_text, ["文本 10"])
        self.assertFalse(menu.is_open)

    def test_long_menu_sends_an_eight_item_page_around_the_selection(self):
        items = tuple(
            key_mapping.TextMenuItem(f"项目 {index}", f"文本 {index}")
            for index in range(12)
        )
        self.menu.toggle(items)
        for _ in range(10):
            self.menu.move(1)

        visible, selected = self.backend.shown[-1]
        self.assertEqual([item.label for item in visible], [f"项目 {i}" for i in range(4, 12)])
        self.assertEqual(selected, 6)

    def test_overlay_command_uses_module_mode_for_source_runs(self):
        with mock.patch.object(text_menu_overlay.sys, "executable", "python.exe"):
            with mock.patch.object(text_menu_overlay.sys, "frozen", False, create=True):
                self.assertEqual(
                    text_menu_overlay._overlay_command(),
                    ["python.exe", "-m", "ovb_rc003", "--text-menu-overlay"],
                )

    def test_overlay_command_reuses_the_packaged_executable_when_frozen(self):
        with mock.patch.object(text_menu_overlay.sys, "executable", "RemoteMicRC003.exe"):
            with mock.patch.object(text_menu_overlay.sys, "frozen", True, create=True):
                self.assertEqual(
                    text_menu_overlay._overlay_command(),
                    ["RemoteMicRC003.exe", "--text-menu-overlay"],
                )

    def test_binary_pipe_message_is_explicitly_decoded_as_utf8(self):
        raw = (
            '{"visible":true,"items":[{"label":"细画","text":"细画练习"}]}'
        ).encode("utf-8")

        message = text_menu_overlay._parse_overlay_message_line(raw)

        self.assertEqual(message["items"][0]["label"], "细画")
        self.assertEqual(message["items"][0]["text"], "细画练习")

    def test_invalid_pipe_encoding_is_ignored(self):
        self.assertIsNone(text_menu_overlay._parse_overlay_message_line(b"\xff\xfe"))

    def test_panel_is_above_and_left_aligned_with_cursor(self):
        self.assertEqual(
            text_menu_overlay._panel_position_above_cursor(
                600, 500, 0, 0, 1920, 1080, 164, 158
            ),
            (600, 334),
        )

    def test_panel_alignment_is_clamped_at_screen_edges(self):
        self.assertEqual(
            text_menu_overlay._panel_position_above_cursor(
                1900, 100, 0, 0, 1920, 1080, 164, 158
            ),
            (1748, 8),
        )

    def test_backend_reads_the_foreground_text_caret_in_screen_coordinates(self):
        class FakeUser32:
            def GetForegroundWindow(self):
                return 81

            def GetWindowThreadProcessId(self, hwnd, process_id):
                self.hwnd = hwnd
                self.process_id = process_id
                return 19

            def GetGUIThreadInfo(self, thread_id, info_pointer):
                self.thread_id = thread_id
                info = info_pointer._obj
                info.hwndCaret = 82
                info.rcCaret.left = 14
                info.rcCaret.top = 27
                info.rcCaret.right = 15
                info.rcCaret.bottom = 47
                return True

            def ClientToScreen(self, hwnd, point_pointer):
                self.caret_hwnd = hwnd
                point = point_pointer._obj
                point.x += 500
                point.y += 300
                return True

            def PhysicalToLogicalPointForPerMonitorDPI(self, hwnd, point_pointer):
                raise AssertionError("legacy caret should not need DPI conversion")

        backend = text_menu_overlay._TextMenuProcessBackend.__new__(
            text_menu_overlay._TextMenuProcessBackend
        )
        backend._user32 = FakeUser32()

        with mock.patch.object(
            text_menu_overlay.uia_caret_windows,
            "text_caret_physical_position",
            return_value=None,
        ):
            self.assertEqual(backend.text_caret_position(), (514, 327))
        self.assertEqual(backend._user32.thread_id, 19)
        self.assertEqual(backend._user32.caret_hwnd, 82)

    def test_backend_prefers_modern_uia_caret_and_converts_for_qt(self):
        class FakeUser32:
            def GetForegroundWindow(self):
                return 81

            def PhysicalToLogicalPointForPerMonitorDPI(self, hwnd, point_pointer):
                self.hwnd = hwnd
                point = point_pointer._obj
                point.x //= 2
                point.y //= 2
                return True

            def GetWindowThreadProcessId(self, hwnd, process_id):
                raise AssertionError("valid UIA caret should bypass legacy lookup")

        backend = text_menu_overlay._TextMenuProcessBackend.__new__(
            text_menu_overlay._TextMenuProcessBackend
        )
        backend._user32 = FakeUser32()

        with mock.patch.object(
            text_menu_overlay.uia_caret_windows,
            "text_caret_physical_position",
            return_value=(1800, 1200),
        ):
            self.assertEqual(backend.text_caret_position(), (900, 600))
        self.assertEqual(backend._user32.hwnd, 81)

    def test_zero_height_legacy_placeholder_caret_is_rejected(self):
        class FakeUser32:
            def GetForegroundWindow(self):
                return 81

            def GetWindowThreadProcessId(self, hwnd, process_id):
                return 19

            def GetGUIThreadInfo(self, thread_id, info_pointer):
                info = info_pointer._obj
                info.hwndCaret = 81
                info.rcCaret.left = 0
                info.rcCaret.top = 0
                info.rcCaret.right = 0
                info.rcCaret.bottom = 0
                return True

        backend = text_menu_overlay._TextMenuProcessBackend.__new__(
            text_menu_overlay._TextMenuProcessBackend
        )
        backend._user32 = FakeUser32()
        with mock.patch.object(
            text_menu_overlay.uia_caret_windows,
            "text_caret_physical_position",
            return_value=None,
        ):
            self.assertIsNone(backend.text_caret_position())

    def test_uia_rectangle_parser_returns_first_valid_caret_point(self):
        self.assertEqual(
            text_menu_overlay.uia_caret_windows._point_from_bounding_rectangles(
                [float("nan"), 2.0, 0.0, 20.0, 1640.4, 1875.6, 1.0, 32.0]
            ),
            (1640, 1876),
        )

    def test_overlay_child_no_longer_reads_the_mouse_cursor_for_positioning(self):
        source = text_menu_overlay.Path(text_menu_overlay.__file__).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("QCursor.pos()", source)
        self.assertIn('message["caretX"]', source)


if __name__ == "__main__":
    unittest.main()
