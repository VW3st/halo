"""Tests for halo/discovery.py — cmdline classification + a live smoke
test against the real process table.

Run: python scripts/test_discovery.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halo.discovery import (
    _classify_cmdline,
    _label_for,
    is_available,
    scan_once,
)


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}")


# ---------------------------------------------------------------------------
# cmdline classification

def test_classify_claude_npm_shim():
    """Windows npm-installed claude is `node.exe ...\\@anthropic-ai\\claude-code\\cli.js`."""
    cmdline = [
        "C:\\Program Files\\nodejs\\node.exe",
        "C:\\Users\\agenc\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js",
    ]
    assert_eq(_classify_cmdline(cmdline), "claude_code")


def test_classify_codex_npm_shim():
    cmdline = [
        "/usr/bin/node",
        "/usr/lib/node_modules/@openai/codex/bin/codex.js",
    ]
    assert_eq(_classify_cmdline(cmdline), "codex_cli")


def test_classify_excludes_halo_persistent():
    """Halo's own persistent Claude has --input-format stream-json and
    must NOT be reported as a discovered session."""
    cmdline = [
        "C:\\Program Files\\nodejs\\node.exe",
        "C:\\path\\@anthropic-ai\\claude-code\\cli.js",
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
    ]
    assert_eq(_classify_cmdline(cmdline), None, "Halo-spawned should filter out")


def test_classify_excludes_halo_codex_oneshot():
    cmdline = ["codex", "exec", "-c", "approval_policy=never", "build something"]
    assert _classify_cmdline(cmdline) is None, "codex exec is Halo-spawned"


def test_classify_winget_claude_continue():
    """WinGet-installed Claude is a standalone .exe binary, no node shim."""
    cmdline = [
        "C:\\Users\\agenc\\AppData\\Local\\Microsoft\\WinGet\\Links\\claude.exe",
        "--continue",
    ]
    assert_eq(_classify_cmdline(cmdline), "claude_code")


def test_classify_winget_claude_bare():
    cmdline = ["C:\\Users\\agenc\\AppData\\Local\\Microsoft\\WinGet\\Links\\claude.exe"]
    assert_eq(_classify_cmdline(cmdline), "claude_code")


def test_classify_excludes_claude_desktop_app():
    """Claude Desktop is an Electron app at WindowsApps/Claude_*/app/Claude.exe.
    Must NOT be mis-detected as the Claude Code CLI."""
    cmdline = [
        "C:\\Program Files\\WindowsApps\\Claude_1.8555.2.0_x64__pzs8sxrjxfjjc\\app\\Claude.exe",
        "--type=renderer",
        "--user-data-dir=C:\\Users\\agenc\\AppData\\Roaming\\Claude",
    ]
    assert_eq(_classify_cmdline(cmdline), None, "Electron renderer must be excluded")


def test_classify_excludes_claude_desktop_top_level():
    """The top-level desktop binary (no --type= arg) — still matches the
    WindowsApps/Claude_ exclusion because the path is the giveaway."""
    cmdline = [
        "C:\\Program Files\\WindowsApps\\Claude_1.8555.2.0_x64__pzs8sxrjxfjjc\\app\\Claude.exe",
    ]
    assert_eq(_classify_cmdline(cmdline), None)


def test_classify_excludes_claude_desktop_gpu_process():
    cmdline = [
        "C:\\Program Files\\WindowsApps\\Claude_1.8555.2.0_x64__pzs8sxrjxfjjc\\app\\Claude.exe",
        "--type=gpu-process",
    ]
    assert_eq(_classify_cmdline(cmdline), None)


def test_classify_unrelated_node():
    cmdline = ["node", "server.js"]
    assert_eq(_classify_cmdline(cmdline), None)


def test_classify_unrelated_python():
    cmdline = ["python", "-m", "halo"]
    assert_eq(_classify_cmdline(cmdline), None)


def test_classify_unrelated_node_mcp_server():
    """MCP server processes (playwright-mcp, context7-mcp, etc.) are
    node processes living deep in npm-cache paths — must NOT be picked
    up just because the path contains 'claude' somewhere."""
    cmdline = [
        "node",
        "C:\\Users\\agenc\\AppData\\Local\\npm-cache\\_npx\\xyz\\node_modules\\@playwright\\mcp\\cli.js",
    ]
    assert_eq(_classify_cmdline(cmdline), None)


# ---------------------------------------------------------------------------
# Label derivation

def test_label_basename():
    assert_eq(_label_for("D:\\Halo"), "Halo")
    assert_eq(_label_for("/home/v/projects/website"), "website")


def test_label_drive_root():
    label = _label_for("D:\\")
    # Either "D:" or "D" — both are acceptable; we just want non-empty.
    assert label and ":" in label or label, f"drive root label too empty: {label!r}"


# ---------------------------------------------------------------------------
# Live smoke

def test_live_scan_does_not_crash():
    """scan_once() must complete without throwing even if psutil refuses
    permission for some processes. Doesn't assert specific finds — that's
    environment-dependent."""
    if not is_available():
        print("    (skipped — psutil not installed)")
        return
    sessions = scan_once()
    # Sanity check shape of each row.
    for s in sessions:
        assert s.pid > 0
        assert s.cwd, f"empty cwd in session {s}"
        assert s.label, f"empty label in session {s}"
        assert s.agent_key in ("claude_code", "codex_cli"), s.agent_key
    print(f"    (found {len(sessions)} live session(s))")
    for s in sessions:
        print(f"      {s}")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_classify_claude_npm_shim,
        test_classify_codex_npm_shim,
        test_classify_excludes_halo_persistent,
        test_classify_excludes_halo_codex_oneshot,
        test_classify_winget_claude_continue,
        test_classify_winget_claude_bare,
        test_classify_excludes_claude_desktop_app,
        test_classify_excludes_claude_desktop_top_level,
        test_classify_excludes_claude_desktop_gpu_process,
        test_classify_unrelated_node,
        test_classify_unrelated_python,
        test_classify_unrelated_node_mcp_server,
        test_label_basename,
        test_label_drive_root,
        test_live_scan_does_not_crash,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            print(f"  FAIL  {t.__name__}\n    {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERR   {t.__name__}\n    {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
