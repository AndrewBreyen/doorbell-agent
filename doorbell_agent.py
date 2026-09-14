#!/usr/bin/env python3
"""
doorbell_agent.py

Local AI doorstep concierge for a UniFi Protect doorbell, run via Home Assistant.

Flow (triggered by an HA automation when the doorbell rings):
  1. Check if HA already flagged a *known/recognized face* -> if so, exit immediately.
  2. Otherwise: record a short audio clip -> transcribe (whisper.cpp)
  3. Classify visitor intent with a local LLM (Ollama)
  4. Generate a short spoken response matching that intent (firmer for solicitors)
  5. Synthesize speech (Piper) and play it back through the doorbell speaker via HA
  6. Repeat for a few turns, then send you a notification with a summary

Usage:
    python doorbell_agent.py

Requires config.yaml (copy from config.example.yaml) in the same directory.
"""

import subprocess
import sys
import time
import wave
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"

from prompts import CLASSIFY_PROMPT, RESPONSE_PROMPT


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("Missing config.yaml — copy config.example.yaml and fill it in first.")
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


class HomeAssistant:
    """Thin wrapper around the HA REST API."""

    def __init__(self, cfg):
        self.base = cfg["home_assistant"]["url"].rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {cfg['home_assistant']['token']}",
            "Content-Type": "application/json",
        }

    def get_state(self, entity_id):
        r = requests.get(f"{self.base}/api/states/{entity_id}", headers=self.headers, timeout=5)
        r.raise_for_status()
        return r.json()["state"]

    def call_service(self, domain, service, data):
        r = requests.post(
            f"{self.base}/api/services/{domain}/{service}",
            headers=self.headers,
            json=data,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def notify(self, service_name, message, title="Doorbell"):
        domain, service = service_name.split(".", 1)
        self.call_service(domain, service, {"title": title, "message": message})

    def play_audio_on_doorbell(self, entity_id, media_url):
        self.call_service(
            "media_player",
            "play_media",
            {
                "entity_id": entity_id,
                "media_content_id": media_url,
                "media_content_type": "music",
            },
        )


def is_known_face(ha, cfg):
    if not cfg["behavior"].get("known_face_check_enabled", True):
        # Toggle off for testing -- always treat visitors as unknown.
        return False

    sensor = cfg["home_assistant"].get("known_face_sensor")
    if not sensor:
        return False
    try:
        return ha.get_state(sensor) == "on"
    except requests.RequestException:
        # If HA/sensor is unreachable, fail open to "unknown" so the agent still runs
        return False


def record_clip(cfg, out_path: Path):
    """
    Records `clip_seconds` of audio from the doorbell RTSP stream using ffmpeg.
    Requires ffmpeg installed (brew install ffmpeg).
    """
    seconds = cfg["behavior"]["clip_seconds"]
    stream_url = cfg["home_assistant"]["doorbell_stream_url"]
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", stream_url,
        "-t", str(seconds),
        "-ac", "1", "-ar", "16000",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def transcribe(cfg, audio_path: Path) -> str:
    cmd = [
        cfg["models"]["whisper_binary"],
        "-m", cfg["models"]["whisper_model"],
        "-f", str(audio_path),
        "-nt",  # no timestamps
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def ollama_generate(cfg, prompt: str) -> str:
    r = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": cfg["models"]["ollama_model"], "prompt": prompt, "stream": False},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["response"].strip()


def classify_intent(cfg, transcript: str) -> str:
    prompt = CLASSIFY_PROMPT.format(transcript=transcript)
    label = ollama_generate(cfg, prompt).strip().upper()
    valid = {"DELIVERY", "VISITOR", "SOLICITOR", "UNCLEAR"}
    return label if label in valid else "UNCLEAR"


def generate_response(cfg, transcript: str, intent: str, history: str) -> str:
    prompt = RESPONSE_PROMPT.format(
        household_name=cfg["behavior"]["household_name"],
        intent=intent,
        history=history or "(none yet)",
        transcript=transcript,
    )
    return ollama_generate(cfg, prompt)


def synthesize_speech(cfg, text: str, out_path: Path):
    cmd = [
        cfg["models"]["piper_binary"],
        "--model", cfg["models"]["piper_voice"],
        "--output_file", str(out_path),
    ]
    subprocess.run(cmd, input=text, check=True, capture_output=True, text=True)


def run_visit(cfg, ha: HomeAssistant):
    tmp_dir = Path("/tmp/doorbell_agent")
    tmp_dir.mkdir(exist_ok=True)

    history_lines = []
    final_intent = "UNCLEAR"

    for turn in range(cfg["behavior"]["max_exchanges"]):
        clip_path = tmp_dir / f"clip_{turn}.wav"
        reply_path = tmp_dir / f"reply_{turn}.wav"

        record_clip(cfg, clip_path)
        transcript = transcribe(cfg, clip_path)

        if not transcript:
            break  # visitor said nothing / left

        intent = classify_intent(cfg, transcript)
        final_intent = intent
        reply_text = generate_response(cfg, transcript, intent, "\n".join(history_lines))

        history_lines.append(f"Visitor: {transcript}")
        history_lines.append(f"Assistant: {reply_text}")

        synthesize_speech(cfg, reply_text, reply_path)
        ha.play_audio_on_doorbell(
            cfg["home_assistant"]["doorbell_talkback_entity"],
            f"file://{reply_path}",
        )

        # Solicitors: say the line once and stop engaging further
        if intent == "SOLICITOR":
            break

        time.sleep(1)

    summary = "\n".join(history_lines) if history_lines else "(no speech detected)"
    ha.notify(
        cfg["behavior"]["notify_service"],
        message=f"Intent: {final_intent}\n\n{summary}",
        title="Doorbell — AI concierge",
    )


def main():
    cfg = load_config()
    ha = HomeAssistant(cfg)

    if is_known_face(ha, cfg):
        # Known face recognized by UniFi Protect -> skip the AI entirely.
        print("Known face detected, skipping AI concierge.")
        return

    run_visit(cfg, ha)


if __name__ == "__main__":
    main()
