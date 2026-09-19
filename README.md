# AI Doorbell Concierge — Setup Guide

Local, AI-powered doorstep assistant for a UniFi Protect doorbell. Home Assistant
(running on your VM) just detects the ring and forwards a trigger over your LAN;
all the actual STT/LLM/TTS processing runs on your Mac mini. No cloud APIs.

**Architecture:**
```
UniFi doorbell → HA (VM) → REST call over LAN → server.py (Mac mini)
                                                        ↓
                                     whisper.cpp → Ollama → Piper (all on Mac mini)
                                                        ↓
                                  HA REST API ← play audio + send notification
```

## What it does

- Ignores known/recognized faces (uses UniFi Protect's built-in face recognition) —
  the AI never engages with people you already know.
- For unknown visitors, listens to what they say, classifies their intent
  (delivery / visitor / solicitor / unclear), and responds appropriately:
  - Friendly and brief for deliveries and visitors
  - Firm and short for salespeople/solicitors — no small talk, no follow-up
- Sends you a push notification afterward with a transcript summary.

## 1. Install Home Assistant

```bash
# Requires Docker Desktop installed first
docker run -d --name homeassistant --restart=unless-stopped \
  -v /home/youruser/ha_config:/config \
  --network=host \
  ghcr.io/home-assistant/home-assistant:stable
```

Open `http://localhost:8123`, finish onboarding.

## 2. Connect UniFi Protect

- In HA: **Settings → Devices & Services → Add Integration → UniFi Protect**
- Enter your Protect controller's local IP + a local admin login
- In the UniFi Protect app itself, make sure **Face Recognition** is enabled and
  tag the faces of people you want auto-recognized (family, close friends, etc.)

Confirm you now have entities like:
- `binary_sensor.front_door_doorbell` (ring event)
- `binary_sensor.front_door_recognized_face` (known face flag)
- `media_player.front_door_speaker` (talkback playback)

Entity names vary — check **Settings → Devices & Services → Entities** and update
`config.yaml` accordingly.

## 3. Install the local AI stack (on the Mac mini)

```bash
# Whisper.cpp (speech-to-text)
brew install whisper-cpp
# Confirm the binary installed correctly:
which whisper-cli
# Download a model, e.g.:
curl -L -o ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

# Ollama (LLM)
brew install ollama
ollama pull llama3.1:8b

# Piper (text-to-speech) -- NOT available via Homebrew, install via pip instead
pip install piper-tts
# Download a voice model, e.g.:
curl -L -o en_US-lessac-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
curl -L -o en_US-lessac-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

# ffmpeg (audio capture from RTSP)
brew install ffmpeg
```

## 4. Set up a Python virtual environment and configure the agent

macOS's Homebrew-managed Python blocks installing packages system-wide (PEP 668),
so this project uses a virtual environment — an isolated set of Python packages
just for this folder.

```bash
cd doorbell_agent
python3 -m venv venv
source venv/bin/activate   # run this every time you open a new terminal for this project
pip install requests pyyaml python-dotenv flask piper-tts numpy
cp config.example.yaml config.yaml
cp .env.example .env
# Edit config.yaml: HA URL, entity names, model paths
# Edit .env: your real HA long-lived access token
```

Generate an HA long-lived token: **HA Profile (bottom left) → Security →
Long-Lived Access Tokens → Create Token**. Put it in `.env` as `HA_TOKEN=...`
— this file is gitignored, so it's safe from accidentally being committed.
`config.yaml` no longer holds any secrets, so it's fine to commit changes to it.

`venv/` is also gitignored — it's a local, machine-specific folder and should
never be pushed to the repo.

## 5. Start the webhook server on the Mac mini

```bash
source venv/bin/activate   # if not already active in this terminal
python server.py
```

This listens on port `8787` for the trigger from HA. Keep it running (see
"Keeping it running" below for a way to auto-start it).

Find your Mac mini's LAN IP (`ipconfig getifaddr en0`) and set a DHCP
reservation for it in your router so it doesn't change.

## 6. Wire up Home Assistant (on the VM)

- Add the `rest_command` block from `ha_automation.yaml`'s comments to your HA
  `configuration.yaml`, using your Mac mini's actual LAN IP
- Add the automation itself via **Settings → Automations → Edit in YAML**,
  pasting the contents of `ha_automation.yaml`
- Restart Home Assistant

Since HA now only makes an outbound REST call, the VM never needs local access
to Python, whisper.cpp, Ollama, or Piper — all of that stays on the Mac mini.

## 7. Test it

- Have someone NOT tagged as a known face ring the doorbell
- You should hear the assistant greet them, classify what they need, and respond
- Check your phone for the notification summary afterward
- Tune `prompts.py` if the tone isn't quite right (e.g., make the solicitor
  response firmer/softer to taste)

## Files in this project

| File | Purpose |
|---|---|
| `server.py` | Webhook server — runs on the Mac mini, receives HA's trigger |
| `doorbell_agent.py` | Core logic: known-face check → STT → classify → respond → TTS → notify |
| `prompts.py` | The classification and response prompts — tune tone here |
| `config.example.yaml` | Template config — copy to `config.yaml` and fill in |
| `ha_automation.yaml` | Home Assistant automation + `rest_command` that calls the Mac mini |

## Keeping it running

`server.py` needs to stay running on the Mac mini. Easiest option — a `launchd`
agent so it starts on boot and restarts if it crashes:

```bash
# ~/Library/LaunchAgents/com.doorbell.agent.plist
```
```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.doorbell.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/doorbell_agent/venv/bin/python3</string>
    <string>/path/to/doorbell_agent/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/doorbell_agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
Then: `launchctl load ~/Library/LaunchAgents/com.doorbell.agent.plist`

Note the `ProgramArguments` above points at `venv/bin/python3`, not the system
Python — this ensures launchd runs the server with the packages you installed
in step 4.

## Testing

Set `known_face_check_enabled: false` in `config.yaml` to make the agent engage
with *everyone*, including known faces — useful while you're testing the
conversation flow without needing an "unknown" visitor. Flip it back to `true`
once you're happy with it.

## Tuning tips

- **Too slow?** Use a smaller Ollama model (e.g., `phi3:mini`) or a smaller
  Whisper model (`ggml-tiny.en.bin`) — trade a little accuracy for speed.
- **Too aggressive/not aggressive enough with solicitors?** Edit the SOLICITOR
  rule in `prompts.py`'s `RESPONSE_PROMPT`.
- **False known-face negatives?** Improve tagging in UniFi Protect's Face
  Recognition settings — the agent only skips people Protect actually recognizes.