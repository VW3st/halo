"""Global hotkey primitive (Windows) — "press -> signal".

Owns a single Win32 global hotkey registration and its message-pump thread. On
press it calls a caller-supplied callback (the orchestrator wires that to "start
dictation"). It knows nothing about dictation or the mic — a pure primitive, the
way floor.py is. No pip dependency: pure ctypes + RegisterHotKey/GetMessage.

`RegisterHotKey(hwnd=NULL, ...)` posts WM_HOTKEY to the CALLING thread's queue, so
`GetMessage` must run on the same thread that registered — hence the pump thread
does both. `parse_hotkey` is pure and unit-tested; importing this module is safe
on any OS (only `ctypes.windll` access is deferred into start()/the pump).
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

# Win32 fsModifiers (RegisterHotKey).
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # don't auto-repeat a held combo

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0xB001  # arbitrary app-unique id

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
    "alt": MOD_ALT, "opt": MOD_ALT, "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN, "meta": MOD_WIN,
}

# Named non-letter/digit keys -> virtual-key codes.
_VK_NAMES = {
    "space": 0x20, "spacebar": 0x20,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}


def _vk_for(token: str) -> int | None:
    """Virtual-key code for a single key token, or None if not a key."""
    if len(token) == 1:
        c = token.upper()
        if "A" <= c <= "Z" or "0" <= c <= "9":
            return ord(c)
        return None
    if token in _VK_NAMES:
        return _VK_NAMES[token]
    if token.startswith("f") and token[1:].isdigit():  # F1..F24
        n = int(token[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)
    return None


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """Parse "ctrl+alt+d" -> (fsModifiers | MOD_NOREPEAT, vk). Pure; no Win32.

    Requires at least ONE modifier and exactly one key. Accepts '+' or '-' as the
    separator, is case/whitespace-insensitive. Returns None for empty, modifier-
    only, no-modifier, two-key, or unknown-token specs."""
    if not spec:
        return None
    tokens = [t.strip().lower() for t in spec.replace("-", "+").split("+") if t.strip()]
    if not tokens:
        return None
    mods = 0
    key_vk: int | None = None
    for tok in tokens:
        if tok in _MODS:
            mods |= _MODS[tok]
            continue
        if key_vk is not None:
            return None  # more than one non-modifier key
        key_vk = _vk_for(tok)
        if key_vk is None:
            return None  # unknown token
    if key_vk is None or mods == 0:
        return None  # need a key AND at least one modifier
    return (mods | MOD_NOREPEAT, key_vk)


class HotkeyListener:
    """Registers one global hotkey and pumps its messages on a daemon thread.
    On press, calls `on_press()`. Windows-only; a no-op (start() -> False) else."""

    def __init__(self, spec: str, on_press: Callable[[], None]) -> None:
        self._spec = (spec or "").strip()
        self._parsed = parse_hotkey(self._spec)
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._bound: str | None = None

    @property
    def bound_spec(self) -> str | None:
        return self._bound

    def start(self) -> bool:
        """Register the hotkey + start the pump. True if bound, else False (logged)."""
        if sys.platform != "win32":
            print(f"  hotkey: {self._spec!r} not registered (not Windows)")
            return False
        if self._parsed is None:
            print(f"  hotkey: {self._spec!r} not registered (unparseable spec)")
            return False

        ready = threading.Event()
        ok = {"v": False}

        def _pump() -> None:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._tid = kernel32.GetCurrentThreadId()
            mods, vk = self._parsed
            if not user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk):
                err = ctypes.get_last_error()
                reason = "already in use" if err == 1409 else f"err {err}"
                print(f"  hotkey: {self._spec!r} not registered ({reason})")
                ready.set()
                return
            ok["v"] = True
            self._bound = self._spec
            ready.set()
            msg = wintypes.MSG()
            try:
                while True:
                    ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret in (0, -1):  # WM_QUIT or error
                        break
                    if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                        try:
                            self._on_press()
                        except Exception as exc:  # never let a callback kill the pump
                            print(f"  hotkey callback error: {exc}")
            finally:
                user32.UnregisterHotKey(None, _HOTKEY_ID)

        self._thread = threading.Thread(target=_pump, daemon=True, name="halo-hotkey")
        self._thread.start()
        ready.wait(timeout=2.0)
        return ok["v"]

    def stop(self) -> None:
        """Unregister + end the pump. Idempotent, best-effort, never raises."""
        if sys.platform != "win32" or self._tid is None:
            return
        try:
            import ctypes

            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                self._tid, _WM_QUIT, 0, 0
            )
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
