# ABOUTME: Flags and helpers every omi-* tool shares: logging, sink selection (archive, optional webhook), transcriber selection, terminal echo.
# ABOUTME: Defaults are local-first: archive + terminal only; the webhook is opt-in per run.
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime

from omi_voice_notes.pipeline.sinks import DEFAULT_ARCHIVE, Sinks, Transcript, build_sinks
from omi_voice_notes.pipeline.transcribe import (
    DEFAULT_SERVER_PORT,
    Transcriber,
    TranscriptionError,
    make_transcriber,
)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("bleak").setLevel(logging.INFO if verbose else logging.WARNING)


def add_sink_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("where transcripts go")
    g.add_argument(
        "--archive",
        metavar="DIR",
        default=os.environ.get("OMI_NOTES_ARCHIVE", str(DEFAULT_ARCHIVE)),
        help=f"recordings/, transcripts/, events/ (default $OMI_NOTES_ARCHIVE or {DEFAULT_ARCHIVE})",
    )
    g.add_argument(
        "--webhook",
        metavar="URL",
        default=os.environ.get("OMI_NOTES_WEBHOOK"),
        help="also POST each transcript as JSON to this URL (default $OMI_NOTES_WEBHOOK; see docs/INTEGRATIONS.md)",
    )
    g.add_argument(
        "--webhook-token",
        metavar="TOKEN",
        default=os.environ.get("OMI_NOTES_WEBHOOK_TOKEN"),
        help="sent as a bearer token with each webhook POST (default $OMI_NOTES_WEBHOOK_TOKEN)",
    )
    g.add_argument("--quiet", action="store_true", help="don't print transcripts to the terminal")


def add_transcriber_args(p: argparse.ArgumentParser, live: bool) -> None:
    g = p.add_argument_group("transcription")
    g.add_argument(
        "--engine",
        choices=["auto", "server", "cli", "none"],
        default="auto",
        help="auto = whisper-server for live audio, whisper-cli for whole files; none = record only",
    )
    g.add_argument(
        "--whisper-url",
        metavar="URL",
        help="use an already running whisper-server (e.g. http://127.0.0.1:8785)",
    )
    p.set_defaults(_live=live)


def sinks_from_args(args: argparse.Namespace) -> Sinks:
    return build_sinks(
        args.archive, args.webhook, args.webhook_token, None if args.quiet else echo_transcript
    )


def transcriber_from_args(args: argparse.Namespace, live: bool | None = None) -> Transcriber | None:
    if args.engine == "none":
        return None
    try:
        return make_transcriber(
            args.engine, args.whisper_url, live if live is not None else args._live
        )
    except TranscriptionError as exc:
        print(f"transcription unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def echo_transcript(t: Transcript) -> None:
    stamp = t.start.strftime("%H:%M:%S") if t.start else "--:--:--"
    print(f"[{stamp} {t.source} {t.duration_s:4.0f}s] {t.text}", flush=True)


def fmt_clock(epoch: float | None) -> str:
    return (
        "unknown"
        if epoch is None
        else datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    )


__all__ = [
    "DEFAULT_SERVER_PORT",
    "add_sink_args",
    "add_transcriber_args",
    "echo_transcript",
    "fmt_clock",
    "setup_logging",
    "sinks_from_args",
    "transcriber_from_args",
]  # fmt: skip
