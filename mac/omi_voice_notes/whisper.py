# ABOUTME: Local transcription with whisper.cpp's whisper-cli and a Whisper model on disk: where the models and vocabulary live,
# ABOUTME: the shared error types, WAV duration, and the one-at-a-time whisper-cli runner. Silero VAD trims silence so Whisper stops inventing "Thank you."
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

MODELS = Path(
    os.environ.get(
        "OMI_NOTES_MODELS", Path.home() / "Library" / "Application Support" / "omi-voice-notes"
    )
)
MODEL = Path(os.environ.get("OMI_NOTES_WHISPER_MODEL", MODELS / "ggml-medium.bin"))
VAD_MODEL = Path(os.environ.get("OMI_NOTES_VAD_MODEL", MODELS / "ggml-silero-v5.1.2.bin"))
# One name or phrase per line; they go to whisper as the initial prompt so it spells them that way.
VOCABULARY = Path(
    os.environ.get(
        "OMI_NOTES_VOCABULARY", Path.home() / ".config" / "omi-voice-notes" / "vocabulary.txt"
    )
)
WHISPER_CLI = (
    os.environ.get("OMI_NOTES_WHISPER_CLI")
    or shutil.which("whisper-cli")
    or "/opt/homebrew/bin/whisper-cli"
)

Runner = Callable[..., subprocess.CompletedProcess]


class TranscriptionError(RuntimeError):
    """The backend ran but produced no transcript (bad exit, unreadable output)."""


class TranscriptionTimeout(TranscriptionError):
    """The backend produced nothing within the clip's time budget."""


def wav_duration_ms(audio: Path) -> int | None:
    try:
        with wave.open(str(audio), "rb") as clip:
            return round(clip.getnframes() * 1000 / clip.getframerate())
    except (wave.Error, EOFError, OSError):
        return None


def read_vocabulary(path: Path = VOCABULARY) -> str:
    """The prompt built from the vocabulary file, or "" when there is none. Read on every call so an
    edit needs no restart."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    words = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return ", ".join(words)


class WhisperCli:
    """One transcription at a time: each run loads the model (1.5 GB for Medium) and frees it after."""

    def __init__(
        self,
        binary: str = WHISPER_CLI,
        model: Path = MODEL,
        vad_model: Path = VAD_MODEL,
        vocabulary: Path = VOCABULARY,
        language: str = "en",
        runner: Runner = subprocess.run,
    ) -> None:
        self._binary = binary
        self._model = model
        self._vad_model = vad_model
        self._vocabulary = vocabulary
        self._language = language
        self._run = runner
        self._lock = threading.Lock()

    @property
    def ready(self) -> str | None:
        """None when whisper-cli and the model are in place, else what is missing."""
        if not Path(self._binary).exists():
            return f"no whisper-cli at {self._binary} (brew install whisper-cpp)"
        if not self._model.exists():
            return f"no model at {self._model} (run setup.sh)"
        return None

    def command(self, audio: Path, prefix: Path) -> list[str]:
        cmd = [
            self._binary,
            "-m", str(self._model),
            "-f", str(audio),
            "-l", self._language,
            "-np",  # nothing but the result on stdout
            "-sns",  # suppress non-speech tokens like (laughs)
            "-oj", "-of", str(prefix),
        ]  # fmt: skip
        if self._vad_model.exists():
            cmd += ["--vad", "-vm", str(self._vad_model)]
        prompt = read_vocabulary(self._vocabulary)
        if prompt:
            cmd += ["--prompt", prompt]
        return cmd

    def transcribe(self, audio: Path, timeout: float, expected_ms: int | None = None) -> dict:
        """`result` is the text, `segments` the timed pieces, `processingTime` the wall-clock ms including model load."""
        with self._lock, tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
            prefix = Path(tmp) / "out"
            started = time.monotonic()
            try:
                proc = self._run(
                    self.command(audio, prefix), capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                raise TranscriptionTimeout(
                    f"whisper-cli wrote no result within {timeout:.0f}s"
                ) from None
            except OSError as exc:
                raise TranscriptionError(f"could not run whisper-cli: {exc}") from None
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if proc.returncode != 0:
                detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
                raise TranscriptionError(f"whisper-cli exited {proc.returncode}: {detail[0]}")
            try:
                output = json.loads(prefix.with_suffix(".json").read_text())
                segments = [
                    {
                        "text": piece["text"].strip(),
                        "start": piece["offsets"]["from"] / 1000,
                        "end": piece["offsets"]["to"] / 1000,
                    }
                    for piece in output["transcription"]
                ]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise TranscriptionError(f"whisper-cli wrote no readable JSON: {exc}") from None
        text = " ".join(piece["text"] for piece in segments if piece["text"])
        return {
            "result": text,
            "segments": segments,
            "processingTime": elapsed_ms,
            "duration": expected_ms,
            "engine": "whisper-cli",
            "modelName": self._model.name,
        }
