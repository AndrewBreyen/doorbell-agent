#!/usr/bin/env python3
"""
server.py

Runs on the Mac mini. Exposes a small webhook that Home Assistant (on your VM)
calls when the doorbell rings. All the actual STT/LLM/TTS processing happens
here, locally on the Mac mini — HA's VM just fires the trigger.

Start with:
    python -m doorbell.server

Then point HA's rest_command at:
    http://<mac-mini-ip>:8787/trigger
"""

import logging
import threading

from flask import Flask, jsonify, send_from_directory
from waitress import serve

from .agent import AUDIO_DIR, HomeAssistant, handle_ring, load_config

app = Flask(__name__)
cfg = load_config()

AUDIO_DIR.mkdir(exist_ok=True)
visit_lock = threading.Lock()
logger = logging.getLogger(__name__)


def handle_visit():
    try:
        ha = HomeAssistant(cfg)
        if not handle_ring(cfg, ha):
            logger.info("Known face detected, skipping AI concierge.")
    except Exception:
        logger.exception("Doorbell visit failed")
    finally:
        visit_lock.release()


@app.route("/trigger", methods=["POST"])
def trigger():
    if not visit_lock.acquire(blocking=False):
        return jsonify({"status": "busy"}), 202
    try:
        threading.Thread(target=handle_visit, daemon=True).start()
    except Exception:
        visit_lock.release()
        raise
    return jsonify({"status": "started"}), 202


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    # Serves generated reply clips so Home Assistant (on a different machine)
    # can fetch them over HTTP instead of needing local filesystem access.
    return send_from_directory(AUDIO_DIR, filename)


def main():
    # Bind to 0.0.0.0 so the VM running HA can reach it over your LAN.
    logging.basicConfig(level=logging.INFO)
    serve(app, host="0.0.0.0", port=8787)


if __name__ == "__main__":
    main()
