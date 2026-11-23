"""把文本放进系统剪贴板（零依赖，失败只会返回 False）。"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _windows(text: str) -> bool:
    import ctypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def _posix(text: str) -> bool:
    for command in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]):
        if not shutil.which(command[0]):
            continue
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=3)
            return True
        except Exception:
            continue
    return False


def copy_to_clipboard(text: str) -> bool:
    """复制文本到剪贴板。任何平台失败都不抛异常，只返回 False。"""
    if not text:
        return False
    try:
        return _windows(text) if sys.platform == "win32" else _posix(text)
    except Exception:
        return False
