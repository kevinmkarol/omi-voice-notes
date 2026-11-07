# ABOUTME: Tiny WAV helpers for 16-bit PCM: write, read, and an append-friendly writer for recordings that grow while open.
# ABOUTME: whisper-cli wants 16 kHz mono WAV, so everything in the pipeline ends up in this shape.
from __future__ import annotations

import struct
import wave
from pathlib import Path


def write_wav(path: Path, pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)


def read_wav(path: Path) -> tuple[bytes, int, int]:
    with wave.open(str(path), "rb") as clip:
        if clip.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit PCM is supported")
        return clip.readframes(clip.getnframes()), clip.getframerate(), clip.getnchannels()


def duration_s(pcm_bytes: int, sample_rate: int = 16000, channels: int = 1) -> float:
    return pcm_bytes / (2 * channels * sample_rate)


class GrowingWav:
    """A WAV file whose header is patched on every flush, so a crash mid-recording still leaves a playable file."""

    def __init__(self, path: Path, sample_rate: int = 16000, channels: int = 1) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.sample_rate, self.channels = path, sample_rate, channels
        self._f = open(path, "wb")  # noqa: SIM115 - stays open while the recording grows
        self._data = 0
        self._write_header()

    def _write_header(self) -> None:
        byte_rate = self.sample_rate * self.channels * 2
        header = b"RIFF" + struct.pack("<I", 36 + self._data) + b"WAVE"
        header += b"fmt " + struct.pack(
            "<IHHIIHH", 16, 1, self.channels, self.sample_rate, byte_rate, self.channels * 2, 16
        )
        header += b"data" + struct.pack("<I", self._data)
        self._f.seek(0)
        self._f.write(header)
        self._f.seek(44 + self._data)

    def write(self, pcm: bytes) -> None:
        self._f.write(pcm)
        self._data += len(pcm)

    @property
    def seconds(self) -> float:
        return duration_s(self._data, self.sample_rate, self.channels)

    def flush(self) -> None:
        self._write_header()
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self.flush()
            self._f.close()
