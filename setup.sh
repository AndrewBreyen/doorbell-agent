#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

case "${1:-}" in
    --help|-h)
        printf 'Usage: %s [--simulator]\n' "$0"
        printf 'Install Python dependencies and create live configuration files. Use --simulator to skip live configuration.\n'
        exit 0
        ;;
    ""|--simulator) ;;
    *)
        printf 'Usage: %s [--simulator]\n' "$0" >&2
        exit 2
        ;;
esac

if [[ $# -gt 1 ]]; then
    printf 'Usage: %s [--simulator]\n' "$0" >&2
    exit 2
fi

if [[ ! -x venv/bin/python ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        printf 'Install Python 3, then run ./setup.sh again.\n' >&2
        exit 1
    fi
    python3 -m venv venv
fi

venv/bin/python -m pip install flask requests pyyaml python-dotenv waitress

if [[ "${1:-}" == --simulator ]]; then
    printf '\nSetup complete. Start the simulator with ./launch.sh --simulator.\n'
    exit 0
fi

setup_live_config() {
    if [[ ! -e config.yaml && ! -L config.yaml ]]; then
        cp config.example.yaml config.yaml
    fi
    if [[ ! -e .env && ! -L .env ]]; then
        printf 'Home Assistant long-lived access token (input hidden): ' >&2
        if ! IFS= read -rs token || [[ -z "${token//[[:space:]]/}" ]]; then
            printf '\nA token is required to create .env. Run ./setup.sh again when you have it.\n' >&2
            exit 1
        fi
        printf '\n' >&2
        token=${token//\\/\\\\}
        token=${token//\'/\\\'}
        (umask 077; set -o noclobber; printf "HA_TOKEN='%s'\n" "$token" > .env)
        unset token
    fi
}

install_live_tools() {
    local formulas=()
    command -v ffmpeg >/dev/null || formulas+=(ffmpeg)
    command -v whisper-cli >/dev/null || formulas+=(whisper-cpp)
    command -v ollama >/dev/null || formulas+=(ollama)
    if [[ ! -x venv/piper-env/bin/python ]] && ! command -v python3.11 >/dev/null; then
        formulas+=(python@3.11)
    fi
    if [[ -n "${formulas[*]:-}" ]]; then
        if ! command -v brew >/dev/null; then
            printf 'Install Homebrew from https://brew.sh/ and rerun ./setup.sh.\n' >&2
            return 1
        fi
        brew install "${formulas[@]}"
    fi
    if [[ ! -x venv/piper-env/bin/python ]]; then
        python3.11 -m venv venv/piper-env
    fi
    if [[ ! -x venv/piper-env/bin/piper ]]; then
        venv/piper-env/bin/python -m pip install 'piper-tts>=1.4.0'
    fi
}

download_model() (
    url=$1
    destination=$2
    if [[ -s "$destination" ]]; then
        exit 0
    fi
    partial=$(mktemp "${destination}.download.XXXXXX")
    trap 'rm -f -- "$partial"' EXIT
    printf 'Downloading %s\n' "$destination"
    curl --fail --location --retry 3 --connect-timeout 15 --output "$partial" "$url"
    test -s "$partial"
    mv -f -- "$partial" "$destination"
)

setup_live_config
install_live_tools
mkdir -p models
download_model https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin models/ggml-base.en.bin
voice_url=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
download_model "$voice_url" models/en_US-lessac-medium.onnx
download_model "$voice_url.json" models/en_US-lessac-medium.onnx.json

# Migrate the old example paths while retaining comments and custom settings.
venv/bin/python - <<'PYTHON'
import re
from pathlib import Path

path = Path("config.yaml")
original = path.read_text()
updated = original
paths = {
    "whisper_binary": ("/usr/local/bin/whisper-cli", "whisper-cli"),
    "piper_binary": ("/usr/local/bin/piper", "venv/piper-env/bin/piper"),
    "whisper_model": ("/home/user/models/ggml-base.en.bin", "models/ggml-base.en.bin"),
    "piper_voice": ("/home/user/models/en_US-lessac-medium.onnx", "models/en_US-lessac-medium.onnx"),
}
for key, (old, new) in paths.items():
    # The legacy binary paths may point to valid installations on Intel Macs.
    if Path(old).is_file():
        continue
    updated = re.sub(
        rf"^(  {key}:[ \t]*)(['\"]?){re.escape(old)}\2(?=[ \t]*(?:#|$))",
        lambda match: f'{match[1]}"{new}"',
        updated,
        flags=re.MULTILINE,
    )
if updated != original:
    path.write_text(updated)
PYTHON

printf '\nLive dependencies installed. Review config.yaml, then run ./launch.sh.\n'
