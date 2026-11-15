import unittest
from unittest.mock import patch

from doorbell import agent
from doorbell.simulator import app


class SimulatorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_dashboard_and_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for marker in (b'id="scenario"', b'id="events"', b'id="turn-template"', b"Simulate ring"):
            self.assertIn(marker, page.data)
        for path, content_type in (("/static/simulator.css", "text/css"), ("/static/simulator.js", "text/javascript")):
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content_type.startswith(content_type))

    def test_invalid_payloads(self):
        turn = {"transcript": "hello", "intent": "VISITOR"}
        for payload in (
            [], {"known_face": 1, "turns": [turn]}, {"known_face": False, "turns": []},
            {"known_face": False, "turns": [turn] * 4},
            {"known_face": False, "turns": [{"transcript": "x" * 501, "intent": "VISITOR"}]},
            {"known_face": False, "turns": [{"transcript": "hello", "intent": []}]},
        ):
            with self.subTest(payload=payload):
                result = self.client.post("/api/simulate", json=payload)
                self.assertEqual(result.status_code, 400)
                self.assertTrue(result.json["error"])
        self.assertEqual(self.client.post("/api/simulate", data="{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/simulate", data=b"x" * 17000, content_type="application/json").status_code, 413)

    def test_multiple_turns_do_not_touch_live_integrations(self):
        with patch.object(agent.requests, "post") as post, patch.object(agent.requests, "get") as get, \
             patch.object(agent.subprocess, "Popen") as popen:
            result = self.client.post("/api/simulate", json={
                "known_face": False,
                "turns": [
                    {"transcript": "  Hello  ", "intent": "VISITOR"},
                    {"transcript": "I'm here to deliver a package.", "intent": "DELIVERY"},
                ],
            })
        post.assert_not_called()
        get.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(result.status_code, 200)
        events = result.json["events"]
        self.assertEqual(sum(event["state"] == "Talkback played" for event in events), 2)
        summary = next(event["detail"] for event in events if event["state"] == "Notification sent")
        self.assertIn("Visitor: Hello\n", summary)
        self.assertIn("Intent: DELIVERY", summary)
