import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch
import wave

from doorbell import agent
from doorbell.simulator import app as simulator


def config(exchanges=2):
    return {
        "behavior": {
            "max_exchanges": exchanges,
            "agent_base_url": "http://doorbell.local:8787",
            "notify_service": "notify.test",
        },
        "home_assistant": {
            "doorbell_stream_url": "rtsp://example.invalid/audio",
            "doorbell_talkback_entity": "media_player.doorbell",
        },
    }


class RecordingTests(unittest.TestCase):
    def record(self, program, limit=1):
        cfg = config()
        cfg["behavior"].update(max_clip_seconds=limit, silence_duration_seconds=0.2)
        popen = subprocess.Popen
        children = []

        def launch(cmd, **kwargs):
            child = popen([sys.executable, "-c", program], **kwargs)
            children.append(child)
            return child

        with TemporaryDirectory() as temp, patch.object(agent.subprocess, "Popen", side_effect=launch):
            clip = Path(temp) / "clip.wav"
            started = time.monotonic()
            spoken = agent.record_clip(cfg, clip)
            elapsed = time.monotonic() - started
            with wave.open(str(clip), "rb") as audio:
                frames = audio.getnframes()
        self.assertIsNotNone(children[0].returncode)
        return spoken, elapsed, frames

    def test_stalled_stream_hits_wall_clock_deadline(self):
        spoken, elapsed, frames = self.record("import time; time.sleep(30)", limit=0.25)
        self.assertFalse(spoken)
        self.assertEqual(frames, 0)
        self.assertLess(elapsed, 2)

    def test_speech_then_silence_ends_capture(self):
        spoken, _, frames = self.record(
            "import os, struct, time; "
            "os.write(1, struct.pack('<h', 1200) * 1600 + bytes(9600)); time.sleep(30)"
        )
        self.assertTrue(spoken)
        self.assertEqual(frames, 4800)

    def test_short_clip_at_eof_handles_pcm_alignment(self):
        spoken, _, frames = self.record("import os; os.write(1, bytes([0, 8, 0]))")
        self.assertTrue(spoken)
        self.assertEqual(frames, 1)

    def test_silent_clip_is_not_speech(self):
        spoken, _, frames = self.record("import os; os.write(1, bytes(3200))")
        self.assertFalse(spoken)
        self.assertEqual(frames, 1600)


class VisitTests(unittest.TestCase):
    def setUp(self):
        self.audio = TemporaryDirectory()
        self.addCleanup(self.audio.cleanup)
        self.audio_dir = Path(self.audio.name)
        self.ha = Mock()
        self.operations = Mock()
        self.operations.record.return_value = True
        self.operations.transcribe.return_value = "Hello"
        self.operations.classify_and_respond.return_value = ("VISITOR", "Hi")
        self.operations.synthesize.side_effect = self.synthesize

    def synthesize(self, cfg, reply, path):
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(bytes(64000))

    def test_unique_audio_names_timing_and_playback_wait(self):
        with self.assertLogs(agent.logger, level="INFO") as logs:
            for _ in range(2):
                agent.handle_ring(config(), self.ha, operations=self.operations, tmp_dir=self.audio_dir)

        self.assertEqual([call.args for call in self.operations.pause.call_args_list], [(3.0,), (3.0,)])
        self.assertEqual(self.ha.notify.call_count, 2)
        urls = [call.args[1] for call in self.ha.play_audio_on_doorbell.call_args_list]
        self.assertEqual(len(set(urls)), 4)
        for url in urls:
            self.assertRegex(url, r"/audio/reply_[a-f0-9]{32}-[01]\.wav$")
            self.assertTrue((self.audio_dir / url.rsplit("/", 1)[1]).exists())
        for call in self.operations.record.call_args_list:
            self.assertFalse(call.args[1].parent.exists())
        output = "\n".join(logs.output)
        for stage in ("face_check", "capture", "whisper", "ollama", "piper", "playback_request", "notification"):
            self.assertIn(f"stage={stage}", output)
        self.assertIn("ring_to_first_reply_request=", output)
        self.assertIn("total=", output)

    def test_expiry_keeps_recent_replies_and_unrelated_files(self):
        old = self.audio_dir / "reply_old-0.wav"
        recent = self.audio_dir / "reply_recent-0.wav"
        unrelated = self.audio_dir / "unrelated.wav"
        for path in (old, recent, unrelated):
            path.touch()
        expired = time.time() - agent.REPLY_RETENTION_SECONDS - 60
        for path in (old, unrelated):
            os.utime(path, (expired, expired))
        agent.expire_replies(self.audio_dir)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())

    def test_solicitor_ends_even_when_turns_remain(self):
        self.operations.classify_and_respond.return_value = ("SOLICITOR", "No thanks")
        agent.handle_ring(config(), self.ha, operations=self.operations, tmp_dir=self.audio_dir)
        self.operations.record.assert_called_once()
        self.operations.pause.assert_not_called()
        self.assertIn("Intent: SOLICITOR", self.ha.notify.call_args.kwargs["message"])

    def test_known_face_skips_visit(self):
        cfg = config()
        cfg["home_assistant"]["known_face_sensor"] = "binary_sensor.recognized"
        self.ha.get_state.return_value = "on"
        self.assertFalse(agent.handle_ring(cfg, self.ha, operations=self.operations, tmp_dir=self.audio_dir))
        self.operations.record.assert_not_called()
        self.ha.notify.assert_not_called()

    def test_silence_skips_transcription(self):
        self.operations.record.return_value = False
        agent.handle_ring(config(), self.ha, operations=self.operations, tmp_dir=self.audio_dir)
        self.operations.transcribe.assert_not_called()
        self.assertIn("no speech detected", self.ha.notify.call_args.kwargs["message"])


class WebhookTests(unittest.TestCase):
    def setUp(self):
        audio = TemporaryDirectory()
        self.addCleanup(audio.cleanup)
        spec = importlib.util.spec_from_file_location("doorbell.webhook_under_test", Path(agent.__file__).with_name("server.py"))
        self.server = importlib.util.module_from_spec(spec)
        with patch.object(agent, "load_config", return_value=config()), patch.object(agent, "AUDIO_DIR", Path(audio.name)):
            spec.loader.exec_module(self.server)
        self.client = self.server.app.test_client()

    def test_overlapping_ring_gets_busy_and_next_ring_can_start(self):
        entered = threading.Event()
        release = threading.Event()

        def visit(cfg, ha):
            entered.set()
            release.wait(timeout=2)
            return True

        with patch.object(self.server, "HomeAssistant"), patch.object(self.server, "handle_ring", side_effect=visit):
            try:
                first = self.client.post("/trigger")
                self.assertEqual((first.status_code, first.json["status"]), (202, "started"))
                self.assertTrue(entered.wait(timeout=2))
                duplicate = self.client.post("/trigger")
                self.assertEqual((duplicate.status_code, duplicate.json["status"]), (202, "busy"))
            finally:
                release.set()
                self.assertTrue(self.server.visit_lock.acquire(timeout=2))
                self.server.visit_lock.release()
            self.assertEqual(self.client.post("/trigger").json["status"], "started")
            self.assertTrue(self.server.visit_lock.acquire(timeout=2))
            self.server.visit_lock.release()

    def test_failed_visit_releases_lock(self):
        self.server.visit_lock.acquire()
        with patch.object(self.server, "HomeAssistant"), patch.object(self.server, "handle_ring", side_effect=RuntimeError), self.assertLogs(self.server.logger, "ERROR"):
            self.server.handle_visit()
        self.assertFalse(self.server.visit_lock.locked())

    def test_thread_start_failure_releases_lock(self):
        with patch.object(self.server.threading, "Thread") as thread, self.assertLogs(self.server.app.logger, "ERROR"):
            thread.return_value.start.side_effect = RuntimeError
            self.assertEqual(self.client.post("/trigger").status_code, 500)
        self.assertFalse(self.server.visit_lock.locked())

    def test_serves_generated_reply(self):
        name = "reply_123-0.wav"
        (self.server.AUDIO_DIR / name).write_bytes(b"reply audio")
        response = self.client.get(f"/audio/{name}")
        self.addCleanup(response.close)
        self.assertEqual(response.data, b"reply audio")


class SimulatorTests(unittest.TestCase):
    def test_presets_run_through_shared_pipeline(self):
        client = simulator.test_client()
        for intent in ("DELIVERY", "VISITOR", "SOLICITOR", "UNCLEAR"):
            with self.subTest(intent=intent):
                result = client.post("/api/simulate", json={
                    "known_face": False, "turns": [{"transcript": "Hello", "intent": intent}]
                })
                self.assertEqual(result.json["status"], "Completed")
                self.assertTrue(any(
                    event["state"] == "Notification sent" and f"Intent: {intent}" in event["detail"]
                    for event in result.json["events"]
                ))
        for is_known in (True, False):
            with self.subTest(known_face=is_known):
                result = client.post("/api/simulate", json={
                    "known_face": is_known, "turns": [{"transcript": "", "intent": "UNCLEAR"}]
                })
                self.assertEqual(result.json["status"], "Skipped" if is_known else "Completed")
                self.assertFalse(any(event["state"] == "Talkback played" for event in result.json["events"]))


if __name__ == "__main__":
    unittest.main()
