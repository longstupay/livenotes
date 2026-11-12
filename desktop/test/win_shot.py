"""窗口截图：Win32 GDI + 手写 PNG，不依赖 PowerShell / PIL。

给自动化验收用：按 PID 和标题找到窗口，置前，抓窗口矩形。
"""

from __future__ import annotations

import ctypes
import struct
import time
import zlib
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.SetProcessDPIAware()
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _windows_for_pid(pid: int, title_like: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []

    def cb(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        title = buf.value
        if title_like and title_like.lower() not in title.lower():
            return True
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        found.append((area, int(hwnd), title))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def find_window(pid: int, title_like: str = "LiveNotes /") -> int:
    found = _windows_for_pid(pid, title_like)
    if not found and title_like:
        found = _windows_for_pid(pid, "")
    if not found:
        raise RuntimeError(f"process {pid}: no visible window like {title_like!r}")
    found.sort(reverse=True)
    return found[0][1]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data +
            struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _write_png(path: Path, w: int, h: int, bgra: bytes) -> None:
    stride = w * 4
    raw = bytearray()
    for y in range(h - 1, -1, -1):          # DIB 是自下而上
        row = bgra[y * stride:(y + 1) * stride]
        raw.append(0)                        # filter: none
        rgb = bytearray(w * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        raw.extend(rgb)
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + _png_chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def capture(pid: int, out: Path, title_like: str = "LiveNotes /") -> tuple[int, int, int, int]:
    hwnd = find_window(pid, title_like)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    time.sleep(0.35)

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"bad window rect {w}x{h}")

    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not gdi32.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, SRCCOPY):
            raise ctypes.WinError(ctypes.get_last_error())
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = h
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)
        if got != h:
            raise RuntimeError(f"GetDIBits returned {got}/{h}")
        _write_png(out, w, h, buf.raw)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
    return w, h, rect.left, rect.top


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pid", type=int)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", default="LiveNotes /")
    a = ap.parse_args()
    print(capture(a.pid, a.out, a.title))
