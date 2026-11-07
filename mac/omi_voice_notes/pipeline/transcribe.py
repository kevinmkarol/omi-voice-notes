# ABOUTME: Local transcription backends: whisper-server (model stays loaded, for live segments) and whisper-cli (batch), same model, VAD and vocabulary.
# ABOUTME: Also `transcribe_recording`, the one path every finished WAV takes to become a Transcript in the sinks.
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from omi_voice_notes.pipeline import filters
from omi_voice_notes.whisper import (
    MODEL,
    VAD_MODEL,
    VOCABULARY,
    TranscriptionError,
    TranscriptionTimeout,
    WhisperCli,
    read_vocabulary,
    wav_duration_ms,
)

log = logging.getLogger("omi_voice_notes.transcribe")

WHISPER_SERVER = (
    os.environ.get("OMI_NOTES_WHISPER_SERVER")
    or shutil.which("whisper-server")
    or "/opt/homebrew/bin/whisper-server"
)
DEFAULT_SERVER_PORT = 8785

__all__ = [
    "Transcriber",
    "TranscriptionError",
    "TranscriptionTimeout",
    "WhisperCliTranscriber",
    "WhisperServerTranscriber",
    "make_transcriber",
    "transcribe_recording",
]  # fmt: skip


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: Path, timeout: float) -> dict:
        """{"result": text, "segments": [{"text","start","end"}], "processingTime": ms, "engine": ...}"""
        ...

    def close(self) -> None: ...


class WhisperCliTranscriber:
    """whisper-cli: loads the model per call (~1-2 s for Medium), best for whole recordings."""

    name = "whisper-cli"

    def __init__(self, language: str = "en") -> None:
        self._cli = WhisperCli(language=language)
        missing = self._cli.ready
        if missing:
            raise TranscriptionError(missing)

    def transcribe(self, audio: Path, timeout: float) -> dict:
        return self._cli.transcribe(audio, timeout, wav_duration_ms(audio))

    def close(self) -> None:
        pass


class WhisperServerTranscriber:
    """whisper.cpp's whisper-server kept warm on localhost: ~0.3-1 s per short segment on Apple silicon.
    Starts the server itself unless `url` points at one already running."""

    name = "whisper-server"

    def __init__(
        self,
        url: str | None = None,
        port: int = DEFAULT_SERVER_PORT,
        binary: str = WHISPER_SERVER,
        model: Path = MODEL,
        vad_model: Path = VAD_MODEL,
        vocabulary: Path = VOCABULARY,
        language: str = "en",
        threads: int = 6,
        startup_timeout: float = 90.0,
    ) -> None:
        self._proc: subprocess.Popen | None = None
        if url:
            self.url = url.rstrip("/")
        else:
            if not Path(binary).exists():
                raise TranscriptionError(
                    f"no whisper-server at {binary} (brew install whisper-cpp)"
                )
            if not model.exists():
                raise TranscriptionError(f"no model at {model} (run setup.sh)")
            port = port if _port_free(port) else _free_port()
            cmd = [binary, "-m", str(model), "-l", language, "-t", str(threads), "-sns", "-fa",
                   "--host", "127.0.0.1", "--port", str(port)]  # fmt: skip
            if vad_model.exists():
                cmd += ["--vad", "-vm", str(vad_model)]
            prompt = read_vocabulary(vocabulary)
            if prompt:
                cmd += ["--prompt", prompt]
            log.info("starting whisper-server on :%d (%s)", port, model.name)
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
            self.url = f"http://127.0.0.1:{port}"
            self._wait_ready(startup_timeout)

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc and self._proc.poll() is not None:
                err = (self._proc.stderr.read() if self._proc.stderr else "")[-400:]
                raise TranscriptionError(
                    f"whisper-server exited {self._proc.returncode}: {err.strip()}"
                )
            try:
                with urllib.request.urlopen(self.url + "/", timeout=2) as r:
                    r.read(100)
                return
            except urllib.error.HTTPError:
                return  # any HTTP answer means it is listening
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.3)
        raise TranscriptionError(f"whisper-server did not come up within {timeout:.0f}s")

    def transcribe(self, audio: Path, timeout: float) -> dict:
        started = time.monotonic()
        boundary = "----omi-voice-notes" + uuid.uuid4().hex
        body = bytearray()

        def field(name: str, value: str) -> None:
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )

        field("temperature", "0.0")
        field("response_format", "verbose_json")
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode()
        )
        body.extend(audio.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            self.url + "/inference",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(
                f"whisper-server HTTP {exc.code}: {exc.read(200).decode(errors='replace')}"
            ) from None
        except TimeoutError:
            raise TranscriptionTimeout(
                f"whisper-server gave no result within {timeout:.0f}s"
            ) from None
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise TranscriptionError(f"whisper-server unreachable: {exc}") from None
        if "error" in payload:
            raise TranscriptionError(f"whisper-server: {payload['error']}")
        segments = [
            {
                "text": str(s.get("text", "")).strip(),
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
            }
            for s in payload.get("segments", [])
        ]
        text = (
            " ".join(s["text"] for s in segments if s["text"])
            or str(payload.get("text", "")).strip()
        )
        return {
            "result": text,
            "segments": segments,
            "processingTime": round((time.monotonic() - started) * 1000),
            "engine": "whisper-server",
            "modelName": MODEL.name,
        }

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_transcriber(
    engine: str = "auto", server_url: str | None = None, live: bool = False
) -> Transcriber:
    """auto = whisper-server for live use when the binary exists, whisper-cli otherwise."""
    if engine == "server" or (
        engine == "auto" and (server_url or (live and Path(WHISPER_SERVER).exists()))
    ):
        return WhisperServerTranscriber(url=server_url)
    return WhisperCliTranscriber()


def transcribe_recording(
    audio: Path,
    transcriber: Transcriber,
    sinks,
    source: str,
    start_epoch: float | None,
    session: str | None = None,
    extra: dict | None = None,
):
    """The one path from a finished WAV to the sinks. Returns the Transcript (text may be empty)."""
    from omi_voice_notes.pipeline.sinks import Transcript

    duration_ms = wav_duration_ms(audio) or 0
    timeout = 60 + 2 * duration_ms / 1000
    meta = transcriber.transcribe(audio, timeout)
    text = str(meta.get("result") or "").strip()
    reason = "empty"
    if text and filters.is_prompt_echo(text):
        log.info("%s: whisper recited its prompt, dropped: %r", audio.name, text)
        text, reason = "", "prompt_echo"
    transcript = Transcript(
        source=source,
        text=text,
        start_epoch=start_epoch,
        duration_s=duration_ms / 1000,
        wav=audio,
        segments=meta.get("segments", []),
        session=session,
        engine=str(meta.get("engine", transcriber.name)),
        processing_ms=int(meta.get("processingTime") or 0),
        extra=extra or {},
    )
    if transcript.text:
        sinks.transcript(transcript)
    else:
        sinks.event("no_speech", {"source": source, "session": session, "wav": str(audio), "seconds": round(duration_ms / 1000, 1),
                                  "start_epoch": start_epoch, "reason": reason, **(extra or {})})  # fmt: skip
    return transcript
