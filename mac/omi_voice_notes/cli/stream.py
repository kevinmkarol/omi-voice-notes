# ABOUTME: omi-stream: connect to the pendant over BLE and transcribe its microphone live (frames → whisper-server segments → sinks), recording a WAV.
# ABOUTME: Reconnects when the pendant drops; a double tap sends the last two minutes of transcript to the webhook if one is configured.
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import uuid

from omi_voice_notes.ble import client, uuids
from omi_voice_notes.ble.audio import FrameReassembler
from omi_voice_notes.ble.uuids import BUTTON_DOUBLE_TAP, BUTTON_NAMES
from omi_voice_notes.cli.common import (
    add_sink_args,
    add_transcriber_args,
    fmt_clock,
    setup_logging,
    sinks_from_args,
    transcriber_from_args,
)  # fmt: skip
from omi_voice_notes.pipeline.live import LivePipeline
from omi_voice_notes.pipeline.sinks import Transcript, WebhookSink

log = logging.getLogger("omi_voice_notes.stream")


class Recent:
    """The last `window_s` of transcripts, for the double-tap question."""

    def __init__(self, window_s: float = 120.0) -> None:
        self.window_s = window_s
        self._items: list[Transcript] = []

    def add(self, t: Transcript) -> None:
        self._items.append(t)
        cutoff = time.time() - self.window_s
        self._items = [i for i in self._items if (i.end_epoch or 0) >= cutoff]

    def text(self) -> str:
        return " ".join(i.text for i in self._items if i.text)


async def run(args: argparse.Namespace) -> int:
    sinks = sinks_from_args(args)
    recent = Recent()
    if sinks.echo:
        inner = sinks.echo

        def echo(t: Transcript) -> None:
            recent.add(t)
            inner(t)

        sinks.echo = echo
    transcriber = transcriber_from_args(args, live=True)
    hooks = [s for s in sinks.others if isinstance(s, WebhookSink)]
    session = uuid.uuid4().hex
    pipeline = LivePipeline(
        session,
        "live",
        transcriber,
        sinks,
        sinks.archive.recordings,
        label="live",
        record=not args.no_record,
    )
    reassembler = FrameReassembler()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    disconnected = asyncio.Event()
    battery = {"level": None}

    def on_packet(data: bytes) -> None:
        now = loop.time()
        for frame in reassembler.feed(data, now):
            pipeline.feed_frame(frame.data, None, frame.gap)

    def on_button(event: int) -> None:
        name = BUTTON_NAMES.get(event, str(event))
        log.info("button: %s", name)
        pipeline.event("button", {"button": name})
        if event == BUTTON_DOUBLE_TAP and hooks:
            text = recent.text()
            if text:
                for hook in hooks:
                    hook.transcript(
                        Transcript(
                            "double_tap",
                            text,
                            time.time() - recent.window_s,
                            recent.window_s,
                            None,
                            session=session,
                        )
                    )

    def on_battery(level: int) -> None:
        battery["level"] = level

    deadline = loop.time() + args.duration if args.duration else None
    for sig in (signal.SIGTERM,):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            disconnected.clear()
            try:
                device = await client.find(args.address, args.timeout)
            except ConnectionError as exc:
                log.warning("%s; retrying", exc)
                await asyncio.sleep(5)
                continue
            try:
                async with client.OmiDevice(device, disconnected.set) as dev:
                    who = await dev.identify()
                    battery["level"] = who.battery
                    print(
                        f"{who.name} {who.address}  fw {who.firmware}  battery {who.battery}%  codec {who.codec_name}  "
                        f"clock {fmt_clock(who.device_time)}",
                        file=sys.stderr,
                    )
                    if who.codec not in uuids.OPUS_CODECS:
                        log.warning("codec %s is not Opus; audio will not decode", who.codec_name)
                    await dev.sync_time()
                    pipeline.event(
                        "connected",
                        {"address": who.address, "firmware": who.firmware, "battery": who.battery},
                    )
                    await dev.subscribe_button(on_button)
                    await dev.subscribe_battery(on_battery)
                    await dev.subscribe_audio(on_packet)
                    log.info("streaming; Ctrl-C to stop")
                    last_report = loop.time()
                    while not disconnected.is_set() and not stop.is_set():
                        await asyncio.sleep(0.05)
                        now = loop.time()
                        for frame in reassembler.flush(now):
                            pipeline.feed_frame(frame.data, None, frame.gap)
                        if now - last_report >= 1.0:
                            pipeline.tick()
                        if now - last_report >= 10.0:
                            s = pipeline.stats
                            log.info("%.0fs audio, %d frames, %d lost, %d segments, %d transcribed, battery %s%%",
                                     s.seconds, s.frames, s.lost, s.segments, s.transcribed, battery["level"])  # fmt: skip
                            last_report = now
                        if deadline and now >= deadline:
                            stop.set()
                    if not stop.is_set():
                        pipeline.event("disconnected", {})
                        log.warning("pendant gone; reconnecting")
                        await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                log.warning("connection failed: %s; retrying in 5 s", exc)
                await asyncio.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        for frame in reassembler.flush(loop.time(), force=True):
            pipeline.feed_frame(frame.data, None, frame.gap)
        print("\nfinishing transcription…", file=sys.stderr)
        pipeline.close(wait=True)
        if transcriber:
            transcriber.close()
        sinks.close()
        s = pipeline.stats
        print(
            f"{s.seconds:.0f}s recorded, {s.frames} frames, {s.lost} lost, {s.transcribed} segments transcribed"
            + (f" → {pipeline.wav.path}" if pipeline.wav else ""),
            file=sys.stderr,
        )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="omi-stream", description="Live transcription from the Omi pendant over Bluetooth."
    )
    p.add_argument("--address", help="CoreBluetooth id of the pendant (default: first Omi seen)")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument(
        "--duration",
        type=float,
        metavar="S",
        help="stop after this many seconds (default: until Ctrl-C)",
    )
    p.add_argument("--no-record", action="store_true", help="transcribe only, keep no WAV")
    p.add_argument("-v", "--verbose", action="store_true")
    add_transcriber_args(p, live=True)
    add_sink_args(p)
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
