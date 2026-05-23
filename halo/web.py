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
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from halo.bus import current_seq, events_since

_STATIC_DIR = Path(__file__).parent / "web_static"

app = Flask(__name__, static_folder=None)

# Quiet Flask's per-request log spam — we'd see one line every poll otherwise.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.get("/")
def index() -> Any:
    return send_from_directory(_STATIC_DIR, "index.html")


@app.get("/api/events")
def api_events() -> Any:
    since = int(request.args.get("since", 0) or 0)
    evts = events_since(since)
    return jsonify({"seq": current_seq(), "events": evts})


@app.get("/api/state")
def api_state() -> Any:
    # Imported lazily so importing halo.web doesn't drag in audio stuff.
    from halo.agents import AGENTS, active_jobs, session_name, session_status

    sstatus = session_status()
    actives_by_key = {j.agent_key: j for j in active_jobs()}
    agents = []
    for key, cfg in AGENTS.items():
        j = actives_by_key.get(key)
        agents.append({
            "key": key,
            "spoken_name": cfg.spoken_name,
            "session_name": session_name(key),
            "session_active": sstatus.get(key, False),
            "job_running": j is not None,
            "job_elapsed_sec": j.elapsed_sec if j else None,
            "job_prompt": j.prompt if j else None,
        })
    return jsonify({"agents": agents, "seq": current_seq()})


def start_server(host: str = "127.0.0.1", port: int = 7070) -> str:
    """Start the dashboard on a daemon thread. Returns the URL printed
    at Halo startup so the user knows where to click."""
    def _run() -> None:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, daemon=True, name="halo-web")
    thread.start()
    return f"http://{host}:{port}"
