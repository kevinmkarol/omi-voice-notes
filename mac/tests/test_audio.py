# ABOUTME: Live audio packet reassembly (docs/PROTOCOL.md A.2), button decoding and the time-sync encoding.
import struct

from omi_voice_notes.ble import audio
from omi_voice_notes.testing import live_packets


def test_single_fragment_frames_emit_on_next_frame_or_flush():
    r = audio.FrameReassembler()
    packets = live_packets([b"A" * 50, b"B" * 60, b"C" * 70])
    out = []
    for i, p in enumerate(packets):
        out.extend(r.feed(p, now=i * 0.02))
    assert [f.data for f in out] == [b"A" * 50, b"B" * 60]
    assert r.flush(now=0.04 + 0.01) == []  # too soon
    flushed = r.flush(now=0.04 + 0.06)
    assert [f.data for f in flushed] == [b"C" * 70]
    assert r.flush(now=1.0) == []
    assert all(not f.gap for f in out + flushed)


def test_multi_fragment_frame_is_joined_in_index_order():
    r = audio.FrameReassembler()
    frame = bytes(range(200)) * 3  # 600 bytes, longer than one notification
    packets = live_packets([frame, b"next"], mtu_payload=244)
    assert len(packets) == 4
    out = []
    for p in packets:
        out.extend(r.feed(p, 0.0))
    assert [f.data for f in out] == [frame]
    assert r.flush(0.0, force=True)[0].data == b"next"


def test_packet_id_gap_is_flagged_and_counted_with_wrap():
    r = audio.FrameReassembler()
    r.feed(struct.pack("<HB", 65534, 0) + b"x", 0.0)
    r.feed(struct.pack("<HB", 65535, 0) + b"y", 0.0)
    out = r.feed(struct.pack("<HB", 2, 0) + b"z", 0.0)  # ids 0 and 1 lost across the wrap
    assert out[0].data == b"y" and not out[0].gap
    assert r.lost == 2
    assert r.flush(0.0, force=True)[0].gap is True


def test_short_packets_are_ignored():
    r = audio.FrameReassembler()
    assert r.feed(b"\x00\x01", 0.0) == []


def test_button_and_time_sync_encodings():
    assert audio.parse_button(struct.pack("<ii", 2, 0)) == 2
    assert audio.parse_button(b"\x01") is None
    assert audio.encode_time_sync(1_762_000_000) == struct.pack("<I", 1_762_000_000)
    assert audio.parse_time_sync(audio.encode_time_sync(123)) == 123
