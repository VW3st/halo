"""Local web dashboard for Halo.

Serves a single-page HTML view at http://127.0.0.1:7070 by default.
The page polls /api/events every 250 ms and renders the pipeline,
transcripts, jobs, and log live. No build step, no frameworks — one
HTML file in halo/web_static/.

The HTTP server runs on a daemon thread so it dies with the process
and never blocks the main voice loop.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from halo.bus import current_seq, events_since

# Captured at module import so /api/recent-files only surfaces things
# modified during THIS Halo session — useful for "show me what the
# agent just built". Walks CWD (the project the user launched Halo in).
_SESSION_START_TS = time.time()
_SESSION_START_CWD = Path.cwd()

# Dirs to skip when walking for recent files. Keep tight — these are
# the high-noise dirs you never want to surface as "recent work".
_RECENT_FILES_SKIP = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "models", "recordings", "dist", "build", "target", ".next",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "halo.egg-info", "halo_voice.egg-info",
}

_STATIC_DIR = Path(__file__).parent / "web_static"

app = Flask(__name__, static_folder=None)

# Quiet Flask's per-request log spam — we'd see one line every poll otherwise.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.get("/")
def index() -> Any:
    return send_from_directory(_STATIC_DIR, "index.html")


@app.get("/assets/<path:filename>")
def assets(filename: str) -> Any:
    """Serve the Vite-built JS/CSS bundles (dashboard/ -> web_static/assets)."""
    return send_from_directory(_STATIC_DIR / "assets", filename)


@app.get("/api/events")
def api_events() -> Any:
    since = int(request.args.get("since", 0) or 0)
    evts = events_since(since)
    return jsonify({"seq": current_seq(), "events": evts})


@app.get("/api/recent-files")
def api_recent_files() -> Any:
    """Files in the launch-cwd whose mtime is after the session started.

    Useful for the dashboard's "recent files" panel — surfaces whatever
    agents (or the user) created/modified this session, so the user can
    click one to open it in the OS default app. Capped to 25 to keep
    payloads small.
    """
    out: list[dict] = []
    cutoff = _SESSION_START_TS
    for root, dirs, files in os.walk(_SESSION_START_CWD):
        # Prune skip dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d not in _RECENT_FILES_SKIP and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            try:
                rel = p.relative_to(_SESSION_START_CWD)
            except ValueError:
                rel = p
            out.append({
                "path": str(rel).replace("\\", "/"),
                "name": p.name,
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return jsonify({"files": out[:25], "cwd": str(_SESSION_START_CWD)})


@app.post("/api/open-file")
def api_open_file() -> Any:
    """Open a file from the recent-files list in the OS default app.

    Path must resolve inside the launch-cwd — refuses anything that
    escapes it, so a malicious browser tab can't trick the local
    dashboard into opening arbitrary system files.
    """
    data = request.get_json(silent=True) or {}
    rel = data.get("path", "")
    if not rel:
        return jsonify({"ok": False, "error": "missing path"}), 400
    target = (_SESSION_START_CWD / rel).resolve()
    try:
        target.relative_to(_SESSION_START_CWD.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "path escapes cwd"}), 400
    if not target.exists():
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        if sys.platform == "win32":
            os.startfile(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "opened": target.name})


@app.get("/api/system-metrics")
def api_system_metrics() -> Any:
    """Live machine metrics — CPU/RAM, GPU if available, conversation
    timing. Polled by the bottom-left SYSTEM METRICS panel every ~1 s.

    All optional sources degrade gracefully — psutil missing returns
    zeros for CPU/RAM, NVML missing skips GPU. Never raises."""
    out: dict = {
        "cpu_pct": 0.0,
        "mem_used_gb": 0.0,
        "mem_total_gb": 0.0,
        "mem_pct": 0.0,
        "gpu_pct": 0.0,
        "gpu_mem_pct": 0.0,
        "gpu_temp_c": 0.0,
        "gpu_name": "",
        "tokens_per_sec": 0.0,
        "context_tokens": 0,
        "uptime_sec": time.time() - _SESSION_START_TS,
        "session_count": 0,
        "active_jobs": 0,
    }
    try:
        import psutil  # type: ignore
        # non-blocking CPU sample — psutil's first call returns 0.0, so the
        # frontend sees real numbers from the second poll onwards.
        out["cpu_pct"] = float(psutil.cpu_percent(interval=None))
        vm = psutil.virtual_memory()
        out["mem_used_gb"] = round((vm.total - vm.available) / 1024**3, 2)
        out["mem_total_gb"] = round(vm.total / 1024**3, 2)
        out["mem_pct"] = float(vm.percent)
    except Exception:
        pass

    # GPU stats via NVML if available. pynvml ships with torch/cuda
    # installs and is the only Python binding that's both stdlib-light
    # AND tells us temp/util in one call.
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out["gpu_pct"] = float(util.gpu)
        out["gpu_mem_pct"] = round(mem.used / mem.total * 100, 1)
        out["gpu_temp_c"] = float(
            pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        )
        out["gpu_name"] = pynvml.nvmlDeviceGetName(h).decode() if isinstance(
            pynvml.nvmlDeviceGetName(h), bytes
        ) else str(pynvml.nvmlDeviceGetName(h))
        pynvml.nvmlShutdown()
    except Exception:
        pass

    # Conversation-side metrics.
    try:
        from halo.__main__ import REGISTRY
        out["session_count"] = len(REGISTRY.list())
    except Exception:
        pass
    try:
        from halo.agents import active_jobs
        jobs = active_jobs()
        out["active_jobs"] = len(jobs)
        # tokens/sec rough estimate from any running job's elapsed +
        # streaming chars (Claude streams sentences via agent.streaming
        # events; we use the recent event-bus rate as a proxy).
        from halo.bus import events_since, current_seq
        recent = events_since(max(0, current_seq() - 200))
        stream_evts = [
            e for e in recent
            if e["kind"] == "agent.streaming"
            and e["ts"] > time.time() - 5.0
        ]
        if stream_evts:
            chars = sum(len(e.get("sentence", "")) for e in stream_evts)
            # Rough char->token ratio (~4 chars/token for English code).
            out["tokens_per_sec"] = round(chars / 4.0 / 5.0, 1)
    except Exception:
        pass
    return jsonify(out)


@app.post("/api/control/reset-sessions")
def api_reset_sessions() -> Any:
    """Drop agent continuation pointers — next dispatch starts fresh
    Claude/Codex threads with new spoken names. Same as voice "new task"
    but driven from the dashboard."""
    from halo.agents import reset_session

    reset_session()
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state() -> Any:
    # Imported lazily so importing halo.web doesn't drag in audio stuff.
    from halo.agents import (
        AGENTS,
        active_jobs,
        check_availability,
        session_name,
        session_status,
    )

    sstatus = session_status()
    avail = check_availability()  # cached after first call
    actives_by_key = {j.agent_key: j for j in active_jobs()}
    agents = []
    for key, cfg in AGENTS.items():
        j = actives_by_key.get(key)
        a = avail.get(key, {})
        agents.append({
            "key": key,
            "spoken_name": cfg.spoken_name,
            "session_name": session_name(key),
            "session_active": sstatus.get(key, False),
            "job_running": j is not None,
            "job_elapsed_sec": j.elapsed_sec if j else None,
            "job_prompt": j.prompt if j else None,
            "installed": bool(a.get("installed")),
            "responsive": bool(a.get("responsive")),
            "install_hint": a.get("install_hint", ""),
        })
    return jsonify({"agents": agents, "seq": current_seq()})


def start_server(
    host: str = "127.0.0.1",
    port: int = 7070,
    open_browser: bool = True,
) -> str:
    """Start the dashboard on a daemon thread. Returns the URL printed
    at Halo startup so the user knows where to click.

    Auto-opens the URL in the default browser unless `open_browser=False`
    or the `HALO_NO_BROWSER` env var is set (handy for headless runs).
    """
    def _run() -> None:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True, name="halo-web")
    thread.start()

    url = f"http://{host}:{port}"

    if open_browser and not os.environ.get("HALO_NO_BROWSER"):
        # Don't spawn a NEW tab on every restart — the dashboard's polling UI
        # auto-reconnects across server restarts, so an already-open tab just
        # resumes. We open at most once per rolling 6h window (tracked by a
        # marker file), so rapid start/stop cycles don't pile up tabs.
        from pathlib import Path
        import time as _t

        marker = Path.home() / ".halo" / ".dashboard-opened"
        fresh = False
        try:
            fresh = marker.is_file() and (_t.time() - marker.stat().st_mtime) < 6 * 3600
        except Exception:
            fresh = False
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(int(_t.time())))  # roll the window forward
        except Exception:
            pass

        if fresh:
            print("  dashboard: reusing the already-open tab (it auto-reconnects)")
        else:
            # Brief delay so Flask's bind completes before the browser hits the
            # URL — avoids a "can't reach the site" flash on slow boxes.
            def _open_when_ready() -> None:
                import time
                time.sleep(0.4)
                try:
                    webbrowser.open(url, new=2)
                except Exception:
                    pass

            threading.Thread(target=_open_when_ready, daemon=True,
                             name="halo-web-open").start()

    return url
