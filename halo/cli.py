"""Halo CLI entry point.

After `pip install halo-voice`, the `halo` command is available with these
subcommands:

  halo                  Start the voice loop (same as `halo run`).
  halo run              Start the voice loop.
  halo download-models  Fetch Kokoro TTS model files into the models dir.
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
    sub.add_parser("version", help="Print the installed version and exit.")
    return parser


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

    if args.command == "version":
        print(f"halo-voice {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
