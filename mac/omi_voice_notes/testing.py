# ABOUTME: Synthetic audio and pendant-shaped data for tests: tones, Opus frames, packed 444-byte ring records, live packets.
# ABOUTME: Keeps the firmware's packing rules in one place so a test failure here means the real parser and the synthetic data disagree.
from __future__ import annotations

import math
import struct

from omi_voice_notes import opus
from omi_voice_notes.ble import ring
from omi_voice_notes.ble.uuids import FRAME_SAMPLES, SAMPLE_RATE

FRAME_BYTES = FRAME_SAMPLES * 2


def tone(
    seconds: float, freq: float = 440.0, amplitude: float = 0.3, rate: int = SAMPLE_RATE
) -> bytes:
    n = int(seconds * rate)
    peak = int(amplitude * 32767)
    return struct.pack(
        f"<{n}h", *(int(peak * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
    )


def silence(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    return bytes(int(seconds * rate) * 2)


def encode_frames(pcm: bytes) -> list[bytes]:
    enc = opus.Encoder()
    try:
        return [
            enc.encode(pcm[i : i + FRAME_BYTES])
            for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        ]
    finally:
        enc.close()


def pack_records(frames: list[bytes], start_epoch: int | None, uptime: int = 0) -> list[bytes]:
    """Firmware packing: [ts u32 BE][len][frame]... zero padded to 444 bytes; a frame that would end at or
    past the boundary moves whole to the next record (its length byte is NOT written, unlike the firmware's
    stale trailing byte, which the parser tolerates either way). `start_epoch=None` writes uptime stamps."""
    records: list[bytes] = []
    payload = bytearray()
    first_in_record = 0

    def stamp(frame_index: int) -> int:
        seconds = (frame_index * FRAME_SAMPLES) // SAMPLE_RATE
        if start_epoch is None:
            return ring.UPTIME_FLAG | ((uptime + seconds) & 0x7FFF_FFFF)
        return start_epoch + seconds

    for frames_done, frame in enumerate(frames):
        if len(payload) + 1 + len(frame) >= ring.AUDIO_PAYLOAD_BYTES:
            records.append(
                struct.pack(">I", stamp(first_in_record))
                + bytes(payload).ljust(ring.AUDIO_PAYLOAD_BYTES, b"\0")
            )
            payload = bytearray()
            first_in_record = frames_done
        payload.append(len(frame))
        payload.extend(frame)
    if payload:
        records.append(
            struct.pack(">I", stamp(first_in_record))
            + bytes(payload).ljust(ring.AUDIO_PAYLOAD_BYTES, b"\0")
        )
    return records


def live_packets(frames: list[bytes], first_id: int = 0, mtu_payload: int = 495) -> list[bytes]:
    """Frames as the pendant notifies them: [id u16 LE][index u8][payload], fragmented when a frame exceeds mtu_payload."""
    out: list[bytes] = []
    pid = first_id
    for frame in frames:
        for index, off in enumerate(range(0, len(frame), mtu_payload)):
            out.append(struct.pack("<HB", pid & 0xFFFF, index) + frame[off : off + mtu_payload])
            pid += 1
    return out


class FakeTranscriber:
    """Returns a fixed text for every clip and remembers what it was asked to transcribe."""

    name = "fake"

    def __init__(self, text: str = "hello from the fake transcriber") -> None:
        self.text = text
        self.calls: list[float] = []

    def transcribe(self, audio, timeout: float) -> dict:
        from omi_voice_notes.whisper import wav_duration_ms

        self.calls.append((wav_duration_ms(audio) or 0) / 1000)
        return {
            "result": self.text,
            "segments": [{"text": self.text, "start": 0.0, "end": self.calls[-1]}],
            "processingTime": 1,
            "engine": "fake",
        }

    def close(self) -> None:
        pass
