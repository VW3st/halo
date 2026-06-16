"""Halo CLI entry point.

After `pip install halo-voice`, the `halo` command is available with these
subcommands:

  halo                  Start the voice loop (same as `halo run`).
  halo run              Start the voice loop.
  halo download-models  Fetch Kokoro TTS model files into the models dir.
  halo doctor           Diagnose dependency setup (Ollama, agents, models).
  halo version          Print the installed version.
  halo --help           Show this help.

Backwards-compat: `python -m halo` continues to work via halo/__main__.py.
"""

from __future__ import annotations

import argparse
import sys

from halo import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo",
        description="Voice frontend for agentic coding tools (Claude Code, Codex CLI).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"halo-voice {__version__}",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("run", help="Start the voice loop (default).")
    sub.add_parser(
        "download-models",
        help="Download Kokoro TTS model files (~200 MB) into the models dir.",
    )
    sub.add_parser(
        "doctor",
        help="Check that Ollama, agents (claude/codex), and Kokoro are wired up.",
    )
    sub.add_parser(
        "calibrate",
        help="Measure the wake word on this mic and save per-machine settings.",
    )
    sub.add_parser(
        "sessions",
        help="List running coding-agent sessions discovered on this machine.",
    )
    sub.add_parser("version", help="Print the installed version and exit.")
    return parser


def _print_sessions() -> int:
    """`halo sessions` — read-only one-shot discovery.

    Useful to verify multi-session discovery works on your machine
    before booting the full voice loop. Exit 0 if any sessions were
    found, 1 if none.
    """
    from halo.discovery import is_available, scan_once

    if not is_available():
        print(
            "psutil is not installed — discovery is disabled.\n"
            "Install with:  pip install psutil"
        )
        return 1

    sessions = scan_once()
    if not sessions:
        print(
            "No running coding-agent sessions detected.\n"
            "Open a terminal and start `claude` or `codex` somewhere, then re-run."
        )
        return 1

    print(f"Discovered {len(sessions)} session{'s' if len(sessions) != 1 else ''}:\n")
    # Aligned columns: label / agent / pid / cwd
    label_w = max((len(s.label) for s in sessions), default=10)
    agent_w = max((len(s.agent_key) for s in sessions), default=10)
    print(f"  {'LABEL':{label_w}}  {'AGENT':{agent_w}}  {'PID':>6}  CWD")
    print(f"  {'-' * label_w}  {'-' * agent_w}  {'-' * 6}  ---")
    for s in sessions:
        print(f"  {s.label:{label_w}}  {s.agent_key:{agent_w}}  {s.pid:>6}  {s.cwd}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "run"):
        from halo.__main__ import main as run_main
        run_main()
        return 0

    if args.command == "download-models":
        from halo.download_models import download_all
        return download_all()

    if args.command == "doctor":
        from halo.doctor import run as run_doctor
        return run_doctor()

    if args.command == "calibrate":
        from halo.calibrate import run as run_calibrate
        return run_calibrate()

    if args.command == "sessions":
        return _print_sessions()

    if args.command == "version":
        print(f"halo-voice {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
