# ABOUTME: Ring records → timestamped recordings: decoding, splitting on time gaps and on unset clocks, saving WAV + sidecar.
import json
import wave

from omi_voice_notes.ble.uuids import FRAME_SAMPLES
from omi_voice_notes.pipeline.recorder import RingRecorder, save_recording
from omi_voice_notes.testing import encode_frames, pack_records, tone


def test_records_become_one_recording_with_the_right_length_and_start(tmp_path):
    frames = encode_frames(tone(3.0))  # 150 frames
    records = pack_records(frames, 1_762_000_000)
    rec = RingRecorder()
    seq = 500
    for r in records:
        rec.feed(r, seq)
        seq += 1
    out = rec.recordings(final=True)
    assert len(out) == 1
    only = out[0]
    assert only.frames == 150
    assert only.records == len(records)
    assert only.first_seq == 500 and only.last_seq == 500 + len(records) - 1
    assert abs(only.seconds - 3.0) < 0.001
    assert only.start_epoch == 1_762_000_000
    path = save_recording(only, tmp_path)
    assert path.name.startswith("omi-") and path.suffix == ".wav"
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 150 * FRAME_SAMPLES
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["clock"] == "epoch" and meta["frames"] == 150 and meta["first_seq"] == 500


def test_time_gap_starts_a_new_recording():
    a = pack_records(encode_frames(tone(2.0)), 1_762_000_000)
    b = pack_records(encode_frames(tone(1.0)), 1_762_000_000 + 600)  # ten minutes later
    rec = RingRecorder(gap_s=90)
    for seq, r in enumerate(a + b):
        rec.feed(r, seq)
    out = rec.recordings(final=True)
    assert [round(o.seconds) for o in out] == [2, 1]
    assert out[1].start_epoch == 1_762_000_000 + 600


def test_uptime_stamped_records_are_kept_apart_and_named_by_seq():
    epoch = pack_records(encode_frames(tone(1.0)), 1_762_000_000)
    uptime = pack_records(encode_frames(tone(1.0)), None, uptime=300)
    rec = RingRecorder()
    seq = 10
    for r in epoch + uptime:
        rec.feed(r, seq)
        seq += 1
    out = rec.recordings(final=True)
    assert len(out) == 2
    assert out[0].start_epoch == 1_762_000_000
    assert out[1].start_epoch is None and out[1].uptime_start == 300
    assert out[1].name().startswith("omi-uptime-300-seq")


def test_seq_jump_splits_even_without_time_jump():
    frames = encode_frames(tone(2.0))
    records = pack_records(frames, 1_762_000_000)
    rec = RingRecorder()
    half = len(records) // 2
    for i, r in enumerate(records[:half]):
        rec.feed(r, i)
    for i, r in enumerate(records[half:]):
        rec.feed(r, 1000 + i)
    assert len(rec.recordings(final=True)) == 2


def test_short_records_are_skipped():
    rec = RingRecorder()
    rec.feed(b"\x00" * 10, 1)
    assert rec.recordings(final=True) == []
