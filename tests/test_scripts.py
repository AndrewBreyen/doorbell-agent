import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from dotenv import dotenv_values
import yaml


ROOT = Path(__file__).resolve().parents[1]


class ScriptTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        for name in ("setup.sh", "launch.sh", "config.example.yaml"):
            shutil.copy2(ROOT / name, self.root)
        shutil.copytree(ROOT / "doorbell", self.root / "doorbell", ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin"}
        self.env.pop("HA_TOKEN", None)
        python = shlex.quote(sys.executable)
        self.stub("venv/bin/python", f"""
case "$1 ${{2:-}}" in
    '-m pip') exit 0 ;;
    '-m doorbell.server'|'-m doorbell.simulator') printf '%s\\n' "$2"; exit 0 ;;
esac
exec {python} "$@"
""")
        self.stub("venv/piper-env/bin/python", "exit 0")
        self.stub("venv/piper-env/bin/piper", "exit 0")
        self.stub("bin/ffmpeg", "exit 0")
        self.stub("bin/whisper-cli", "exit 0")
        self.stub("bin/ollama", "exit 0")
        self.stub("bin/curl", """
for arg in "$@"; do
    if [[ "$arg" == --output ]]; then
        output=next
    elif [[ "${output:-}" == next ]]; then
        printf 'fake model' > "$arg"
        echo download >> downloads.log
        exit 0
    fi
done
test -f ollama-ready
""")

    def stub(self, destination, script):
        path = self.root / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/bash\nset -eu\n{script}\n")
        path.chmod(0o755)
        return path

    def run_script(self, script, *args, stdin=""):
        return subprocess.run(
            [str(self.root / script), *args], cwd=self.root.parent,
            env=self.env, input=stdin, text=True, capture_output=True, timeout=10,
        )

    def assert_succeeded(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_simulator_setup_skips_live_configuration_and_launches_module(self):
        self.assert_succeeded(self.run_script("setup.sh", "--simulator"))
        for name in (".env", "config.yaml", "models", "downloads.log"):
            self.assertFalse((self.root / name).exists())
        launched = self.run_script("launch.sh", "--simulator")
        self.assert_succeeded(launched)
        self.assertEqual(launched.stdout.strip(), "doorbell.simulator")

    def test_live_setup_token_and_idempotent_model_download(self):
        env_file = self.root / ".env"
        self.assertNotEqual(self.run_script("setup.sh", stdin="\n").returncode, 0)
        self.assertFalse(env_file.exists())
        initial = self.run_script("setup.sh", stdin="test-token\n")
        self.assert_succeeded(initial)
        self.assertIn("input hidden", initial.stderr)
        self.assertNotIn("test-token", initial.stdout + initial.stderr)
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(dotenv_values(env_file)["HA_TOKEN"], "test-token")
        for name in ("ggml-base.en.bin", "en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json"):
            self.assertTrue((self.root / "models" / name).is_file())
        rerun = self.run_script("setup.sh")
        self.assert_succeeded(rerun)
        self.assertNotIn("input hidden", rerun.stderr)
        self.assertEqual((self.root / "downloads.log").read_text().splitlines(), ["download"] * 3)

    def test_setup_migrates_legacy_model_path_and_preserves_custom_settings(self):
        fixture = self.root / "config.yaml"
        fixture.write_text(
            'home_assistant: {url: "http://test.local"}\nmodels:\n'
            '  whisper_model: "/home/user/models/ggml-base.en.bin"  # keep this comment\n'
            '  piper_voice: "my-custom-voice.onnx"\n'
        )
        self.assert_succeeded(self.run_script("setup.sh", stdin="test-token\n"))
        updated = fixture.read_text()
        self.assertIn("# keep this comment", updated)
        config = yaml.safe_load(updated)
        self.assertEqual(config["home_assistant"]["url"], "http://test.local")
        self.assertEqual(config["models"]["whisper_model"], "models/ggml-base.en.bin")
        self.assertEqual(config["models"]["piper_voice"], "my-custom-voice.onnx")

    def test_live_launch_validates_models_and_reuses_running_ollama(self):
        self.assert_succeeded(self.run_script("setup.sh", stdin="test-token\n"))
        (self.root / "ollama-ready").touch()
        launched = self.run_script("launch.sh")
        self.assert_succeeded(launched)
        self.assertEqual(launched.stdout.strip(), "doorbell.server")
        (self.root / "models/ggml-base.en.bin").unlink()
        failed = self.run_script("launch.sh")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Model file missing (whisper_model)", failed.stderr)

    def test_live_launch_starts_and_stops_its_own_ollama(self):
        self.assert_succeeded(self.run_script("setup.sh", stdin="test-token\n"))
        self.stub("bin/ollama", """
echo "$$" > ollama.pid
touch ollama-ready
exec /bin/sleep 60
""")
        launched = self.run_script("launch.sh")
        self.assert_succeeded(launched)
        self.assertEqual(launched.stdout.strip(), "doorbell.server")
        pid = int((self.root / "ollama.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_usage_and_missing_python(self):
        for script in ("setup.sh", "launch.sh"):
            with self.subTest(script=script):
                self.assert_succeeded(self.run_script(script, "--help"))
                self.assertEqual(self.run_script(script, "--invalid").returncode, 2)
        (self.root / "venv/bin/python").unlink()
        self.assertIn("Run ./setup.sh first", self.run_script("launch.sh", "--simulator").stderr)
