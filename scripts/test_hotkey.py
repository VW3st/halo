"""Unit tests for halo.hotkey.parse_hotkey — the pure spec parser (no Win32)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    parse_hotkey,
)


def main() -> int:
    f = 0

    def check(label, got, want):
        nonlocal f
        ok = got == want
        f += 0 if ok else 1
        print(f"  [{'OK' if ok else 'FAIL'}] {label:34} -> {got} (want {want})")

    print("valid specs:")
    check("ctrl+alt+d", parse_hotkey("ctrl+alt+d"),
          (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x44))
    check("ctrl+shift+space", parse_hotkey("ctrl+shift+space"),
          (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, 0x20))
    check("whitespace/case ' Ctrl + Alt + D '", parse_hotkey(" Ctrl + Alt + D "),
          (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x44))
    check("dash separator ctrl-alt-f5", parse_hotkey("ctrl-alt-f5"),
          (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x74))
    check("win+d", parse_hotkey("win+d"), (MOD_WIN | MOD_NOREPEAT, 0x44))
    check("alt+1", parse_hotkey("alt+1"), (MOD_ALT | MOD_NOREPEAT, 0x31))
    check("ctrl+up arrow", parse_hotkey("ctrl+up"), (MOD_CONTROL | MOD_NOREPEAT, 0x26))

    print("\nNOREPEAT always set:")
    res = parse_hotkey("ctrl+alt+d")
    check("norepeat bit present", bool(res[0] & MOD_NOREPEAT), True)

    print("\nrejected specs -> None:")
    check("empty", parse_hotkey(""), None)
    check("no modifier 'd'", parse_hotkey("d"), None)
    check("modifier only 'ctrl+ctrl'", parse_hotkey("ctrl+ctrl"), None)
    check("modifier only 'ctrl+alt'", parse_hotkey("ctrl+alt"), None)
    check("two keys 'ctrl+a+b'", parse_hotkey("ctrl+a+b"), None)
    check("garbage token 'ctrl+zzz'", parse_hotkey("ctrl+zzz"), None)
    check("F25 out of range", parse_hotkey("ctrl+f25"), None)

    total = 17
    print(f"\n{total - f}/{total} passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
