# ABOUTME: Cuts a live 16 kHz PCM stream into speech segments on energy (RMS) with hangover, pre-roll and a max length, for near-realtime transcription.
# ABOUTME: Deliberately simple: whisper-server runs Silero VAD on each segment anyway, this only decides when to send one.
from __future__ import annotations

import array
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    pcm: bytes
    start_s: float  # position in the stream
    end_s: float
    reason: str  # "silence" | "max" | "flush"


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class SpeechSegmenter:
    """Feed 20 ms frames; get a Segment back when speech ended (silence for `hang_s`), when a segment
    hit `max_s`, or on flush(). Frames quieter than `threshold` (RMS, 0..1) count as silence, with an
    adaptive floor so a noisy room doesn't look like endless speech."""

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.012,
        hang_s: float = 0.9,
        pre_roll_s: float = 0.4,
        min_speech_s: float = 0.5,
        max_s: float = 25.0,
    ) -> None:
        self.rate = sample_rate
        self.threshold = threshold
        self.hang_s, self.pre_roll_s, self.min_speech_s, self.max_s = (
            hang_s,
            pre_roll_s,
            min_speech_s,
            max_s,
        )
        self._pos_s = 0.0
        self._pre: list[tuple[bytes, float]] = []
        self._pre_len_s = 0.0
        self._active: bytearray | None = None
        self._active_start = 0.0
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._floor = threshold / 2

    @property
    def position_s(self) -> float:
        return self._pos_s

    def feed(self, pcm: bytes) -> list[Segment]:
        out: list[Segment] = []
        frame_s = len(pcm) / (2 * self.rate)
        level = rms(pcm)
        # Track the quiet floor so the effective threshold sits above room noise: drop fast, rise slowly,
        # and only rise between segments, so a long stretch of speech is never re-labelled as noise.
        if level < self._floor:
            self._floor = 0.95 * self._floor + 0.05 * level
        elif self._active is None:
            self._floor = min(0.995 * self._floor + 0.005 * level, self.threshold * 4)
        speaking = level > max(self.threshold, self._floor * 3)
        start = self._pos_s
        self._pos_s += frame_s

        if self._active is None:
            self._pre.append((pcm, start))
            self._pre_len_s += frame_s
            while self._pre_len_s > self.pre_roll_s + 1e-6 and len(self._pre) > 1:
                dropped, _ = self._pre.pop(0)
                self._pre_len_s -= len(dropped) / (2 * self.rate)
            if speaking:
                self._active = bytearray(b"".join(p for p, _ in self._pre))
                self._active_start = self._pre[0][1]
                self._pre, self._pre_len_s = [], 0.0
                self._speech_s, self._silence_s = frame_s, 0.0
            return out

        self._active.extend(pcm)
        if speaking:
            self._speech_s += frame_s
            self._silence_s = 0.0
        else:
            self._silence_s += frame_s
        length = self._pos_s - self._active_start
        if self._silence_s >= self.hang_s:
            out.extend(self._close("silence"))
        elif length >= self.max_s:
            out.extend(self._close("max"))
        return out

    def _close(self, reason: str) -> list[Segment]:
        assert self._active is not None
        pcm = bytes(self._active)
        start, end = self._active_start, self._pos_s
        self._active = None
        if self._speech_s < self.min_speech_s:
            return []
        return [Segment(pcm, start, end, reason)]

    def flush(self) -> list[Segment]:
        if self._active is None:
            return []
        return self._close("flush")
