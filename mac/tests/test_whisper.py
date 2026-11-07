# ABOUTME: The whisper-cli runner without whisper-cli: the command it builds (VAD and vocabulary prompt only when present),
# ABOUTME: how it reads the JSON whisper-cli writes, and the errors for a bad exit, a timeout and unreadable output.
import json
import subprocess
import wave
from pathlib import Path

import pytest

from omi_voice_notes.whisper import (
    TranscriptionError,
    TranscriptionTimeout,
    WhisperCli,
    read_vocabulary,
    wav_duration_ms,
)


def _wav(path: Path, seconds: float = 1.5) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(int(seconds * 16000) * 2))
    return path


def test_wav_duration_and_missing_file(tmp_path):
    assert wav_duration_ms(_wav(tmp_path / "a.wav", 1.5)) == 1500
    assert wav_duration_ms(tmp_path / "missing.wav") is None


def test_read_vocabulary_skips_blanks_and_comments(tmp_path):
    vocab = tmp_path / "vocabulary.txt"
    vocab.write_text("# names\nZephyr\n\n  MCUboot  \n")
    assert read_vocabulary(vocab) == "Zephyr, MCUboot"
    assert read_vocabulary(tmp_path / "none.txt") == ""


def test_command_adds_vad_and_prompt_only_when_present(tmp_path):
    binary = tmp_path / "whisper-cli"
    binary.touch()
    model = tmp_path / "ggml-medium.bin"
    model.write_bytes(b"x")
    cli = WhisperCli(str(binary), model, tmp_path / "no-vad.bin", tmp_path / "no-vocab.txt")
    cmd = cli.command(Path("clip.wav"), tmp_path / "out")
    assert cmd[:3] == [str(binary), "-m", str(model)]
    assert "-np" in cmd and "-sns" in cmd and "-oj" in cmd
    assert "--vad" not in cmd and "--prompt" not in cmd

    vad = tmp_path / "vad.bin"
    vad.write_bytes(b"x")
    vocab = tmp_path / "vocabulary.txt"
    vocab.write_text("Zephyr\n")
    cmd = WhisperCli(str(binary), model, vad, vocab).command(Path("clip.wav"), tmp_path / "out")
    assert cmd[cmd.index("-vm") + 1] == str(vad)
    assert cmd[cmd.index("--prompt") + 1] == "Zephyr"


def test_ready_names_what_is_missing(tmp_path):
    assert "whisper-cli" in WhisperCli(str(tmp_path / "nope"), tmp_path / "m.bin").ready
    binary = tmp_path / "whisper-cli"
    binary.touch()
    assert "setup.sh" in WhisperCli(str(binary), tmp_path / "m.bin").ready
    (tmp_path / "m.bin").write_bytes(b"x")
    assert WhisperCli(str(binary), tmp_path / "m.bin").ready is None


def _fake_runner(output: dict | None, returncode: int = 0, stderr: str = ""):
    def run(cmd, capture_output, text, timeout):
        if output is not None:
            prefix = Path(cmd[cmd.index("-of") + 1])
            prefix.with_suffix(".json").write_text(json.dumps(output))
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    return run


def test_transcribe_joins_segments_from_the_json(tmp_path):
    output = {
        "transcription": [
            {"text": " Hello there.", "offsets": {"from": 0, "to": 1200}},
            {"text": " General.", "offsets": {"from": 1200, "to": 2000}},
        ]
    }
    cli = WhisperCli("whisper-cli", tmp_path / "m.bin", runner=_fake_runner(output))
    meta = cli.transcribe(Path("clip.wav"), timeout=10, expected_ms=2000)
    assert meta["result"] == "Hello there. General."
    assert meta["segments"][1] == {"text": "General.", "start": 1.2, "end": 2.0}
    assert meta["engine"] == "whisper-cli" and meta["duration"] == 2000


def test_transcribe_errors(tmp_path):
    with pytest.raises(TranscriptionError, match="exited 1: boom"):
        WhisperCli("x", tmp_path / "m.bin", runner=_fake_runner(None, 1, "warn\nboom")).transcribe(
            Path("c.wav"), 10
        )
    with pytest.raises(TranscriptionError, match="no readable JSON"):
        WhisperCli("x", tmp_path / "m.bin", runner=_fake_runner(None)).transcribe(Path("c.wav"), 10)

    def slow(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(TranscriptionTimeout):
        WhisperCli("x", tmp_path / "m.bin", runner=slow).transcribe(Path("c.wav"), 10)
