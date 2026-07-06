"""Halo CLI entry point.

After `pip install halo-voice`, the `halo` command is available with these
subcommands:

  halo                  Start the voice loop (same as `halo run`).
  halo run              Start the voice loop.
  halo download-models  Fetch Kokoro TTS model files into the models dir.
  halo doctor           Diagnose dependency setup (Ollama, agents, models).
  halo config           Print effective config + sources (--init writes a template).
  halo prompts          List adjustable brain prompts (--init scaffolds editable files).
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
    cfg_parser = sub.add_parser(
        "config",
        help="Print the effective config and where it was loaded from.",
    )
    cfg_parser.add_argument(
        "--init",
        action="store_true",
        help="Write a commented starter config you can edit.",
    )
    cfg_parser.add_argument(
        "--path",
        help="Destination for --init (default: ~/.halo/config.toml).",
    )
    cfg_parser.add_argument(
        "--force",
        action="store_true",
        help="With --init, overwrite an existing file.",
    )
    pr_parser = sub.add_parser(
        "prompts",
        help="List the adjustable brain prompts and where they're loaded from.",
    )
    pr_parser.add_argument(
        "--init",
        action="store_true",
        help="Write the default prompts to editable files you can tweak.",
    )
    pr_parser.add_argument(
        "--force",
        action="store_true",
        help="With --init, overwrite existing prompt files.",
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


def _print_config(args) -> int:
    """`halo config` — show effective config + sources, or `--init` a template."""
    from halo.userconfig import (
        config_sources,
        effective_config_as_dict,
        write_template,
    )

    if getattr(args, "init", False):
        from pathlib import Path

        dest = Path(args.path) if getattr(args, "path", None) else None
        try:
            written = write_template(dest, overwrite=getattr(args, "force", False))
        except FileExistsError as exc:
            print(f"{exc}\nPass --force to overwrite.")
            return 1
        print(f"Wrote config template to {written}")
        print("Edit it, then run `halo config` to see the effective values.")
        return 0

    import json

    print("Config sources (later overrides earlier):")
    srcs = config_sources()
    if srcs:
        for p in srcs:
            print(f"  {p}")
    else:
        print("  (defaults only — no config files found)")
    print("\nEffective config:")
    print(json.dumps(effective_config_as_dict(), indent=2, default=str))
    return 0


def _print_prompts(args) -> int:
    """`halo prompts` — list adjustable prompts, or `--init` scaffold them."""
    from halo import prompts as _p

    if getattr(args, "init", False):
        from halo.router import PROMPT_DEFAULTS

        written = _p.write_defaults(
            PROMPT_DEFAULTS, overwrite=getattr(args, "force", False)
        )
        if written:
            print(f"Wrote {len(written)} prompt file(s) to {_p.prompts_dir()}:")
            for p in written:
                print(f"  {p.name}")
            print("Edit any of them; Halo loads your version on the next run.")
        else:
            print(
                f"All prompt files already exist in {_p.prompts_dir()}.\n"
                "Pass --force to overwrite with the current defaults."
            )
        return 0

    d = _p.prompts_dir()
    print(f"Prompt overrides dir: {d}\n")
    for name, desc in _p.KNOWN.items():
        custom = any((d / f"{name}{e}").is_file() for e in (".txt", ".md"))
        print(f"  [{'custom ' if custom else 'default'}] {name:16} {desc}")
    print("\nRun `halo prompts --init` to scaffold editable copies.")
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

    if args.command == "config":
        return _print_config(args)

    if args.command == "prompts":
        return _print_prompts(args)

    if args.command == "version":
        print(f"halo-voice {__version__}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
