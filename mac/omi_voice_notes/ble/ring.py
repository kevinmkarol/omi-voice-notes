# ABOUTME: Pure codec for the CV1 storage ring protocol (docs/PROTOCOL.md §3): commands out, notifications in, 444-byte records apart.
# ABOUTME: Mirrors the app's ring_protocol.dart so a Mac sync reads exactly what the Omi app would; no I/O here.
from __future__ import annotations

import struct
from dataclasses import dataclass

RECORD_SIZE = 444
TIMESTAMP_BYTES = 4
AUDIO_PAYLOAD_BYTES = RECORD_SIZE - TIMESTAMP_BYTES
UPTIME_FLAG = 0x8000_0000  # bit 31 of a record timestamp: seconds since boot, not epoch

CMD_STOP = 0x03
CMD_INFO = 0x10
CMD_READ = 0x11
CMD_ADVANCE = 0x12
CMD_CLEAR = 0x13

NOTIFY_ACK = 0x01
NOTIFY_INFO = 0x02
NOTIFY_DATA = 0x03
NOTIFY_DONE = 0x04
NOTIFY_READ_BEGIN = 0x05
NOTIFY_LIVE_MARK = 0x06  # newer firmware; ignored

ACK_OK = 0
ACK_INVALID_COMMAND = 6
ACK_NOT_READY = 9
ACK_SEQ_OUT_OF_RANGE = 10
ACK_NAMES = {
    0: "ok",
    6: "invalid command",
    9: "storage not ready",
    10: "seq out of range",
    11: "ring id mismatch",
}


@dataclass(frozen=True)
class RingInfo:
    read_seq: int
    write_seq: int
    capacity_packets: int
    dropped_packets: int
    record_size: int

    @property
    def unread(self) -> int:
        return max(0, self.write_seq - self.read_seq)

    @property
    def unread_seconds(self) -> float:
        """Rough duration: five 20 ms frames per record."""
        return self.unread * 0.1


@dataclass(frozen=True)
class RingStatus:
    used_bytes: int
    unread_packets: int
    free_bytes: int
    rtc_valid: bool


@dataclass(frozen=True)
class Ack:
    status: int

    @property
    def ok(self) -> bool:
        return self.status == ACK_OK

    def __str__(self) -> str:
        return ACK_NAMES.get(self.status, f"status {self.status}")


@dataclass(frozen=True)
class ReadBegin:
    start_seq: int
    count: int


@dataclass(frozen=True)
class Done:
    status: int
    next_seq: int

    @property
    def ok(self) -> bool:
        return self.status == ACK_OK


@dataclass(frozen=True)
class Data:
    payload: bytes


Notification = Ack | RingInfo | ReadBegin | Done | Data | None


def encode_info() -> bytes:
    return bytes([CMD_INFO])


def encode_read(start_seq: int, count: int = 0) -> bytes:
    if count:
        return struct.pack(">BQI", CMD_READ, start_seq, count)
    return struct.pack(">BQ", CMD_READ, start_seq)


def encode_advance(new_read_seq: int) -> bytes:
    return struct.pack(">BQ", CMD_ADVANCE, new_read_seq)


def encode_clear() -> bytes:
    return bytes([CMD_CLEAR])


def encode_stop() -> bytes:
    return bytes([CMD_STOP])


def parse_status(value: bytes) -> RingStatus | None:
    """The 16-byte read on STORAGE_STATUS; little-endian, unlike everything else."""
    if len(value) < 16:
        return None
    used, unread, free, rtc = struct.unpack_from("<IIII", value)
    return RingStatus(used, unread, free, bool(rtc))


def parse_notification(value: bytes) -> Notification:
    """Decode one notification from STORAGE_CONTROL; None for empty/unknown opcodes."""
    if not value:
        return None
    op = value[0]
    if op == NOTIFY_ACK and len(value) >= 2:
        return Ack(value[1])
    if op == NOTIFY_INFO and len(value) >= 31:
        read_seq, write_seq, capacity, dropped, size = struct.unpack_from(">QQIQH", value, 1)
        return RingInfo(read_seq, write_seq, capacity, dropped, size)
    if op == NOTIFY_READ_BEGIN and len(value) >= 13:
        start, count = struct.unpack_from(">QI", value, 1)
        return ReadBegin(start, count)
    if op == NOTIFY_DONE and len(value) >= 10:
        status, next_seq = struct.unpack_from(">BQ", value, 1)
        return Done(status, next_seq)
    if op == NOTIFY_DATA:
        return Data(bytes(value[1:]))
    return None


def record_timestamp(record: bytes) -> int | None:
    """Epoch seconds of a record's first frame, or None when the firmware only had uptime."""
    raw = struct.unpack_from(">I", record)[0]
    if raw & UPTIME_FLAG:
        return None
    return raw


def record_frames(record: bytes) -> list[bytes]:
    """The Opus frames packed in a record: [len][frame]... with zero padding; a frame that would
    run past the end is a boundary artefact and is not returned (the firmware's `>=` rule)."""
    audio = record[TIMESTAMP_BYTES:RECORD_SIZE]
    frames: list[bytes] = []
    offset = 0
    end = len(audio)
    while offset < end - 1:
        size = audio[offset]
        if size == 0:
            offset += 1
            continue
        if offset + 1 + size >= end:
            break
        frames.append(bytes(audio[offset + 1 : offset + 1 + size]))
        offset += 1 + size
    return frames


class RecordReassembler:
    """DATA notifications are not aligned to records: feed every payload in, pull whole records out."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def append(self, payload: bytes) -> list[bytes]:
        self._buffer.extend(payload)
        out: list[bytes] = []
        while len(self._buffer) >= RECORD_SIZE:
            out.append(bytes(self._buffer[:RECORD_SIZE]))
            del self._buffer[:RECORD_SIZE]
        return out

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self._buffer.clear()
