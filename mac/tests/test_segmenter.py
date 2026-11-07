# ABOUTME: The energy segmenter: silence yields nothing, a burst ends after the hangover with pre-roll, long speech is cut at max, flush closes.
from omi_voice_notes.pipeline.segmenter import SpeechSegmenter, rms
from omi_voice_notes.testing import FRAME_BYTES, silence, tone


def _feed(seg: SpeechSegmenter, pcm: bytes):
    out = []
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        out.extend(seg.feed(pcm[i : i + FRAME_BYTES]))
    return out


def test_rms():
    assert rms(b"") == 0.0
    assert rms(silence(0.1)) == 0.0
    assert 0.2 < rms(tone(0.1, amplitude=0.3)) < 0.25  # sine RMS = amplitude / sqrt 2


def test_silence_produces_no_segment():
    seg = SpeechSegmenter()
    assert _feed(seg, silence(5.0)) == []
    assert seg.flush() == []


def test_burst_then_silence_yields_one_segment_with_preroll_and_hang():
    seg = SpeechSegmenter(hang_s=0.9, pre_roll_s=0.4, min_speech_s=0.5)
    out = _feed(seg, silence(1.0) + tone(2.0) + silence(2.0))
    assert len(out) == 1
    s = out[0]
    assert s.reason == "silence"
    assert 0.55 <= s.start_s <= 0.66  # about 0.4 s of pre-roll before the burst at 1.0 s
    assert 3.85 <= s.end_s <= 3.95  # burst ends at 3.0 s, plus 0.9 s hang
    assert abs(len(s.pcm) / (2 * 16000) - (s.end_s - s.start_s)) < 0.001


def test_too_short_burst_is_dropped():
    seg = SpeechSegmenter(min_speech_s=0.5)
    assert _feed(seg, silence(0.5) + tone(0.2) + silence(2.0)) == []


def test_long_speech_is_cut_at_max_and_flush_closes_the_rest():
    seg = SpeechSegmenter(max_s=5.0)
    out = _feed(seg, tone(12.0))
    assert [s.reason for s in out] == ["max", "max"]
    assert all(abs((s.end_s - s.start_s) - 5.0) < 0.05 for s in out)
    tail = seg.flush()
    assert len(tail) == 1 and tail[0].reason == "flush"
    assert abs((tail[0].end_s - tail[0].start_s) - 2.0) < 0.05
