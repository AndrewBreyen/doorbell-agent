import contextlib
import io
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from doorbell import agent


MODELS = {
    "whisper_binary": "whisper-cli",
    "whisper_model": "whisper.bin",
    "ollama_model": "example-model",
    "piper_binary": "piper",
    "piper_voice": "voice.onnx",
}


class ConfigTests(unittest.TestCase):
    def test_loads_yaml_and_token_from_project_env(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            cfg = Path(directory) / "config.yaml"
            cfg.write_text("home_assistant:\n  url: http://homeassistant.local\n")
            cfg.with_name(".env").write_text("HA_TOKEN=test-token\n")
            with patch.object(agent, "CONFIG_PATH", cfg):
                result = agent.load_config()
                self.assertEqual(result["home_assistant"]["token"], "test-token")
                os.environ["HA_TOKEN"] = "environment-token"
                self.assertEqual(agent.load_config()["home_assistant"]["token"], "environment-token")

    def test_missing_config_and_missing_token_report_their_causes(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            cfg = Path(directory) / "config.yaml"
            with patch.object(agent, "CONFIG_PATH", cfg):
                with self.assertRaisesRegex(SystemExit, "Missing config.yaml"):
                    agent.load_config()
                cfg.write_text("home_assistant: {}\n")
                with self.assertRaisesRegex(SystemExit, "Missing HA_TOKEN"):
                    agent.load_config()


class HomeAssistantTests(unittest.TestCase):
    def setUp(self):
        self.ha = agent.HomeAssistant({
            "home_assistant": {"url": "http://ha.local/", "token": "test-token"}
        })

    @patch.object(agent.requests, "get")
    def test_state_request(self, get):
        get.return_value.json.return_value = {"state": "on"}
        self.assertEqual(self.ha.get_state("binary_sensor.face"), "on")
        get.assert_called_once_with(
            "http://ha.local/api/states/binary_sensor.face", headers=self.ha.headers, timeout=5
        )
        self.assertEqual(self.ha.headers["Authorization"], "Bearer test-token")
        get.return_value.raise_for_status.assert_called_once()

    @patch.object(agent.requests, "post")
    def test_services_send_expected_payloads_and_propagate_http_errors(self, post):
        self.ha.notify("notify.mobile", "Test summary")
        post.assert_called_with(
            "http://ha.local/api/services/notify/mobile",
            headers=self.ha.headers, json={"title": "Doorbell", "message": "Test summary"}, timeout=10,
        )
        self.ha.play_audio_on_doorbell("media_player.front", "http://agent.local/audio/reply.wav")
        post.assert_called_with(
            "http://ha.local/api/services/media_player/play_media", headers=self.ha.headers, timeout=10,
            json={
                "entity_id": "media_player.front", "media_content_id": "http://agent.local/audio/reply.wav",
                "media_content_type": "music",
            },
        )
        post.return_value.raise_for_status.side_effect = requests.HTTPError
        with self.assertRaises(requests.HTTPError):
            self.ha.notify("notify.mobile", "Test summary")

    def test_face_recognition_disabled_missing_and_unreachable(self):
        ha = Mock()
        cfg = {"behavior": {}, "home_assistant": {}}
        self.assertFalse(agent.is_known_face(ha, cfg))
        cfg["home_assistant"]["known_face_sensor"] = "binary_sensor.face"
        cfg["behavior"]["known_face_check_enabled"] = False
        self.assertFalse(agent.is_known_face(ha, cfg))
        ha.get_state.assert_not_called()
        cfg["behavior"]["known_face_check_enabled"] = True
        ha.get_state.return_value = "off"
        self.assertFalse(agent.is_known_face(ha, cfg))
        ha.get_state.side_effect = requests.Timeout
        self.assertFalse(agent.is_known_face(ha, cfg))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"models": MODELS, "behavior": {"household_name": "Example house"}}

    @patch.object(agent.subprocess, "run")
    def test_whisper_invocation_and_failure(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "  hello   \n", "")
        self.assertEqual(agent.transcribe(self.cfg, Path("clip.wav")), "hello")
        run.assert_called_once_with(
            ["whisper-cli", "-m", "whisper.bin", "-f", "clip.wav", "-nt"], capture_output=True, text=True,
        )
        run.return_value = subprocess.CompletedProcess([], 1, "", "missing model")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError) as error:
            agent.transcribe(self.cfg, Path("clip.wav"))
        self.assertEqual(error.exception.stderr, "missing model")

    @patch.object(agent.subprocess, "run")
    def test_piper_invocation_pronunciation_and_failure(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        agent.synthesize_speech(self.cfg, "BREYEN or Breyenwood", Path("reply.wav"))
        run.assert_called_once_with(
            ["piper", "--model", "voice.onnx", "--output_file", "reply.wav"],
            input="Brian or Breyenwood", capture_output=True, text=True,
        )
        run.return_value = subprocess.CompletedProcess([], 2, "", "bad voice")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError) as error:
            agent.synthesize_speech(self.cfg, "Hello", Path("reply.wav"))
        self.assertEqual(error.exception.returncode, 2)

    @patch.object(agent.requests, "post")
    def test_ollama_request_and_http_failure(self, post):
        post.return_value.json.return_value = {"response": "  reply  "}
        self.assertEqual(agent.ollama_generate(self.cfg, "prompt"), "reply")
        post.assert_called_once_with(
            "http://localhost:11434/api/generate", timeout=30,
            json={"model": "example-model", "prompt": "prompt", "stream": False, "options": {"temperature": 0.2}},
        )
        post.return_value.raise_for_status.side_effect = requests.HTTPError
        with self.assertRaises(requests.HTTPError):
            agent.ollama_generate(self.cfg, "prompt")

    @patch.object(agent, "ollama_generate")
    def test_combined_prompt_and_fenced_json(self, generate):
        generate.return_value = '```json\n{"intent":"delivery","reply":" Leave it here. "}\n```'
        result = agent.classify_and_respond(self.cfg, "Looking for Alex", "Visitor: first turn")
        self.assertEqual(result, ("DELIVERY", "Leave it here."))
        generate.assert_called_once()
        for text in ("Example house", "Looking for Alex", "Visitor: first turn"):
            self.assertIn(text, generate.call_args.args[1])

    @patch.object(agent, "ollama_generate")
    def test_malformed_and_wrongly_typed_model_responses_fall_back(self, generate):
        for raw in ("invalid", "[]", "null", '{"intent":"other","reply":"hi"}',
                    '{"intent":"VISITOR","reply":null}', '{"intent":"DELIVERY","reply":"  "}'):
            with self.subTest(raw=raw):
                generate.return_value = raw
                self.assertEqual(
                    agent.classify_and_respond(self.cfg, "hello", ""),
                    ("UNCLEAR", "Hi, can I help you with something?"),
                )
