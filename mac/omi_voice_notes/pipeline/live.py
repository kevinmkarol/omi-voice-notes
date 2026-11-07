# ABOUTME: The live pipeline: Opus frames in from BLE → PCM → growing WAV + speech segments → whisper in a worker → sinks.
# ABOUTME: One instance per session; anything that produces Opus frames can feed it.
from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omi_voice_notes import opus
from omi_voice_notes.ble.uuids import FRAME_SAMPLES, SAMPLE_RATE
from omi_voice_notes.pipeline import filters
from omi_voice_notes.pipeline.segmenter import Segment, SpeechSegmenter
from omi_voice_notes.pipeline.sinks import Sinks, Transcript
from omi_voice_notes.pipeline.transcribe import Transcriber, TranscriptionError
from omi_voice_notes.wav import GrowingWav, write_wav

log = logging.getLogger("omi_voice_notes.live")

FRAME_US = 20_000
GAP_FILL_MIN_US = 200_000
GAP_FILL_MAX_US = 5_000_000  # a reconnect hole longer than this is not padded further


@dataclass
class LiveStats:
    frames: int = 0
    lost: int = 0
    gaps: int = 0
    segments: int = 0
    transcribed: int = 0
    seconds: float = 0.0


class LivePipeline:
    def __init__(
        self,
        session: str,
        source: str,
        transcriber: Transcriber | None,
        sinks: Sinks,
        recordings: Path,
        start_epoch: float | None = None,
        label: str = "live",
        segmenter: SpeechSegmenter | None = None,
        record: bool = True,
    ) -> None:
        self.session, self.source = session, source
        self.start_epoch = start_epoch if start_epoch is not None else time.time()
        self._decoder = opus.Decoder(SAMPLE_RATE, 1)
        self._segmenter = segmenter or SpeechSegmenter(SAMPLE_RATE)
        self._transcriber = transcriber
        self._sinks = sinks
        self.stats = LiveStats()
        self._last_pts: int | None = None
        self._lock = threading.Lock()
        self._closed = False
        day = datetime.fromtimestamp(self.start_epoch).astimezone()
        name = day.strftime(f"omi-{label}-%Y%m%d-%H%M%S") + f"-{session[:8]}"
        self.wav: GrowingWav | None = None
        if record:
            self.wav = GrowingWav(
                recordings / day.strftime("%Y-%m-%d") / f"{name}.wav", SAMPLE_RATE, 1
            )
        self._queue: queue.Queue[Segment | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._run, name=f"omi-live-{session[:8]}", daemon=True
        )
        self._worker.start()

    # ----- audio in -----
    def feed_frame(
        self, frame: bytes, pts_us: int | None = None, lost_before: bool = False
    ) -> None:
        with self._lock:
            if self._closed:
                return
            if pts_us is not None and self._last_pts is not None:
                hole = pts_us - self._last_pts - FRAME_US
                if hole >= GAP_FILL_MIN_US:
                    self._fill(min(hole, GAP_FILL_MAX_US))
            elif lost_before:
                self._fill(FRAME_US)
            try:
                pcm = self._decoder.decode(frame)
            except opus.OpusError as exc:
                log.warning("bad frame: %s", exc)
                pcm = self._decoder.decode(None)
            self._push(pcm)
            self.stats.frames += 1
            if pts_us is not None:
                self._last_pts = pts_us

    def feed_pcm(self, pcm: bytes) -> None:
        with self._lock:
            if not self._closed:
                self._push(pcm)

    def _fill(self, hole_us: int) -> None:
        samples = int(hole_us * SAMPLE_RATE / 1_000_000)
        self.stats.gaps += 1
        self.stats.lost += max(1, samples // FRAME_SAMPLES)
        self._push(bytes(samples * 2))

    def _push(self, pcm: bytes) -> None:
        if self.wav:
            self.wav.write(pcm)
        self.stats.seconds += len(pcm) / (2 * SAMPLE_RATE)
        for segment in self._segmenter.feed(pcm):
            self._enqueue(segment)

    def _enqueue(self, segment: Segment) -> None:
        self.stats.segments += 1
        self._queue.put(segment)

    def tick(self) -> None:
        """Call every ~second: flushes the WAV header so a crash leaves a playable file."""
        if self.wav:
            self.wav.flush()

    # ----- transcription worker -----
    def _run(self) -> None:
        while True:
            segment = self._queue.get()
            if segment is None:
                return
            try:
                self._transcribe(segment)
            except TranscriptionError as exc:
                log.warning("segment %.0f-%.0fs: %s", segment.start_s, segment.end_s, exc)
            except Exception:
                log.exception("segment %.0f-%.0fs failed", segment.start_s, segment.end_s)

    def _transcribe(self, segment: Segment) -> None:
        if self._transcriber is None:
            return
        with tempfile.TemporaryDirectory(prefix="omi-seg-") as tmp:
            clip = Path(tmp) / "segment.wav"
            write_wav(clip, segment.pcm, SAMPLE_RATE, 1)
            duration = segment.end_s - segment.start_s
            meta = self._transcriber.transcribe(clip, 30 + 2 * duration)
        text = str(meta.get("result") or "").strip()
        if text and filters.is_prompt_echo(text):
            log.info(
                "segment %.0f-%.0fs: whisper recited its prompt, dropped: %r",
                segment.start_s,
                segment.end_s,
                text,
            )
            text = ""
        self.stats.transcribed += 1
        transcript = Transcript(
            source=self.source,
            text=text,
            start_epoch=self.start_epoch + segment.start_s,
            duration_s=duration,
            wav=self.wav.path if self.wav else None,
            segments=meta.get("segments", []),
            session=self.session,
            engine=str(meta.get("engine", "")),
            processing_ms=int(meta.get("processingTime") or 0),
            extra={"offset_s": round(segment.start_s, 2), "cut": segment.reason},
        )
        if text:
            self._sinks.transcript(transcript)

    # ----- lifecycle -----
    def event(self, kind: str, payload: dict) -> None:
        self._sinks.event(kind, {"session": self.session, "source": self.source, **payload})

    def close(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for segment in self._segmenter.flush():
                self._enqueue(segment)
            if self.wav:
                self.wav.close()
                if self.stats.seconds == 0:
                    self.wav.path.unlink(missing_ok=True)
                    self.wav = None
        self._queue.put(None)
        if wait:
            self._worker.join(timeout=600)
        self._decoder.close()
        self._sinks.event(
            "session_end",
            {"session": self.session, "source": self.source, "seconds": round(self.stats.seconds, 1),
             "frames": self.stats.frames, "lost": self.stats.lost, "segments": self.stats.segments,
             "wav": str(self.wav.path) if self.wav else None},
        )  # fmt: skip
