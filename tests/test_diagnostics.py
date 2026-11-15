from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import check_pipeline


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(StringIO()))

    def test_single_turn_uses_local_playback(self):
        with patch.object(check_pipeline, "classify_and_respond", return_value=("VISITOR", "Hello")), \
             patch.object(check_pipeline, "synthesize_speech") as synthesize, \
             patch.object(check_pipeline.subprocess, "run") as subprocess_run:
            result = check_pipeline.run_single_turn({}, Path("/tmp"), "Hi", 4)
        self.assertEqual(result, "VISITOR")
        synthesize.assert_called_once_with({}, "Hello", Path("/tmp/test_reply_4.wav"))
        subprocess_run.assert_called_once_with(["afplay", "/tmp/test_reply_4.wav"])

    def test_interactive_threads_history_and_allows_quit(self):
        with patch("builtins.input", side_effect=["first", "second", "quit"]), \
             patch.object(check_pipeline, "classify_and_respond", return_value=("SOLICITOR", "No thanks")) as respond, \
             patch.object(check_pipeline, "synthesize_speech"), patch.object(check_pipeline.subprocess, "run"):
            check_pipeline.run_interactive({}, Path("/tmp"))
        self.assertEqual(respond.call_args_list[0].args, ({}, "first", ""))
        self.assertEqual(respond.call_args_list[1].args, ({}, "second", "Visitor: first\nAssistant: No thanks"))

    def test_audio_cli_stops_when_transcript_is_blank(self):
        with patch("sys.argv", ["check_pipeline", "--audio", "clip.wav"]), \
             patch.object(check_pipeline, "load_config", return_value={}), \
             patch.object(Path, "mkdir"), patch.object(check_pipeline, "transcribe", return_value="") as transcribe, \
             patch.object(check_pipeline, "run_single_turn") as run:
            check_pipeline.main()
        transcribe.assert_called_once_with({}, Path("clip.wav"))
        run.assert_not_called()
