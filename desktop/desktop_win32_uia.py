"""Win32 ctypes, clipboard, window enumeration, and PrintWindow capture helpers for desktop_agent."""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import io
import sys
from pathlib import Path
from typing import Any

IS_WIN = sys.platform == 'win32'

if IS_WIN:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.GetTickCount.argtypes = []
    kernel32.GetTickCount.restype = wt.DWORD
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wt.DWORD
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wt.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = wt.INT if hasattr(wt, 'INT') else ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.IsChild.argtypes = [wt.HWND, wt.HWND]
    user32.IsChild.restype = wt.BOOL
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindowRect.restype = wt.BOOL
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetClientRect.restype = wt.BOOL
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ClientToScreen.restype = wt.BOOL
    user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.ScreenToClient.restype = wt.BOOL
    user32.WindowFromPoint.argtypes = [wt.POINT]
    user32.WindowFromPoint.restype = wt.HWND
    user32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
    user32.ChildWindowFromPointEx.restype = wt.HWND
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.EnumChildWindows.argtypes = [wt.HWND, ctypes.c_void_p, wt.LPARAM]
    user32.EnumChildWindows.restype = wt.BOOL
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SendMessageW.restype = wt.LPARAM
    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wt.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wt.BOOL
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetWindowDC.argtypes = [wt.HWND]
    user32.GetWindowDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    user32.PrintWindow.restype = wt.BOOL

    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteObject.restype = wt.BOOL
    gdi32.DeleteDC.argtypes = [wt.HDC]
    gdi32.DeleteDC.restype = wt.BOOL

    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
else:
    user32 = None
    gdi32 = None
    kernel32 = None

WM_SETTEXT = 0x000C
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_COMMAND = 0x0111
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEWHEEL = 0x020A
BM_CLICK = 0x00F5
MK_LBUTTON = 0x0001
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_TAB = 0x09
VK_ESCAPE = 0x1B
PW_RENDERFULLCONTENT = 0x00000002
SW_SHOWNOACTIVATE = 4
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('dwTime', wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wt.DWORD),
        ('biWidth', wt.LONG),
        ('biHeight', wt.LONG),
        ('biPlanes', wt.WORD),
        ('biBitCount', wt.WORD),
        ('biCompression', wt.DWORD),
        ('biSizeImage', wt.DWORD),
        ('biXPelsPerMeter', wt.LONG),
        ('biYPelsPerMeter', wt.LONG),
        ('biClrUsed', wt.DWORD),
        ('biClrImportant', wt.DWORD),
    ]


def makelparam(lo: int, hi: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


def get_human_idle_seconds() -> float:
    if not IS_WIN:
        return 999.0
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        elapsed = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
        return round(elapsed / 1000.0, 2)
    return 999.0


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def win32_last_error() -> int:
    if not IS_WIN or kernel32 is None:
        return 0
    try:
        return int(kernel32.GetLastError() or 0)
    except Exception:
        return 0


def get_virtual_screen_rect() -> tuple[int, int, int, int]:
    """Return full multi-monitor DPI-aware virtual desktop rect (left, top, width, height)."""
    if not IS_WIN:
        return (0, 0, 1920, 1080)
    vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN) or 0)
    vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN) or 0)
    vw = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 0)
    vh = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 0)
    if vw <= 0 or vh <= 0:
        vw = int(user32.GetSystemMetrics(0) or 1920)
        vh = int(user32.GetSystemMetrics(1) or 1080)
        return (0, 0, vw, vh)
    return (vx, vy, vw, vh)


def get_screen_size() -> tuple[int, int]:
    if not IS_WIN:
        return (1920, 1080)
    _, _, vw, vh = get_virtual_screen_rect()
    return (vw, vh)


def _get_hwnd_text(hwnd: int) -> str:
    if not IS_WIN or not hwnd:
        return ''
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value.strip()


def _get_hwnd_class(hwnd: int) -> str:
    if not IS_WIN or not hwnd:
        return ''
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value.strip()


def _get_hwnd_process_name(hwnd: int) -> tuple[int, str]:
    if not IS_WIN or not hwnd:
        return (0, '')
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc_id = int(pid.value)
    if proc_id <= 0:
        return (0, '')
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, proc_id)
    if not hproc:
        return (proc_id, '')
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            return (proc_id, Path(buf.value).name)
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(hproc)
    return (proc_id, '')


def list_windows(filter_query: str = '') -> list[dict[str, Any]]:
    if not IS_WIN:
        return []
    fg_hwnd = int(user32.GetForegroundWindow() or 0)
    results: list[dict[str, Any]] = []
    q = (filter_query or '').strip().lower()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _get_hwnd_text(hwnd)
            if not title or title in ('Program Manager', 'Windows Input Experience', 'MSCTFIME UI', 'Default IME'):
                return True
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 24 or h < 24:
                return True
            pid, proc_name = _get_hwnd_process_name(hwnd)
            cls_name = _get_hwnd_class(hwnd)
            if q and q not in title.lower() and q not in proc_name.lower() and q not in cls_name.lower():
                return True
            results.append({
                'hwnd': int(hwnd),
                'title': title,
                'process': proc_name,
                'pid': pid,
                'class': cls_name,
                'bounds': [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                'width': int(w),
                'height': int(h),
                'minimized': bool(user32.IsIconic(hwnd)),
                'active': int(hwnd) == fg_hwnd,
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    results.sort(key=lambda x: (not x['active'], x['minimized'], x['title'].lower()))
    return results


def win32_clipboard_get_checked() -> tuple[bool, str, str]:
    if not IS_WIN:
        return (False, '', '仅支持 Windows 剪贴板')
    if not user32.OpenClipboard(0):
        return (False, '', f'OpenClipboard 失败 (err={win32_last_error()})')
    try:
        hmem = user32.GetClipboardData(CF_UNICODETEXT)
        if not hmem:
            return (True, '', '')
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            return (False, '', f'GlobalLock 失败 (err={win32_last_error()})')
        try:
            return (True, ctypes.wstring_at(ptr), '')
        finally:
            kernel32.GlobalUnlock(hmem)
    finally:
        user32.CloseClipboard()


def win32_clipboard_get() -> str:
    ok, txt, _ = win32_clipboard_get_checked()
    return txt if ok else ''


def win32_clipboard_set_checked(text: str) -> tuple[bool, str]:
    if not IS_WIN:
        return (False, '仅支持 Windows 剪贴板')
    encoded = (text or '').encode('utf-16-le') + b'\x00\x00'
    hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not hmem:
        return (False, f'GlobalAlloc 失败 (err={win32_last_error()})')
    ptr = kernel32.GlobalLock(hmem)
    if not ptr:
        kernel32.GlobalFree(hmem)
        return (False, f'GlobalLock 失败 (err={win32_last_error()})')
    try:
        ctypes.memmove(ptr, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(hmem)

    if not user32.OpenClipboard(0):
        kernel32.GlobalFree(hmem)
        return (False, f'OpenClipboard 失败 (err={win32_last_error()})')
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(hmem)
            return (False, f'EmptyClipboard 失败 (err={win32_last_error()})')
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return (False, f'SetClipboardData 失败 (err={win32_last_error()})')
        return (True, '')
    finally:
        user32.CloseClipboard()


def win32_clipboard_set(text: str) -> bool:
    ok, _ = win32_clipboard_set_checked(text)
    return ok


def capture_window_jpeg(hwnd: int = 0, full_screen: bool = False) -> tuple[str, int, int]:
    """Capture a background window via Win32 PrintWindow(PW_RENDERFULLCONTENT) or full screen."""
    if not IS_WIN:
        return ('', 0, 0)
    from PIL import Image

    if hwnd and not full_screen and user32.IsWindow(hwnd):
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(1, rect.right - rect.left)
        h = max(1, rect.bottom - rect.top)
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return ('', 0, 0)
        mfc_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        if not mfc_dc:
            user32.ReleaseDC(hwnd, hwnd_dc)
            return ('', 0, 0)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        if not bmp:
            gdi32.DeleteDC(mfc_dc)
            user32.ReleaseDC(hwnd, hwnd_dc)
            return ('', 0, 0)
        old_obj = gdi32.SelectObject(mfc_dc, bmp)
        try:
            ok = bool(user32.PrintWindow(hwnd, mfc_dc, PW_RENDERFULLCONTENT))
            if not ok:
                ok = bool(user32.PrintWindow(hwnd, mfc_dc, 0))
            if not ok:
                return ('', 0, 0)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            scanlines = gdi32.GetDIBits(mfc_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
            if not scanlines:
                return ('', 0, 0)
            img = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB')
        finally:
            if old_obj:
                gdi32.SelectObject(mfc_dc, old_obj)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mfc_dc)
            user32.ReleaseDC(hwnd, hwnd_dc)
    else:
        from PIL import ImageGrab
        try:
            img = ImageGrab.grab(all_screens=True).convert('RGB')
        except TypeError:
            img = ImageGrab.grab().convert('RGB')
        w, h = img.size

    if max(w, h) > 1280:
        scale = 1280.0 / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        w, h = img.size
    bio = io.BytesIO()
    img.save(bio, format='JPEG', quality=70, optimize=True)
    return (base64.b64encode(bio.getvalue()).decode('ascii'), w, h)
