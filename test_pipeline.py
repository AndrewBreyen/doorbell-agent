#!/usr/bin/env python3
"""
test_pipeline.py

Test the classify -> LLM -> TTS pipeline WITHOUT touching the real doorbell
or Home Assistant. Useful for tuning model choice / prompt / speed without
having to walk outside and ring the bell every time.

Two modes:

  1. Text mode -- skips recording and whisper entirely, just tests the LLM
     and TTS steps (fastest way to iterate on prompts/models):

       python test_pipeline.py --text "Hi, I have a package for you"

  2. Audio mode -- runs a local audio file through whisper too, so you can
     also test transcription speed/accuracy. Record a quick clip with your
     Mac's own mic first:

       # record 5 seconds from your Mac's built-in mic
       ffmpeg -f avfoundation -i ":0" -t 5 -ac 1 -ar 16000 test_input.wav

       python test_pipeline.py --audio test_input.wav

Prints timing for each stage and plays the synthesized reply out loud
through your Mac's speakers (not the doorbell) with `afplay`.
"""

import argparse
import subprocess
import time
from pathlib import Path

from doorbell_agent import (
    classify_and_respond,
    load_config,
    synthesize_speech,
    transcribe,
)


def main():
    parser = argparse.ArgumentParser(description="Test the doorbell agent pipeline locally.")
    parser.add_argument("--text", help="Skip recording+whisper, use this as the transcript directly.")
    parser.add_argument("--audio", help="Path to a local .wav file to run through whisper.")
    args = parser.parse_args()

    if not args.text and not args.audio:
        parser.error("Pass either --text \"...\" or --audio path/to/file.wav")

    cfg = load_config()
    tmp_dir = Path("/tmp/doorbell_agent")
    tmp_dir.mkdir(exist_ok=True)

    total_start = time.time()

    if args.audio:
        print(f"Transcribing {args.audio} ...")
        t0 = time.time()
        transcript = transcribe(cfg, Path(args.audio))
        print(f"  -> whisper took {time.time() - t0:.2f}s")
        print(f"  -> transcript: \"{transcript}\"")
        if not transcript:
            print("No speech detected in that file, stopping.")
            return
    else:
        transcript = args.text
        print(f"Using provided text: \"{transcript}\"")

    print("Classifying + generating reply ...")
    t0 = time.time()
    intent, reply_text = classify_and_respond(cfg, transcript, history="")
    llm_time = time.time() - t0
    print(f"  -> LLM took {llm_time:.2f}s")
    print(f"  -> intent: {intent}")
    print(f"  -> reply: \"{reply_text}\"")

    print("Synthesizing speech ...")
    t0 = time.time()
    reply_path = tmp_dir / "test_reply.wav"
    synthesize_speech(cfg, reply_text, reply_path)
    tts_time = time.time() - t0
    print(f"  -> Piper took {tts_time:.2f}s")

    total_time = time.time() - total_start
    print(f"\nTotal pipeline time (excluding any recording): {total_time:.2f}s")

    print(f"\nPlaying reply locally: {reply_path}")
    subprocess.run(["afplay", str(reply_path)])


if __name__ == "__main__":
    main()