"""Desktop terminal control for discovered sessions.

Discovery can see CLI sessions the user already has open in a terminal.
This module sends text to that existing terminal instead of spawning a
parallel Halo-owned agent process.
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

from halo.discovery import DiscoveredSession


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message: str


def send_to_session(session: DiscoveredSession, text: str) -> SendResult:
    text = (text or "").strip()
    if not text:
        return SendResult(False, "Nothing to send.")
    if sys.platform != "win32":
        return SendResult(False, "Desktop terminal control is only implemented on Windows.")

    hwnd = _find_window_for_session(session)
    if not hwnd:
        return SendResult(False, f"I couldn't find the terminal window for {session.label}.")

    try:
        _set_clipboard_text(text)
        _focus_window(hwnd)
        time.sleep(0.08)
        _send_ctrl_v()
        time.sleep(0.04)
        _send_enter()
    except Exception as exc:
        return SendResult(False, f"I couldn't type into {session.label}: {exc}")
    return SendResult(True, f"Sent to {session.label}.")


def foreground_pid() -> int | None:
    """PID that owns the current foreground window, Windows only."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    proc_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
    return int(proc_id.value or 0) or None


def foreground_window() -> int | None:
    """HWND of the current foreground window, Windows only."""
    if sys.platform != "win32":
        return None
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def type_into_focus(
    text: str,
    *,
    send_enter: bool = False,
    method: str = "unicode",
) -> SendResult:
    """Type `text` into whatever control currently has keyboard focus.

    Unlike send_to_session this does NOT resolve or focus a specific window —
    it injects into the live foreground focus, so the user can click into any
    text field (editor, browser, chat box, terminal) and have speech typed
    there. This is the primitive behind dictation mode.

    method="unicode" (default) emits one KEYEVENTF_UNICODE keystroke per
    UTF-16 code unit — layout-independent, no clipboard side effects, and it
    does NOT press Enter unless asked (Enter submits chat boxes / forms).
    method="paste" routes long text through the clipboard (Ctrl+V) and
    restores the user's prior clipboard afterwards.
    """
    text = text or ""
    if not text and not send_enter:
        return SendResult(False, "Nothing to type.")
    if sys.platform != "win32":
        return SendResult(False, "Dictation injection is only implemented on Windows.")
    try:
        if text:
            if method == "paste":
                _paste_text(text)
            else:
                _send_unicode(text)
        if send_enter:
            time.sleep(0.01)
            _send_enter()
    except Exception as exc:
        return SendResult(False, f"I couldn't type that: {exc}")
    return SendResult(True, "")


def press_enter() -> None:
    _send_enter()


def press_backspace() -> None:
    VK_BACK = 0x08
    try:
        _send_keys(_key(VK_BACK), _key(VK_BACK, True))
    except RuntimeError:
        _keybd_event(VK_BACK)
        _keybd_event(VK_BACK, up=True)


def _find_window_for_session(session: DiscoveredSession) -> int:
    # Prefer the parent shell/terminal window. The agent process itself is
    # often node.exe and may not own a visible top-level window.
    for pid in (session.parent_pid, session.pid):
        if not pid:
            continue
        hwnd = _find_window_for_pid(int(pid))
        if hwnd:
            return hwnd
    return 0


def _find_window_for_pid(pid: int) -> int:
    user32 = ctypes.windll.user32
    user32.EnumWindows.argtypes = (ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p), ctypes.c_void_p)
    user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    found = ctypes.c_void_p(0)

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        proc_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            found.value = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return int(found.value or 0)


def _focus_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def _set_clipboard_text(text: str) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = (text + "\0").encode("utf-16le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise RuntimeError("GlobalAlloc failed")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise RuntimeError("GlobalLock failed")
    ctypes.memmove(locked, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise RuntimeError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise RuntimeError("SetClipboardData failed")
        # Ownership transferred to the system on success.
        handle = 0
    finally:
        user32.CloseClipboard()


def _get_clipboard_text() -> str | None:
    """Read the current CF_UNICODETEXT clipboard contents, or None.

    Used to save/restore the user's clipboard around a paste so dictation
    doesn't permanently clobber whatever they had copied.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    user32.GetClipboardData.argtypes = (ctypes.c_uint,)
    user32.GetClipboardData.restype = ctypes.c_void_p
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)
        if not locked:
            return None
        try:
            return ctypes.c_wchar_p(locked).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _paste_text(text: str) -> None:
    """Set the clipboard, Ctrl+V, then restore the prior clipboard."""
    prior = _get_clipboard_text()
    _set_clipboard_text(text)
    _send_ctrl_v()
    time.sleep(0.05)
    if prior is not None:
        try:
            _set_clipboard_text(prior)
        except Exception:
            pass


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _MOUSEINPUT(ctypes.Structure):
    # The real Win32 INPUT union is sized by its LARGEST member, MOUSEINPUT
    # (32 bytes on x64), not KEYBDINPUT (24). Without this member ctypes
    # computes sizeof(INPUT)=32, so _send_keys passes cbSize=32 while the OS
    # expects 40 — SendInput then returns 0 (ERROR_INVALID_PARAMETER) and the
    # modern path silently never fires (it limps along on the keybd_event
    # fallback, which can't inject Unicode). Including MOUSEINPUT makes the
    # union 32 bytes so sizeof(_INPUT) is the correct 40 on x64.
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("union", _INPUT_UNION))


def _key(vk: int, up: bool = False) -> _INPUT:
    KEYEVENTF_KEYUP = 0x0002
    flags = KEYEVENTF_KEYUP if up else 0
    return _INPUT(1, _INPUT_UNION(_KEYBDINPUT(vk, 0, flags, 0, 0)))


def _send_keys(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (
        ctypes.c_uint,
        ctypes.POINTER(_INPUT),
        ctypes.c_int,
    )
    user32.SendInput.restype = ctypes.c_uint
    sent = user32.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))
    if sent != len(arr):
        err = ctypes.get_last_error()
        suffix = f" ({err})" if err else ""
        raise RuntimeError(f"SendInput failed{suffix}")


def _send_ctrl_v() -> None:
    VK_CONTROL = 0x11
    VK_V = 0x56
    try:
        _send_keys(_key(VK_CONTROL), _key(VK_V), _key(VK_V, True), _key(VK_CONTROL, True))
    except RuntimeError:
        _keybd_event(VK_CONTROL)
        _keybd_event(VK_V)
        _keybd_event(VK_V, up=True)
        _keybd_event(VK_CONTROL, up=True)


def _send_enter() -> None:
    VK_RETURN = 0x0D
    try:
        _send_keys(_key(VK_RETURN), _key(VK_RETURN, True))
    except RuntimeError:
        _keybd_event(VK_RETURN)
        _keybd_event(VK_RETURN, up=True)


def _keybd_event(vk: int, *, up: bool = False) -> None:
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _unicode_input(code_unit: int, *, up: bool) -> _INPUT:
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    # wVk=0, wScan=<UTF-16 code unit>, KEYEVENTF_UNICODE -> the OS delivers a
    # literal character to the focused control regardless of keyboard layout.
    return _INPUT(1, _INPUT_UNION(ki=_KEYBDINPUT(0, code_unit, flags, 0, 0)))


def _send_unicode(text: str) -> None:
    """Inject `text` as literal Unicode characters into the focused control.

    Iterates UTF-16 code units (so astral chars / emoji are sent as their
    surrogate pair, which is exactly what Windows expects). Batched into
    chunks so a long transcript is still a few SendInput calls, not one per
    character.
    """
    units = text.encode("utf-16-le")
    inputs: list[_INPUT] = []
    for i in range(0, len(units), 2):
        cu = units[i] | (units[i + 1] << 8)
        inputs.append(_unicode_input(cu, up=False))
        inputs.append(_unicode_input(cu, up=True))
    if not inputs:
        return
    CHUNK = 512  # SendInput entries per call
    for start in range(0, len(inputs), CHUNK):
        _send_keys(*inputs[start : start + CHUNK])
