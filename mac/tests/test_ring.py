# ABOUTME: The ring protocol codec against the byte layouts in docs/PROTOCOL.md A.3 and the app's ring_protocol.dart.
import struct

from omi_voice_notes.ble import ring
from omi_voice_notes.testing import pack_records


def test_commands_match_firmware_lengths():
    assert ring.encode_info() == b"\x10"
    assert ring.encode_read(7) == b"\x11" + (7).to_bytes(8, "big")
    assert len(ring.encode_read(7)) == 9
    assert ring.encode_read(7, 36) == b"\x11" + (7).to_bytes(8, "big") + (36).to_bytes(4, "big")
    assert len(ring.encode_read(7, 36)) == 13
    assert ring.encode_advance(2**40) == b"\x12" + (2**40).to_bytes(8, "big")
    assert ring.encode_clear() == b"\x13"
    assert ring.encode_stop() == b"\x03"


def test_parse_info_31_bytes_and_with_newer_extras():
    raw = struct.pack(">BQQIQH", 0x02, 10, 25, 1_000_000, 3, 444)
    info = ring.parse_notification(raw)
    assert info == ring.RingInfo(10, 25, 1_000_000, 3, 444)
    assert info.unread == 15
    assert abs(info.unread_seconds - 1.5) < 1e-9
    # firmware with capability bytes appended still parses
    assert ring.parse_notification(raw + b"\x0f\x01" + (99).to_bytes(8, "big")) == info


def test_parse_control_notifications():
    assert ring.parse_notification(b"\x01\x00") == ring.Ack(0)
    assert ring.parse_notification(b"\x01\x0a") == ring.Ack(10)
    assert str(ring.Ack(9)) == "storage not ready"
    assert ring.parse_notification(struct.pack(">BQI", 0x05, 10, 15)) == ring.ReadBegin(10, 15)
    assert ring.parse_notification(struct.pack(">BBQ", 0x04, 0, 25)) == ring.Done(0, 25)
    assert ring.parse_notification(b"\x03abc") == ring.Data(b"abc")
    assert ring.parse_notification(b"") is None
    assert (
        ring.parse_notification(b"\x06" + bytes(18)) is None
    )  # live mark from newer firmware: ignored
    assert ring.parse_notification(b"\x02\x00") is None  # truncated info


def test_status_read_is_little_endian():
    status = ring.parse_status(struct.pack("<IIII", 4440, 10, 500_000_000, 1))
    assert status == ring.RingStatus(4440, 10, 500_000_000, True)
    assert ring.parse_status(b"\x00" * 15) is None


def test_record_timestamp_and_uptime_flag():
    rec = struct.pack(">I", 1_762_000_000) + bytes(440)
    assert ring.record_timestamp(rec) == 1_762_000_000
    rec = struct.pack(">I", ring.UPTIME_FLAG | 12345) + bytes(440)
    assert ring.record_timestamp(rec) is None


def test_record_frames_padding_and_boundary_rule():
    payload = bytearray(440)
    payload[0] = 2
    payload[1:3] = b"\x07\x08"
    payload[3] = 0  # padding
    payload[4] = 3
    payload[5:8] = b"\x01\x02\x03"
    rec = bytes(4) + bytes(payload)
    assert ring.record_frames(rec) == [b"\x07\x08", b"\x01\x02\x03"]
    # a length byte whose frame would end exactly at the boundary is a stale trailer: not a frame
    payload = bytearray(440)
    payload[0] = 2
    payload[1:3] = b"\xaa\xbb"
    payload[300] = 140  # 300 + 1 + 140 = 441 >= 440: stale trailer, not a frame
    payload[301:440] = bytes(range(139))
    rec = bytes(4) + bytes(payload)
    assert ring.record_frames(rec) == [b"\xaa\xbb"]
    payload[300] = 138  # 300 + 1 + 138 = 439 < 440: a real frame
    assert ring.record_frames(bytes(4) + bytes(payload)) == [b"\xaa\xbb", bytes(range(138))]


def test_reassembler_cuts_unaligned_chunks_into_records():
    data = bytes(range(256)) * 6  # 1536 bytes = 3 records + 204 left over
    r = ring.RecordReassembler()
    out = []
    for i in range(0, len(data), 500):
        out.extend(r.append(data[i : i + 500]))
    assert len(out) == 3
    assert b"".join(out) == data[: 3 * 444]
    assert r.pending == 1536 - 3 * 444


def test_pack_records_roundtrip_through_parser():
    frames = [bytes([i % 251]) * (40 + i % 60) for i in range(50)]
    records = pack_records(frames, 1_700_000_000)
    assert all(len(r) == 444 for r in records)
    back = [f for r in records for f in ring.record_frames(r)]
    assert back == frames
    assert ring.record_timestamp(records[0]) == 1_700_000_000
    assert ring.record_timestamp(pack_records(frames, None, uptime=77)[0]) is None
