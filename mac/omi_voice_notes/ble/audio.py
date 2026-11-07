# ABOUTME: Reassembles live audio notifications (docs/PROTOCOL.md §2) into whole codec frames and decodes the button notification.
# ABOUTME: Pure functions over bytes plus a tiny state machine; the 50 ms idle flush is driven by whoever owns the clock.
from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = 3  # packet id u16 LE + fragment index u8
IDLE_FLUSH_S = 0.05


@dataclass(frozen=True)
class Frame:
    data: bytes
    packet_id: int  # id of the notification that started the frame
    gap: bool  # notifications were lost right before this frame
    at: float  # arrival clock of the first fragment (caller's clock)


class FrameReassembler:
    """index 0 completes the pending frame and starts a new one; index>0 appends. `flush()` returns
    the pending frame when nothing arrived for IDLE_FLUSH_S, so one-fragment frames (the CV1 norm)
    never wait for the next frame when the stream pauses."""

    def __init__(self) -> None:
        self._pending: bytearray | None = None
        self._pending_id = 0
        self._pending_gap = False
        self._pending_at = 0.0
        self._last_id: int | None = None
        self.lost = 0

    def feed(self, packet: bytes, now: float) -> list[Frame]:
        if len(packet) < HEADER:
            return []
        packet_id, index = struct.unpack_from("<HB", packet)
        payload = packet[HEADER:]
        gap = False
        if self._last_id is not None:
            expected = (self._last_id + 1) & 0xFFFF
            if packet_id != expected:
                gap = True
                self.lost += (packet_id - expected) & 0xFFFF
        self._last_id = packet_id

        out: list[Frame] = []
        if index == 0 or self._pending is None:
            if self._pending is not None:
                out.append(self._take())
            self._pending = bytearray(payload)
            self._pending_id, self._pending_gap, self._pending_at = packet_id, gap, now
        else:
            self._pending.extend(payload)
            self._pending_gap = self._pending_gap or gap
        return out

    def flush(self, now: float, force: bool = False) -> list[Frame]:
        if self._pending is None:
            return []
        if force or now - self._pending_at >= IDLE_FLUSH_S:
            return [self._take()]
        return []

    def _take(self) -> Frame:
        assert self._pending is not None
        frame = Frame(bytes(self._pending), self._pending_id, self._pending_gap, self._pending_at)
        self._pending = None
        return frame


def parse_button(value: bytes) -> int | None:
    """The 8-byte button notification: two int32 LE, the first is the event (1..5)."""
    if len(value) < 4:
        return None
    return struct.unpack_from("<i", value)[0]


def encode_time_sync(epoch_s: int) -> bytes:
    return struct.pack("<I", epoch_s & 0xFFFFFFFF)


def parse_time_sync(value: bytes) -> int | None:
    if len(value) < 4:
        return None
    return struct.unpack_from("<I", value)[0]
