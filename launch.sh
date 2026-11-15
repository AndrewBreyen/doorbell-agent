#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() {
    printf 'Usage: %s [--simulator]\n' "$0"
    printf 'Start the live Doorbell Agent. Add --simulator to run without hardware.\n'
}

if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi

case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    ""|--simulator) ;;
    *) usage >&2; exit 2 ;;
esac

if [[ ! -x venv/bin/python ]] ||
    ! venv/bin/python -c 'import flask, requests, yaml, dotenv, waitress, numpy' 2>/dev/null; then
    printf 'Python dependencies are missing. Run ./setup.sh first.\n' >&2
    exit 1
fi

export PATH="$PWD/venv/bin:$PATH"

if [[ "${1:-}" == --simulator ]]; then
    exec venv/bin/python -m doorbell.simulator
fi

venv/bin/python - <<'PY'
from pathlib import Path
from shutil import which

from doorbell.agent import load_config

cfg = load_config()
if cfg["home_assistant"]["token"] == "YOUR_LONG_LIVED_ACCESS_TOKEN":
    raise SystemExit("Set HA_TOKEN in .env before launching the live webhook.")

models = cfg["models"]
problems = []

for executable in ("ffmpeg", models["whisper_binary"], models["piper_binary"]):
    if not which(executable):
        problems.append(f"Executable missing or inaccessible: {executable}")

for key in ("whisper_model", "piper_voice"):
    if not Path(models[key]).is_file():
        problems.append(f"Model file missing ({key}): {models[key]}")

if problems:
    raise SystemExit("\n".join([*problems, "See the README for live audio and model setup."]))
PY

ollama_ready() {
    curl --fail --silent --max-time 1 http://localhost:11434/api/tags >/dev/null
}

if ollama_ready; then
    exec venv/bin/python -m doorbell.server
fi

if ! command -v ollama >/dev/null 2>&1; then
    printf 'Ollama is unavailable. See the README for installation.\n' >&2
    exit 1
fi

ollama serve &
ollama_pid=$!
webhook_pid=
cleanup() {
    if [[ -n "$webhook_pid" ]]; then
        kill "$webhook_pid" 2>/dev/null || true
        wait "$webhook_pid" 2>/dev/null || true
    fi
    kill "$ollama_pid" 2>/dev/null || true
    wait "$ollama_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for ((attempt = 0; attempt < 30; attempt++)); do
    if ollama_ready; then
        venv/bin/python -m doorbell.server &
        webhook_pid=$!
        wait "$webhook_pid"
        exit
    fi
    if ! kill -0 "$ollama_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done

printf 'Ollama did not become ready. Check the Ollama output above.\n' >&2
exit 1
