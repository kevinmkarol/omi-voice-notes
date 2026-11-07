# ABOUTME: Declicker and prompt-echo checks: synthetic SD-write thumps vanish, speech-like bursts and tones survive, prompt recitals are flagged.
import struct

from omi_voice_notes.pipeline.filters import declick, find_clicks, is_prompt_echo
from omi_voice_notes.testing import silence, tone

RATE = 16000
PROMPT = "Zephyr, MCUboot, Silero, Opus, Bleak, Whisper"


def _thump(ms: float = 6.0, level: int = 32000) -> bytes:
    n = int(RATE * ms / 1000)
    return struct.pack(f"<{n}h", *([level] * (n // 2) + [-level] * (n - n // 2)))


def _peak(pcm: bytes) -> int:
    return max(abs(v) for v in struct.unpack(f"<{len(pcm) // 2}h", pcm))


def test_thumps_in_quiet_room_noise_are_found_and_flattened():
    room = tone(3.0, 180.0, amplitude=0.004)  # -48 dBFS hum, like the pendant's idle noise floor
    pcm = bytearray(room)
    for at_s in (0.5, 1.7, 2.4):
        off = int(at_s * RATE) * 2
        t = _thump()
        pcm[off : off + len(t)] = t
    clicks = find_clicks(bytes(pcm))
    assert len(clicks) == 3
    cleaned, n = declick(bytes(pcm))
    assert n == 3 and len(cleaned) == len(pcm)
    assert _peak(cleaned) < 600


def test_a_thump_at_the_very_start_is_handled():
    pcm = _thump() + silence(0.5)
    cleaned, n = declick(pcm)
    assert n == 1 and _peak(cleaned) == 0


def test_speech_like_bursts_and_steady_tones_survive():
    # a 120 ms loud burst is longer than any click: left alone
    burst = silence(0.5) + tone(0.12, 300.0, amplitude=0.9) + silence(0.5)
    assert find_clicks(burst) == []
    # a short loud onset right before a vowel-length tone is a plosive, not a click
    plosive = silence(0.5) + _thump(8.0) + tone(0.3, 220.0, amplitude=0.4) + silence(0.5)
    assert find_clicks(plosive) == []
    steady = tone(2.0, 440.0, amplitude=0.8)
    cleaned, n = declick(steady)
    assert n == 0 and cleaned == steady


def test_prompt_echo_detection():
    assert is_prompt_echo("Zephyr, MCUboot, Silero, Opus, Bleak, Whisper", PROMPT)
    assert is_prompt_echo("silero opus", PROMPT)
    assert not is_prompt_echo("Remind me to read up on Zephyr", PROMPT)
    assert not is_prompt_echo("I need to remember to finish up voting tonight.", PROMPT)
    assert not is_prompt_echo("", PROMPT)
    assert not is_prompt_echo("Zephyr", "")
