# ABOUTME: Cleanups between the Opus decoder and whisper: a declicker for the pendant's SD-write thumps in ring audio,
# ABOUTME: and a check for whisper echoing its vocabulary prompt back on non-speech clips (silence, haptics, clicks).
from __future__ import annotations

import array
import re
import sys

# Measured on CV1 firmware 3.0.21 (2026-11-02): ring recordings carry clipped, low-frequency thumps 6-15 ms wide
# at a fixed offset inside an Opus frame, only while the pendant writes to its SD card (which it does only when
# nothing is connected). Live-streamed audio has none. They trip the VAD and make whisper hallucinate.
CLICK_WINDOW = 160  # 10 ms analysis windows at 16 kHz
CLICK_MIN_PEAK = 6000  # a window must reach this (about -15 dBFS) to be a click candidate
CLICK_MAX_WINDOWS = 3  # a click is at most 30 ms; longer loud stretches are speech or real noise
CLICK_ISOLATION = (
    6  # the click must stand this far (about 16 dB) above the windows 20-50 ms either side
)
CLICK_FLOOR = 200  # treats digital silence around a click as this peak so the ratio stays finite
CLICK_MARGIN = 32  # 2 ms repaired on each side of the loud windows

_WORD = re.compile(r"[^\W_]+")


def _samples(pcm: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _window_peaks(samples: array.array, window: int) -> list[int]:
    peaks = []
    for i in range(0, len(samples), window):
        chunk = samples[i : i + window]
        peaks.append(max(max(chunk), -min(chunk)))
    return peaks


def find_clicks(pcm: bytes, window: int = CLICK_WINDOW) -> list[tuple[int, int]]:
    """Sample ranges [start, end) of isolated short transients: loud for <= CLICK_MAX_WINDOWS windows and far
    above the audio 2-5 windows before and after."""
    samples = _samples(pcm)
    if not len(samples):
        return []
    peaks = _window_peaks(samples, window)
    clicks: list[tuple[int, int]] = []
    i = 0
    while i < len(peaks):
        if peaks[i] < CLICK_MIN_PEAK:
            i += 1
            continue
        j = i
        while j + 1 < len(peaks) and peaks[j + 1] >= CLICK_MIN_PEAK:
            j += 1
        run = j - i + 1
        if run <= CLICK_MAX_WINDOWS:
            before = peaks[max(0, i - 5) : max(0, i - 1)]
            after = peaks[j + 2 : j + 6]
            context = max([CLICK_FLOOR, *before, *after])
            if max(peaks[i : j + 1]) >= CLICK_ISOLATION * context:
                start = max(0, i * window - CLICK_MARGIN)
                end = min(len(samples), (j + 1) * window + CLICK_MARGIN)
                clicks.append((start, end))
        i = j + 1
    return clicks


def declick(pcm: bytes) -> tuple[bytes, int]:
    """Returns the audio with each detected click replaced by a straight line between its edges, and how many
    were removed. Speech is left alone: plosives sit next to vowels, so they never look isolated."""
    clicks = find_clicks(pcm)
    if not clicks:
        return pcm, 0
    samples = _samples(pcm)
    for start, end in clicks:
        left = samples[start - 1] if start > 0 else 0
        right = samples[end] if end < len(samples) else 0
        n = end - start
        for k in range(n):
            samples[start + k] = int(left + (right - left) * (k + 1) / (n + 1))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes(), len(clicks)


def is_prompt_echo(text: str, prompt: str | None = None) -> bool:
    """True when every word of `text` comes from the whisper prompt (the vocabulary file): that is whisper
    reciting its prompt over audio with no speech in it, not something that was said."""
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return False
    if prompt is None:
        from omi_voice_notes.whisper import read_vocabulary

        prompt = read_vocabulary()
    allowed = {w.lower() for w in _WORD.findall(prompt or "")}
    return bool(allowed) and all(w in allowed for w in words)
