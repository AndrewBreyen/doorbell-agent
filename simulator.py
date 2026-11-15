"""Local Flask UI for exercising the visit flow with scripted inputs."""

import logging
from tempfile import TemporaryDirectory
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from waitress import serve

from doorbell_agent import handle_ring

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

REPLIES = {
    "DELIVERY": "Thanks! Please leave the package by the door.",
    "VISITOR": "Thanks for stopping by. I'll let the household know.",
    "SOLICITOR": "We're not interested. Please leave the property.",
    "UNCLEAR": "Sorry, could you tell me what brings you here?",
}


class SimulatedDoorbell:
    def __init__(self, known_face, turns):
        self.known_face = known_face
        self.turns = iter(turns)
        self.events = []

    def log(self, state, detail):
        self.events.append({"state": state, "detail": detail})

    def get_state(self, entity_id):
        self.log("Recognition checked", "Known face" if self.known_face else "Unknown face")
        return "on" if self.known_face else "off"

    def record(self, cfg, path):
        self.turn = next(self.turns, {"transcript": "", "intent": "UNCLEAR"})
        self.log("Listening", "Simulated doorbell audio capture")
        if not self.turn["transcript"]:
            self.log("Silence detected", "No speech detected")
            return False
        return True

    def transcribe(self, cfg, path):
        transcript = self.turn["transcript"]
        self.log("Transcribed", transcript)
        return transcript

    def classify_and_respond(self, cfg, transcript, history):
        intent = self.turn["intent"]
        self.log("Intent classified", intent.title())
        reply = REPLIES[intent]
        self.log("Reply generated", reply)
        return intent, reply

    def synthesize(self, cfg, reply, path):
        self.log("Speech synthesized", "Simulated audio clip")

    def play_audio_on_doorbell(self, entity_id, media_url):
        self.log("Talkback played", "Simulated speaker playback")

    def pause(self, seconds):
        pass

    def notify(self, service_name, message, title):
        self.log("Notification sent", message)


@app.get("/")
def dashboard():
    return render_template("simulator.html")


@app.post("/api/simulate")
def simulate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("known_face")) is not bool:
        return jsonify(error="Choose whether the visitor is recognized."), 400

    turns = data.get("turns")
    if not isinstance(turns, list) or not 1 <= len(turns) <= 3:
        return jsonify(error="Provide one to three listening turns."), 400
    for turn in turns:
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("transcript"), str)
            or len(turn["transcript"]) > 500
            or not isinstance(turn.get("intent"), str)
            or turn["intent"] not in REPLIES
        ):
            return jsonify(error="Each turn needs a transcript of up to 500 characters and a valid intent."), 400
        turn["transcript"] = turn["transcript"].strip()

    device = SimulatedDoorbell(data["known_face"], turns)
    cfg = {
        "home_assistant": {"known_face_sensor": "simulated_face", "doorbell_talkback_entity": "simulated_speaker"},
        "behavior": {
            "max_exchanges": 3,
            "notify_service": "simulated_notification",
            "agent_base_url": "http://localhost:8788",
        },
    }
    device.log("Ring received", "Simulated front door button press")
    with TemporaryDirectory(prefix="doorbell-sim-") as temp:
        completed = handle_ring(cfg, device, operations=device, tmp_dir=Path(temp))
    status = "Completed" if completed else "Skipped"
    device.log(status, "Visit finished" if completed else "Known visitor bypassed the concierge")
    return jsonify(status=status, events=device.events)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    serve(app, host="127.0.0.1", port=8788)
