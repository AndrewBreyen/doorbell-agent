#!/usr/bin/env python3
"""
server.py

Runs on the Mac mini. Exposes a small webhook that Home Assistant (on your VM)
calls when the doorbell rings. All the actual STT/LLM/TTS processing happens
here, locally on the Mac mini — HA's VM just fires the trigger.

Start with:
    python server.py

Then point HA's rest_command at:
    http://<mac-mini-ip>:8787/trigger
"""

import threading

from flask import Flask, jsonify

from doorbell_agent import HomeAssistant, is_known_face, load_config, run_visit

app = Flask(__name__)
cfg = load_config()


def handle_visit():
    ha = HomeAssistant(cfg)
    if is_known_face(ha, cfg):
        print("Known face detected, skipping AI concierge.")
        return
    run_visit(cfg, ha)


@app.route("/trigger", methods=["POST"])
def trigger():
    # Run in a background thread so HA's webhook call returns immediately
    # instead of waiting on the full multi-turn conversation.
    threading.Thread(target=handle_visit, daemon=True).start()
    return jsonify({"status": "started"}), 202


if __name__ == "__main__":
    # Bind to 0.0.0.0 so the VM running HA can reach it over your LAN.
    app.run(host="0.0.0.0", port=8787)
