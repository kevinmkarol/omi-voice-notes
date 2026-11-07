# ABOUTME: Turns ring records (timestamp + Opus frames) into timestamped recordings: decodes, splits on time gaps, writes WAV + sidecar JSON.
# ABOUTME: Recording objects carry the start time, seconds and ring sequence range, so transcription and the archive treat every source alike.
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from omi_voice_notes import opus
from omi_voice_notes.ble import ring
from omi_voice_notes.ble.uuids import FRAME_SAMPLES, SAMPLE_RATE
from omi_voice_notes.pipeline import filters

log = logging.getLogger("omi_voice_notes.recorder")

FRAME_S = FRAME_SAMPLES / SAMPLE_RATE


@dataclass
class Recording:
    start_epoch: float | None  # None when the pendant's clock was never set
    pcm: bytearray = field(default_factory=bytearray)
    frames: int = 0
    records: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    uptime_start: int | None = None
    path: Path | None = None
    clicks: int = 0  # SD-write thumps removed by filters.declick

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (2 * SAMPLE_RATE)

    @property
    def start(self) -> datetime | None:
        return (
            None
            if self.start_epoch is None
            else datetime.fromtimestamp(self.start_epoch).astimezone()
        )

    def name(self) -> str:
        if self.start is not None:
            return self.start.strftime("omi-%Y%m%d-%H%M%S")
        return f"omi-uptime-{self.uptime_start or 0}-seq{self.first_seq or 0}"

    def meta(self) -> dict:
        return {
            "start_epoch": self.start_epoch,
            "start_local": None if self.start is None else self.start.isoformat(timespec="seconds"),
            "seconds": round(self.seconds, 2),
            "frames": self.frames,
            "records": self.records,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "clock": "epoch" if self.start_epoch is not None else "uptime",
            "sample_rate": SAMPLE_RATE,
            "clicks_removed": self.clicks,
            "origin": "omi-ring",
        }


class RingRecorder:
    """Feed records in sequence order; `recordings()` yields closed ones. A gap larger than `gap_s`
    between a record's timestamp and where the audio so far says we should be starts a new file."""

    def __init__(self, gap_s: float = 90.0, declick: bool = True) -> None:
        self.gap_s = gap_s
        self.declick = declick
        self._decoder = opus.Decoder(SAMPLE_RATE, 1)
        self._current: Recording | None = None
        self._done: list[Recording] = []
        self._next_seq: int | None = None

    def feed(self, record: bytes, seq: int | None = None) -> None:
        if len(record) < ring.RECORD_SIZE:
            log.warning("short record (%d bytes) skipped", len(record))
            return
        stamp = ring.record_timestamp(record)
        raw_stamp = int.from_bytes(record[:4], "big")
        frames = ring.record_frames(record)
        if seq is not None and self._next_seq is not None and seq != self._next_seq:
            log.info("seq jump %d -> %d, starting a new recording", self._next_seq, seq)
            self._close()
        if self._current is not None:
            expected = (self._current.start_epoch or 0) + self._current.seconds
            if (
                stamp is not None
                and self._current.start_epoch is not None
                and abs(stamp - expected) > self.gap_s
                or (stamp is None) != (self._current.start_epoch is None)
            ):
                self._close()
        if self._current is None:
            self._current = Recording(
                start_epoch=float(stamp) if stamp is not None else None,
                uptime_start=None if stamp is not None else raw_stamp & ~ring.UPTIME_FLAG,
                first_seq=seq,
            )
        rec = self._current
        for frame in frames:
            try:
                rec.pcm.extend(self._decoder.decode(frame))
            except opus.OpusError as exc:
                log.warning("bad frame in record seq %s: %s", seq, exc)
                rec.pcm.extend(self._decoder.decode(None))
            rec.frames += 1
        rec.records += 1
        rec.last_seq = seq
        if seq is not None:
            self._next_seq = seq + 1

    def _close(self) -> None:
        if self._current is not None and self._current.frames:
            if self.declick:
                cleaned, self._current.clicks = filters.declick(bytes(self._current.pcm))
                if self._current.clicks:
                    log.info(
                        "removed %d click(s) from %s", self._current.clicks, self._current.name()
                    )
                    self._current.pcm = bytearray(cleaned)
            self._done.append(self._current)
        self._current = None

    def recordings(self, final: bool = False) -> list[Recording]:
        if final:
            self._close()
        out, self._done = self._done, []
        return out


def save_recording(rec: Recording, directory: Path) -> Path:
    from omi_voice_notes.wav import write_wav

    day = rec.start.strftime("%Y-%m-%d") if rec.start else "undated"
    base = directory / day / rec.name()
    path = base.with_suffix(".wav")
    n = 1
    while path.exists():
        path = base.with_name(f"{base.name}-{n}").with_suffix(".wav")
        n += 1
    write_wav(path, bytes(rec.pcm), SAMPLE_RATE, 1)
    path.with_suffix(".json").write_text(json.dumps(rec.meta(), indent=2))
    rec.path = path
    return path


def utc_now() -> datetime:
    return datetime.now(UTC)
