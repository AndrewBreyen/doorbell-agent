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
import itertools
import subprocess
import time
from pathlib import Path

from doorbell_agent import (
    classify_and_respond,
    load_config,
    synthesize_speech,
    transcribe,
)


def run_single_turn(cfg, tmp_dir, transcript, turn_num=0):
    """Runs one turn through classify+respond+TTS and plays it back. Returns intent."""
    print(f"Visitor: \"{transcript}\"")

    t0 = time.time()
    intent, reply_text = classify_and_respond(cfg, transcript, history="")
    print(f"  -> LLM took {time.time() - t0:.2f}s | intent: {intent}")
    print(f"  -> reply: \"{reply_text}\"")

    t0 = time.time()
    reply_path = tmp_dir / f"test_reply_{turn_num}.wav"
    synthesize_speech(cfg, reply_text, reply_path)
    print(f"  -> Piper took {time.time() - t0:.2f}s")

    subprocess.run(["afplay", str(reply_path)])
    return intent


def run_interactive(cfg, tmp_dir):
    """
    Multi-turn test loop, mirroring run_visit()'s conversational logic (history
    carried between turns, stops early on a SOLICITOR reply) but loops
    indefinitely instead of capping at max_exchanges -- this is for casual
    testing, not simulating a real visit's turn limit.
    """
    history_lines = []

    print("Interactive test -- runs until you type 'quit' (or hit Enter on empty).\n")

    for turn in itertools.count():
        transcript = input(f"[Turn {turn + 1}] Visitor says: ").strip()
        if not transcript or transcript.lower() == "quit":
            print("Ending session.")
            break

        t0 = time.time()
        intent, reply_text = classify_and_respond(cfg, transcript, "\n".join(history_lines))
        print(f"  -> LLM took {time.time() - t0:.2f}s | intent: {intent}")
        print(f"  -> reply: \"{reply_text}\"")

        history_lines.append(f"Visitor: {transcript}")
        history_lines.append(f"Assistant: {reply_text}")

        t0 = time.time()
        reply_path = tmp_dir / f"test_reply_{turn}.wav"
        synthesize_speech(cfg, reply_text, reply_path)
        print(f"  -> Piper took {time.time() - t0:.2f}s")

        subprocess.run(["afplay", str(reply_path)])

        if intent == "SOLICITOR":
            print("(Note: the real doorbell agent would stop engaging here -- this test loop keeps going.)")
        print()


def main():
    parser = argparse.ArgumentParser(description="Test the doorbell agent pipeline locally.")
    parser.add_argument("--text", help="Skip recording+whisper, use this as the transcript directly (single turn).")
    parser.add_argument("--audio", help="Path to a local .wav file to run through whisper (single turn).")
    parser.add_argument(
        "--interactive", action="store_true",
        help="Multi-turn mode -- type each visitor line, loops up to max_exchanges like a real visit."
    )
    args = parser.parse_args()

    if not args.text and not args.audio and not args.interactive:
        parser.error("Pass --text \"...\", --audio path/to/file.wav, or --interactive")

    cfg = load_config()
    tmp_dir = Path("/tmp/doorbell_agent")
    tmp_dir.mkdir(exist_ok=True)

    if args.interactive:
        run_interactive(cfg, tmp_dir)
        return

    total_start = time.time()

    if args.audio:
        print(f"Transcribing {args.audio} ...")
        t0 = time.time()
        transcript = transcribe(cfg, Path(args.audio))
        print(f"  -> whisper took {time.time() - t0:.2f}s")
        if not transcript:
            print("No speech detected in that file, stopping.")
            return
    else:
        transcript = args.text

    run_single_turn(cfg, tmp_dir, transcript)
    print(f"\nTotal pipeline time (excluding any recording): {time.time() - total_start:.2f}s")


if __name__ == "__main__":
    main()