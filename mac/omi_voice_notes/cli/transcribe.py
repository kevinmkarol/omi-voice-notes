# ABOUTME: omi-transcribe: run existing WAVs (or a directory of them) through the same transcriber and sinks as the live tools.
# ABOUTME: For re-doing a day with a better model, or feeding recordings that arrived some other way into the archive.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omi_voice_notes.cli.common import (
    add_sink_args,
    add_transcriber_args,
    setup_logging,
    sinks_from_args,
    transcriber_from_args,
)
from omi_voice_notes.pipeline.transcribe import TranscriptionError, transcribe_recording


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="omi-transcribe", description="Transcribe WAV files through the Omi pipeline."
    )
    p.add_argument("paths", nargs="+", type=Path, help="WAV files or directories")
    p.add_argument(
        "--source", default="file", help="label stored with the transcript (default file)"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    add_transcriber_args(p, live=False)
    add_sink_args(p)
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    sinks = sinks_from_args(args)
    transcriber = transcriber_from_args(args, live=False)
    if transcriber is None:
        print("--engine none makes no sense here", file=sys.stderr)
        return 2
    files: list[Path] = []
    for path in args.paths:
        files.extend(sorted(path.rglob("*.wav")) if path.is_dir() else [path])
    failures = 0
    for wav in files:
        start = None
        meta = wav.with_suffix(".json")
        if meta.exists():
            try:
                start = json.loads(meta.read_text()).get("start_epoch")
            except ValueError:
                pass
        if start is None:
            start = wav.stat().st_mtime
        try:
            transcribe_recording(wav, transcriber, sinks, args.source, start)
        except TranscriptionError as exc:
            failures += 1
            print(f"{wav}: {exc}", file=sys.stderr)
    transcriber.close()
    sinks.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
