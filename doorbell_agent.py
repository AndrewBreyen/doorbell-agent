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
import re
import json
from pathlib import Path

import requests
import yaml
import numpy as np
from dotenv import load_dotenv
import os

CONFIG_PATH = Path(__file__).parent / "config.yaml"

from prompts import CLASSIFY_AND_RESPOND_PROMPT


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("Missing config.yaml — copy config.example.yaml and fill it in first.")
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    load_dotenv(Path(__file__).parent / ".env")
    token = os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("Missing HA_TOKEN — copy .env.example to .env and fill it in first.")
    cfg["home_assistant"]["token"] = token

    return cfg


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


def record_clip(cfg, out_path: Path) -> bool:
    """
    Streams audio from the doorbell and stops as soon as the visitor pauses,
    instead of always recording a fixed duration. Returns True if any speech-
    level audio was detected, False if the visitor never made noise.

    Tunable via config.yaml under behavior:
      max_clip_seconds       -- hard cap, always stops by this point
      silence_duration_seconds -- how long a pause must last to count as "done"
      silence_rms_threshold  -- volume level below which audio counts as silence
    """
    behavior = cfg["behavior"]
    max_seconds = behavior.get("max_clip_seconds", 8)
    silence_duration = behavior.get("silence_duration_seconds", 1.2)
    silence_threshold = behavior.get("silence_rms_threshold", 500)

    sample_rate = 16000
    chunk_seconds = 0.1
    chunk_bytes = int(sample_rate * 2 * chunk_seconds)  # 16-bit mono PCM

    stream_url = cfg["home_assistant"]["doorbell_stream_url"]
    cmd = ["ffmpeg", "-y"]
    if stream_url.startswith("rtsp://"):
        cmd += ["-rtsp_transport", "tcp"]
    if stream_url.startswith("rtsps://"):
        cmd += ["-tls_verify", "0"]
    cmd += [
        "-i", stream_url,
        "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "-",  # raw PCM to stdout instead of a fixed-length file
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    frames = bytearray()
    speech_detected = False
    silence_run = 0.0
    elapsed = 0.0

    try:
        while elapsed < max_seconds:
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                break  # stream ended
            frames.extend(chunk)
            elapsed += chunk_seconds

            samples = np.frombuffer(chunk, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0

            if rms >= silence_threshold:
                speech_detected = True
                silence_run = 0.0
            else:
                silence_run += chunk_seconds

            # Only cut the recording short once the visitor has actually
            # spoken and then paused -- don't stop just because they haven't
            # started talking yet.
            if speech_detected and silence_run >= silence_duration:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))

    return speech_detected



def transcribe(cfg, audio_path: Path) -> str:
    cmd = [
        cfg["models"]["whisper_binary"],
        "-m", cfg["models"]["whisper_model"],
        "-f", str(audio_path),
        "-nt",  # no timestamps
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"whisper-cli failed (exit {result.returncode}):\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout.strip()


def ollama_generate(cfg, prompt: str) -> str:
    r = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": cfg["models"]["ollama_model"],
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},  # lower = more literal, less likely to invent things
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["response"].strip()


def classify_and_respond(cfg, transcript: str, history: str) -> tuple[str, str]:
    """
    Single combined LLM call: classifies intent AND writes the reply in one
    round-trip instead of two, cutting a full model inference out of the
    response latency.
    """
    prompt = CLASSIFY_AND_RESPOND_PROMPT.format(
        household_name=cfg["behavior"]["household_name"],
        history=history or "(none yet)",
        transcript=transcript,
    )
    raw = ollama_generate(cfg, prompt)

    valid = {"DELIVERY", "VISITOR", "SOLICITOR", "UNCLEAR"}
    try:
        # Ollama sometimes wraps JSON in markdown fences despite instructions -- strip those.
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        intent = str(parsed.get("intent", "")).upper()
        reply = str(parsed.get("reply", "")).strip()
        if intent not in valid or not reply:
            raise ValueError("missing/invalid fields")
    except (json.JSONDecodeError, ValueError):
        # Fallback if the model didn't return clean JSON -- don't crash the visit over it.
        intent, reply = "UNCLEAR", "Hi, can I help you with something?"

    return intent, reply


PRONUNCIATION_OVERRIDES = {
    # Only affects what's sent to the TTS engine -- transcripts/notifications
    # keep the real spelling.
    "breyen": "Brian",
}


def apply_pronunciation_overrides(text: str) -> str:
    for spelling, phonetic in PRONUNCIATION_OVERRIDES.items():
        text = re.sub(rf"\b{spelling}\b", phonetic, text, flags=re.IGNORECASE)
    return text


def synthesize_speech(cfg, text: str, out_path: Path):
    speech_text = apply_pronunciation_overrides(text)
    cmd = [
        cfg["models"]["piper_binary"],
        "--model", cfg["models"]["piper_voice"],
        "--output_file", str(out_path),
    ]
    result = subprocess.run(cmd, input=speech_text, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"piper failed (exit {result.returncode}):\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)


def run_visit(cfg, ha: HomeAssistant):
    tmp_dir = Path("/tmp/doorbell_agent")
    tmp_dir.mkdir(exist_ok=True)

    history_lines = []
    final_intent = "UNCLEAR"

    for turn in range(cfg["behavior"]["max_exchanges"]):
        clip_path = tmp_dir / f"clip_{turn}.wav"
        reply_path = tmp_dir / f"reply_{turn}.wav"

        speech_detected = record_clip(cfg, clip_path)
        if not speech_detected:
            break  # visitor never made noise -- skip transcription entirely, save time
        transcript = transcribe(cfg, clip_path)

        if not transcript:
            break  # ffmpeg picked up noise but whisper couldn't make out words

        intent, reply_text = classify_and_respond(cfg, transcript, "\n".join(history_lines))
        final_intent = intent

        history_lines.append(f"Visitor: {transcript}")
        history_lines.append(f"Assistant: {reply_text}")

        synthesize_speech(cfg, reply_text, reply_path)
        agent_base_url = cfg["behavior"]["agent_base_url"].rstrip("/")
        ha.play_audio_on_doorbell(
            cfg["home_assistant"]["doorbell_talkback_entity"],
            f"{agent_base_url}/audio/{reply_path.name}",
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