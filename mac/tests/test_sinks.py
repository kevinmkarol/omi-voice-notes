# ABOUTME: The archive layout (jsonl + markdown per day, events), transcript time formatting, and the webhook sink's
# ABOUTME: delivery: JSON POST with a bearer token, retries on busy statuses, give up on refusals and redirects, archive never blocked.
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from omi_voice_notes.pipeline.sinks import Archive, Sinks, Transcript, WebhookSink, build_sinks


def test_archive_writes_jsonl_markdown_and_events(tmp_path):
    archive = Archive(tmp_path / "omi")
    t = Transcript(
        "ring", "Hello there.", 1_762_000_000.0, 12.5, Path("/x/omi-1.wav"), engine="whisper-cli"
    )
    sinks = Sinks(archive)
    sinks.transcript(t)
    sinks.event("button", {"button": "double_tap"}, epoch=1_762_000_000.0)
    day = t.start.strftime("%Y-%m-%d")
    lines = (archive.transcripts / f"{day}.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert (
        rec["text"] == "Hello there." and rec["source"] == "ring" and rec["wav"] == "/x/omi-1.wav"
    )
    md = (archive.transcripts / f"{day}.md").read_text()
    assert md.startswith(f"# Omi transcripts {day}") and "Hello there." in md and "omi-1.wav" in md
    events = (archive.events / f"{day}.jsonl").read_text().splitlines()
    assert json.loads(events[0])["button"] == "double_tap"
    assert archive.status()["days"] == [day]


def test_when_formats_same_day_ranges():
    t = Transcript("live", "x", 1_762_000_000.0, 90.0, None)
    text = t.when()
    assert "–" in text and text.count(":") == 2  # HH:MM–HH:MM on one day
    assert Transcript("live", "x", None, 1.0, None).when() == "time unknown"


def test_failing_sink_does_not_block_the_archive(tmp_path):
    class Boom:
        def transcript(self, t):
            raise RuntimeError("webhook down")

    archive = Archive(tmp_path)
    Sinks(archive, [Boom()]).transcript(Transcript("live", "kept", 1_762_000_000.0, 1.0, None))
    assert any(p.suffix == ".jsonl" for p in archive.transcripts.iterdir())


class _Endpoint:
    """A local HTTP server that answers each request with the next status in `statuses` and keeps what it got
    (the body is None for a GET). A 3xx status points at `redirect_to`."""

    def __init__(self, statuses: list[int], redirect_to: str | None = None) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[dict, dict | None]] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self._answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def do_GET(self):
                self._answer(None)

            def _answer(self, body):
                endpoint.requests.append((dict(self.headers), body))
                status = endpoint.statuses.pop(0) if endpoint.statuses else 200
                self.send_response(status)
                if 300 <= status < 400:
                    self.send_header("Location", redirect_to)
                self.end_headers()
                self.wfile.write(b"ok" if status < 300 else b"no")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/transcripts"

    def close(self) -> None:
        self.server.shutdown()


def test_webhook_posts_the_transcript_record_with_token():
    endpoint = _Endpoint([200])
    try:
        sink = WebhookSink(endpoint.url, token="s3cret", delay=0)
        sink.transcript(
            Transcript(
                "ring",
                "Buy milk.",
                1_762_000_000.0,
                4.0,
                Path("/x/omi-1.wav"),
                engine="whisper-cli",
            )
        )
        sink.join()
    finally:
        endpoint.close()
    headers, body = endpoint.requests[0]
    assert headers["Authorization"] == "Bearer s3cret"
    assert headers["Idempotency-Key"] == "omi-1762000000.0"
    assert body["text"] == "Buy milk." and body["source"] == "ring" and body["wav"] == "omi-1.wav"
    assert body["start_local"].startswith("2025-11-01") and body["duration_s"] == 4.0


def test_webhook_sends_the_recording_path_relative_to_the_archive(tmp_path):
    endpoint = _Endpoint([200])
    try:
        sinks = build_sinks(tmp_path / "omi", endpoint.url)
        wav = sinks.archive.recordings / "2025-11-01" / "omi-1.wav"
        sinks.transcript(Transcript("ring", "Buy milk.", 1_762_000_000.0, 4.0, wav))
        sinks.close()
    finally:
        endpoint.close()
    _, body = endpoint.requests[0]
    assert body["wav"] == "recordings/2025-11-01/omi-1.wav"
    archived = json.loads((sinks.archive.transcripts / "2025-11-01.jsonl").read_text())
    assert archived["wav"] == str(wav)


def test_webhook_refuses_redirects():
    target = _Endpoint([200])
    redirector = _Endpoint([302], redirect_to=target.url)
    try:
        sink = WebhookSink(redirector.url, token="s3cret", delay=0)
        assert sink.deliver({"text": "note", "ts": 1.0}) is False
    finally:
        redirector.close()
        target.close()
    assert len(redirector.requests) == 1
    assert (
        target.requests == []
    )  # following it would hand the token to another host, as a GET with no transcript


def test_webhook_retries_busy_statuses_and_stops_on_refusal():
    endpoint = _Endpoint([503, 429, 200, 404, 200])
    try:
        sink = WebhookSink(endpoint.url, delay=0)
        assert sink.deliver({"text": "first", "ts": 1.0}) is True
        assert sink.deliver({"text": "second", "ts": 2.0}) is False
    finally:
        endpoint.close()
    assert [b["text"] for _, b in endpoint.requests] == ["first", "first", "first", "second"]
    assert "Authorization" not in endpoint.requests[0][0]


def test_webhook_skips_empty_transcripts():
    endpoint = _Endpoint([])
    try:
        sink = WebhookSink(endpoint.url, delay=0)
        sink.transcript(Transcript("live", "", 1_762_000_000.0, 1.0, None))
        sink.join()
    finally:
        endpoint.close()
    assert endpoint.requests == []


def test_close_waits_for_webhook_delivery(tmp_path):
    endpoint = _Endpoint([503, 200])
    try:
        sinks = Sinks(Archive(tmp_path), [WebhookSink(endpoint.url, delay=0.05)])
        sinks.transcript(Transcript("ring", "late", 1_762_000_000.0, 1.0, None))
        sinks.close()
    finally:
        endpoint.close()
    assert [b["text"] for _, b in endpoint.requests] == ["late", "late"]
