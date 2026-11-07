# ABOUTME: Where transcripts and events go: the archive on disk (recordings, daily JSONL + Markdown, events) and an optional
# ABOUTME: webhook that receives each transcript as JSON. The archive is written first; nothing downstream can lose a transcript.
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("omi_voice_notes.sinks")

DEFAULT_ARCHIVE = Path.home() / "Omi"
_RETRY = {409, 429, 500, 502, 503, 504}


def _local(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch).astimezone()


@dataclass
class Transcript:
    source: str  # ring | live | file | double_tap
    text: str
    start_epoch: float | None
    duration_s: float
    wav: Path | None
    segments: list[dict] = field(default_factory=list)
    session: str | None = None
    engine: str = ""
    processing_ms: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def start(self) -> datetime | None:
        return None if self.start_epoch is None else _local(self.start_epoch)

    @property
    def end_epoch(self) -> float | None:
        return None if self.start_epoch is None else self.start_epoch + self.duration_s

    def when(self) -> str:
        start = self.start
        if start is None:
            return "time unknown"
        end = _local(self.end_epoch or self.start_epoch)
        if end.date() == start.date():
            return f"{start:%a %Y-%m-%d %H:%M}–{end:%H:%M}"
        return f"{start:%a %Y-%m-%d %H:%M} – {end:%a %Y-%m-%d %H:%M}"

    def record(self) -> dict:
        return {
            **self.extra,
            "ts": time.time(),
            "source": self.source,
            "start_epoch": self.start_epoch,
            "start_local": None if self.start is None else self.start.isoformat(timespec="seconds"),
            "duration_s": round(self.duration_s, 2),
            "text": self.text,
            "wav": None if self.wav is None else str(self.wav),
            "segments": self.segments,
            "session": self.session,
            "engine": self.engine,
            "processing_ms": self.processing_ms,
        }


class Archive:
    """`<root>/recordings/<day>/*.wav` (written by the recorders), `<root>/transcripts/<day>.jsonl` + `.md`,
    `<root>/events/<day>.jsonl`."""

    def __init__(self, root: Path = DEFAULT_ARCHIVE) -> None:
        self.root = Path(root).expanduser()
        self.recordings = self.root / "recordings"
        self.transcripts = self.root / "transcripts"
        self.events = self.root / "events"
        for d in (self.recordings, self.transcripts, self.events):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _day(self, epoch: float | None) -> str:
        return _local(epoch or time.time()).strftime("%Y-%m-%d")

    def transcript(self, t: Transcript) -> None:
        day = self._day(t.start_epoch)
        with self._lock:
            with open(self.transcripts / f"{day}.jsonl", "a") as f:
                f.write(json.dumps(t.record(), ensure_ascii=False) + "\n")
            md = self.transcripts / f"{day}.md"
            fresh = not md.exists()
            with open(md, "a") as f:
                if fresh:
                    f.write(f"# Omi transcripts {day}\n\n")
                stamp = t.when()
                wav = f" · `{t.wav.name}`" if t.wav else ""
                f.write(
                    f"## {stamp} · {t.source} · {t.duration_s:.0f}s{wav}\n\n{t.text or '_(no speech)_'}\n\n"
                )

    def event(self, kind: str, payload: dict, epoch: float | None = None) -> None:
        day = self._day(epoch)
        line = {"ts": epoch or time.time(), "kind": kind, **payload}
        with self._lock, open(self.events / f"{day}.jsonl", "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def status(self) -> dict:
        days = sorted(p.stem for p in self.transcripts.glob("*.jsonl"))
        return {
            "root": str(self.root),
            "days": days[-7:],
            "recordings": sum(1 for _ in self.recordings.rglob("*.wav")),
        }


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Following a redirect would resend the bearer token to whatever host the Location names, as a GET
    without the transcript; a 3xx is treated as a refusal instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebhookSink:
    """POSTs each transcript's record as JSON to `url`. One worker thread keeps them in order; busy statuses
    (409, 429, 5xx) and an unreachable host are retried, anything else (redirects included) is logged and
    dropped. The transcript's start time is the Idempotency-Key, so a retry after a lost response is not a
    duplicate on the far side. The record's `wav` is the recording's path inside `archive`, or just its file
    name, so the endpoint never learns where the archive lives."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        archive: Path | None = None,
        attempts: int = 30,
        delay: float = 20,
        timeout: float = 20,
    ) -> None:
        self.url = url
        self._token = token
        self._archive = archive
        self._attempts, self._delay, self._timeout = attempts, delay, timeout
        self._opener = urllib.request.build_opener(_RefuseRedirects)
        self._queue: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=self._run, name="omi-webhook", daemon=True).start()

    def transcript(self, t: Transcript) -> None:
        if not t.text:
            return
        self._queue.put({**t.record(), "wav": self._wav_name(t.wav)})

    def _wav_name(self, wav: Path | None) -> str | None:
        if wav is None:
            return None
        if self._archive is not None and wav.is_relative_to(self._archive):
            return str(wav.relative_to(self._archive))
        return wav.name

    def join(self) -> None:
        self._queue.join()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                self.deliver(item)
            finally:
                self._queue.task_done()

    def deliver(self, item: dict) -> bool:
        key = f"omi-{item.get('start_epoch') or item.get('ts')}"
        for attempt in range(1, self._attempts + 1):
            status, detail = self._post(item, key)
            if 200 <= status < 300:
                log.info("webhook: accepted (%s) %s", status, detail)
                return True
            if status not in _RETRY and status != 0:
                log.warning("webhook: refused (%s) %s", status, detail)
                return False
            log.info("webhook: attempt %s got %s %s", attempt, status, detail)
            time.sleep(self._delay)
        log.warning("webhook: gave up after %s attempts", self._attempts)
        return False

    def _post(self, item: dict, key: str) -> tuple[int, str]:
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self.url, data=json.dumps(item).encode(), headers=headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return response.status, response.read(200).decode(errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(200).decode(errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return 0, str(exc)


class Sinks:
    """Fan-out. The archive is always first and failures elsewhere never lose a transcript."""

    def __init__(self, archive: Archive, others: list | None = None, echo=None) -> None:
        self.archive = archive
        self.others = others or []
        self.echo = echo  # callable(Transcript) for the terminal

    def transcript(self, t: Transcript) -> None:
        self.archive.transcript(t)
        if self.echo:
            try:
                self.echo(t)
            except Exception as exc:  # noqa: BLE001
                log.debug("echo failed: %s", exc)
        for sink in self.others:
            try:
                sink.transcript(t)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s failed: %s", type(sink).__name__, exc)

    def event(self, kind: str, payload: dict, epoch: float | None = None) -> None:
        self.archive.event(kind, payload, epoch)

    def close(self) -> None:
        """Waits until every queued webhook delivery has been attempted; call before the process exits."""
        for sink in self.others:
            join = getattr(sink, "join", None)
            if join:
                join()

    def describe(self) -> str:
        names = ["archive " + str(self.archive.root)] + [
            (f"webhook {s.url}" if isinstance(s, WebhookSink) else type(s).__name__)
            for s in self.others
        ]
        return ", ".join(names)


def build_sinks(
    archive: str | Path | None, webhook: str | None, token: str | None = None, echo=None
) -> Sinks:
    store = Archive(Path(archive) if archive else DEFAULT_ARCHIVE)
    others: list = []
    if webhook:
        others.append(WebhookSink(webhook, token, store.root))
    return Sinks(store, others, echo)
