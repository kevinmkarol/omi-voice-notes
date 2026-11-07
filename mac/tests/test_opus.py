# ABOUTME: libopus through ctypes: a tone survives encode → decode with the pendant's parameters, and loss concealment yields a full frame.
import array
import math

import pytest

from omi_voice_notes import opus
from omi_voice_notes.testing import FRAME_BYTES, encode_frames, tone

pytestmark = pytest.mark.skipif(not opus._CANDIDATES[1] or not __import__("pathlib").Path(opus._CANDIDATES[1]).exists(),
                                reason="libopus not installed")  # fmt: skip


def _corr(a: bytes, b: bytes) -> float:
    xa, xb = array.array("h", a), array.array("h", b)
    n = min(len(xa), len(xb))
    xa, xb = xa[:n], xb[:n]
    dot = sum(p * q for p, q in zip(xa, xb))
    na = math.sqrt(sum(p * p for p in xa)) or 1.0
    nb = math.sqrt(sum(q * q for q in xb)) or 1.0
    return dot / (na * nb)


def test_version_and_roundtrip():
    assert opus.version().startswith("libopus")
    pcm = tone(1.0, 440.0, 0.4)
    frames = encode_frames(pcm)
    assert len(frames) == 50
    assert all(20 <= len(f) <= 160 for f in frames), (
        "frames should look like the pendant's (≤160 bytes at 32 kbps)"
    )
    dec = opus.Decoder()
    out = b"".join(dec.decode(f) for f in frames)
    assert len(out) == len(frames) * FRAME_BYTES
    # skip the first few frames (encoder/decoder warm-up and the low-delay lookahead)
    assert _corr(pcm[10 * FRAME_BYTES :], out[10 * FRAME_BYTES :]) > 0.8
    dec.close()


def test_packet_loss_concealment_returns_a_frame():
    dec = opus.Decoder()
    frames = encode_frames(tone(0.2))
    for f in frames[:5]:
        dec.decode(f)
    assert len(dec.decode(None)) == FRAME_BYTES


def test_bad_packet_raises():
    dec = opus.Decoder()
    with pytest.raises(opus.OpusError):
        dec.decode(b"\xff" * 3)
