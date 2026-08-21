"""Local Flask web UI for goodreads-tools.

Two buttons:
  - Sync to StoryGraph: spawns book_sync.py as a subprocess, streams the log.
  - Generate Year in Books: runs the goodreads_stats pipeline and exposes the
    three generated files (PDF, web PNG, social card PNG) as downloads.

Binds to 127.0.0.1 only. No auth.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

import goodreads_stats
from config_loader import load_config


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OUTPUT_DIR = ROOT / "output"
SYNC_SCRIPT = ROOT / "book_sync.py"

app = Flask(__name__)

_runs: dict = {}
_runs_lock = threading.Lock()


# -------- helpers --------

def _read_config() -> dict:
    try:
        return load_config(CONFIG_PATH)
    except Exception:
        return {}


def _active_run() -> dict | None:
    for run in _runs.values():
        if run["status"] == "running":
            return run
    return None


def _watch_sync(run_id: str) -> None:
    run = _runs[run_id]
    code = run["process"].wait()
    try:
        run["log_file"].close()
    except Exception:
        pass
    with _runs_lock:
        run["status"] = "done" if code == 0 else "failed"
        run["ended"] = datetime.now().isoformat()
        run["exit_code"] = code


def _serializable(run: dict) -> dict:
    return {
        "id": run["id"],
        "status": run["status"],
        "started": run["started"],
        "ended": run["ended"],
        "exit_code": run["exit_code"],
    }


# -------- routes --------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sync", methods=["POST"])
def start_sync():
    with _runs_lock:
        active = _active_run()
        if active:
            return jsonify({
                "error": "Sync already running",
                "active_run_id": active["id"],
            }), 409

        if not SYNC_SCRIPT.exists():
            return jsonify({"error": f"sync script missing: {SYNC_SCRIPT}"}), 500

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:8]
        log_path = OUTPUT_DIR / f"sync_{run_id}.log"
        log_file = open(log_path, "w", buffering=1, encoding="utf-8", errors="replace")

        process = subprocess.Popen(
            [sys.executable, str(SYNC_SCRIPT)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
        )

        _runs[run_id] = {
            "id": run_id,
            "process": process,
            "log_file": log_file,
            "log_path": log_path,
            "status": "running",
            "started": datetime.now().isoformat(),
            "ended": None,
            "exit_code": None,
        }

    threading.Thread(target=_watch_sync, args=(run_id,), daemon=True).start()
    return jsonify({"run_id": run_id, "log_url": f"/sync-log/{run_id}"})


@app.route("/sync-log/<run_id>")
def sync_log(run_id: str):
    run = _runs.get(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    log_path = run["log_path"]
    text = ""
    next_offset = offset
    if log_path.exists():
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        next_offset = offset + len(chunk)

    return jsonify({
        "text": text,
        "next_offset": next_offset,
        "status": run["status"],
        "exit_code": run["exit_code"],
        "log_path": str(log_path.relative_to(ROOT)).replace("\\", "/"),
    })


@app.route("/generate-stats", methods=["POST"])
def generate_stats():
    config = _read_config()
    user_id = config.get("goodreads_user_id")
    if not user_id or user_id == "YOUR_GOODREADS_USER_ID":
        return jsonify({
            "error": "goodreads_user_id missing or unset in config.json",
        }), 400

    body = request.get_json(silent=True) or {}
    start_date_str = (body.get("start_date") or "").strip()
    end_date_str = (body.get("end_date") or "").strip()

    if bool(start_date_str) != bool(end_date_str):
        return jsonify({"error": "start_date and end_date must be given together"}), 400

    window_start = window_end = None
    if start_date_str and end_date_str:
        try:
            window_start = datetime.strptime(start_date_str, "%Y-%m-%d").astimezone()
            window_end = datetime.strptime(end_date_str, "%Y-%m-%d").astimezone().replace(
                hour=23, minute=59, second=59
            )
        except ValueError:
            return jsonify({"error": "dates must be in YYYY-MM-DD format"}), 400
        if window_start > window_end:
            return jsonify({"error": "start_date must not be after end_date"}), 400

    try:
        result = goodreads_stats.generate(
            user_id, OUTPUT_DIR, window_start=window_start, window_end=window_end
        )
    except Exception as e:
        logging.exception("Stats generation failed")
        return jsonify({"error": f"stats generation failed: {e}"}), 502

    outputs = result.get("outputs", {})
    download_urls = {
        name: f"/output/{Path(p).name}"
        for name, p in outputs.items()
    }
    return jsonify({
        "total_books": result["total_books"],
        "total_pages": result["total_pages"],
        "books_missing_pages": result["books_missing_pages"],
        "window_start": result["window_start"],
        "window_end": result["window_end"],
        "downloads": download_urls,
    })


@app.route("/output/<path:filename>")
def output_file(filename: str):
    return send_from_directory(OUTPUT_DIR, filename)


# -------- main --------

def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    host, port = "127.0.0.1", 5000
    url = f"http://{host}:{port}"
    print(f"Goodreads Tools running at {url}")
    print("Press Ctrl+C to stop.")

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
