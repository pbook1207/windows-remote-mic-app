"""Read a modern Windows text caret through UI Automation.

Chromium/WebView and other windowless editors do not create a real Win32
caret. ``GetGUIThreadInfo`` can therefore report a zero-sized placeholder at
the host window's origin even while a visible insertion caret is near the
bottom of the window. UI Automation TextPattern2 exposes the actual caret
range used by those editors.

This module uses the Windows COM ABI directly through ``ctypes`` so the
portable build does not need an additional automation package.
"""

from __future__ import annotations

import ctypes
import math
import sys
import uuid
from ctypes import wintypes
from typing import Optional, Sequence


_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = -2147417850
_UIA_TEXT_PATTERN_ID = 10014
_UIA_TEXT_PATTERN2_ID = 10024
_TEXT_UNIT_CHARACTER = 0


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def from_string(cls, value: str) -> "_GUID":
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


_CLSID_CUIAUTOMATION = _GUID.from_string(
    "ff48dba4-60ef-4201-aa87-54103eef594e"
)
_IID_IUIAUTOMATION = _GUID.from_string(
    "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
)
_IID_IUIAUTOMATION_TEXTPATTERN2 = _GUID.from_string(
    "506a921a-fcc9-409f-b23b-37eb74106872"
)
_IID_IUIAUTOMATION_TEXTPATTERN = _GUID.from_string(
    "32eba289-3583-42c9-9c59-3b6d9a1e9b6a"
)


def _failed(hresult: int) -> bool:
    return int(hresult) < 0


def _com_method(
    interface: ctypes.c_void_p,
    index: int,
    restype,
    *argtypes,
):
    vtable = ctypes.cast(
        interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    address = vtable[index]
    return ctypes.WINFUNCTYPE(
        restype, ctypes.c_void_p, *argtypes
    )(address)


def _release(interface: ctypes.c_void_p) -> None:
    if not interface:
        return
    try:
        _com_method(interface, 2, wintypes.ULONG)(interface)
    except (OSError, ValueError):
        pass


def _read_double_safearray(
    oleaut32,
    safearray: ctypes.c_void_p,
) -> list[float]:
    if not safearray:
        return []
    lower = ctypes.c_long()
    upper = ctypes.c_long()
    if oleaut32.SafeArrayGetDim(safearray) != 1:
        return []
    if _failed(oleaut32.SafeArrayGetLBound(safearray, 1, ctypes.byref(lower))):
        return []
    if _failed(oleaut32.SafeArrayGetUBound(safearray, 1, ctypes.byref(upper))):
        return []
    count = max(0, int(upper.value - lower.value + 1))
    raw_data = ctypes.c_void_p()
    if _failed(oleaut32.SafeArrayAccessData(safearray, ctypes.byref(raw_data))):
        return []
    try:
        data = ctypes.cast(raw_data, ctypes.POINTER(ctypes.c_double))
        return [float(data[index]) for index in range(count)]
    finally:
        oleaut32.SafeArrayUnaccessData(safearray)


def _point_from_bounding_rectangles(
    values: Sequence[float],
) -> Optional[tuple[int, int]]:
    """Return the first valid UIA rectangle's upper-left screen point."""

    for index in range(0, len(values) - 3, 4):
        left, top, width, height = values[index : index + 4]
        if not all(math.isfinite(value) for value in (left, top, width, height)):
            continue
        if width < 0 or height < 0:
            continue
        return int(round(left)), int(round(top))
    return None


def text_caret_physical_position() -> Optional[tuple[int, int]]:
    """Return the focused UIA editor's caret in physical screen pixels."""

    if sys.platform != "win32":
        return None
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    oleaut32 = ctypes.windll.oleaut32  # type: ignore[attr-defined]
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = ()
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_long
    oleaut32.SafeArrayGetDim.argtypes = (ctypes.c_void_p,)
    oleaut32.SafeArrayGetDim.restype = wintypes.UINT
    oleaut32.SafeArrayGetLBound.argtypes = (
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_long),
    )
    oleaut32.SafeArrayGetLBound.restype = ctypes.c_long
    oleaut32.SafeArrayGetUBound.argtypes = oleaut32.SafeArrayGetLBound.argtypes
    oleaut32.SafeArrayGetUBound.restype = ctypes.c_long
    oleaut32.SafeArrayAccessData.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    oleaut32.SafeArrayAccessData.restype = ctypes.c_long
    oleaut32.SafeArrayUnaccessData.argtypes = (ctypes.c_void_p,)
    oleaut32.SafeArrayUnaccessData.restype = ctypes.c_long
    oleaut32.SafeArrayDestroy.argtypes = (ctypes.c_void_p,)
    oleaut32.SafeArrayDestroy.restype = ctypes.c_long

    initialized = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if _failed(initialized) and int(initialized) != _RPC_E_CHANGED_MODE:
        return None
    should_uninitialize = not _failed(initialized)
    automation = ctypes.c_void_p()
    element = ctypes.c_void_p()
    pattern = ctypes.c_void_p()
    range_array = ctypes.c_void_p()
    text_range = ctypes.c_void_p()
    safearray = ctypes.c_void_p()
    try:
        result = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_CUIAUTOMATION),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_IUIAUTOMATION),
            ctypes.byref(automation),
        )
        if _failed(result) or not automation:
            return None
        # IUIAutomation::GetFocusedElement (IUnknown's 3 entries + method 5).
        result = _com_method(
            automation,
            8,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_void_p),
        )(automation, ctypes.byref(element))
        if _failed(result) or not element:
            return None
        # IUIAutomationElement::GetCurrentPatternAs.
        result = _com_method(
            element,
            14,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(
            element,
            _UIA_TEXT_PATTERN2_ID,
            ctypes.byref(_IID_IUIAUTOMATION_TEXTPATTERN2),
            ctypes.byref(pattern),
        )
        if not _failed(result) and pattern:
            is_active = wintypes.BOOL()
            # IUIAutomationTextPattern2::GetCaretRange.
            result = _com_method(
                pattern,
                10,
                ctypes.c_long,
                ctypes.POINTER(wintypes.BOOL),
                ctypes.POINTER(ctypes.c_void_p),
            )(pattern, ctypes.byref(is_active), ctypes.byref(text_range))
            if _failed(result):
                text_range.value = None
        else:
            pattern.value = None

        if not text_range:
            # Older Chromium/WebView providers may expose only TextPattern.
            # Its current selection is a collapsed range at the insertion
            # point when there is no selected text.
            if not pattern:
                result = _com_method(
                    element,
                    14,
                    ctypes.c_long,
                    ctypes.c_int,
                    ctypes.POINTER(_GUID),
                    ctypes.POINTER(ctypes.c_void_p),
                )(
                    element,
                    _UIA_TEXT_PATTERN_ID,
                    ctypes.byref(_IID_IUIAUTOMATION_TEXTPATTERN),
                    ctypes.byref(pattern),
                )
                if _failed(result) or not pattern:
                    return None
            # IUIAutomationTextPattern::GetSelection.
            result = _com_method(
                pattern,
                5,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p),
            )(pattern, ctypes.byref(range_array))
            if _failed(result) or not range_array:
                return None
            length = ctypes.c_int()
            result = _com_method(
                range_array,
                3,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_int),
            )(range_array, ctypes.byref(length))
            if _failed(result) or length.value < 1:
                return None
            result = _com_method(
                range_array,
                4,
                ctypes.c_long,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            )(range_array, 0, ctypes.byref(text_range))
            if _failed(result) or not text_range:
                return None

        def bounding_point() -> Optional[tuple[int, int]]:
            safearray.value = None
            result = _com_method(
                text_range,
                10,
                ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p),
            )(text_range, ctypes.byref(safearray))
            if _failed(result) or not safearray:
                return None
            try:
                return _point_from_bounding_rectangles(
                    _read_double_safearray(oleaut32, safearray)
                )
            finally:
                oleaut32.SafeArrayDestroy(safearray)
                safearray.value = None

        point = bounding_point()
        if point is not None:
            return point
        # UIA permits an empty rectangle array for a collapsed range. Expand
        # only the temporary caret range to its enclosing character and retry.
        result = _com_method(
            text_range,
            6,
            ctypes.c_long,
            ctypes.c_int,
        )(text_range, _TEXT_UNIT_CHARACTER)
        if _failed(result):
            return None
        return bounding_point()
    except (AttributeError, OSError, ValueError):
        return None
    finally:
        if safearray:
            oleaut32.SafeArrayDestroy(safearray)
        _release(text_range)
        _release(range_array)
        _release(pattern)
        _release(element)
        _release(automation)
        if should_uninitialize:
            ole32.CoUninitialize()
