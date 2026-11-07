# ABOUTME: omi-sync: connect to the pendant over BLE, download every unread ring record, write timestamped WAVs, transcribe them, free the ring.
# ABOUTME: Safe by default: records are only ADVANCEd (freed on the pendant) after they are on disk; --keep leaves them.
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from omi_voice_notes.ble import client
from omi_voice_notes.ble.sync import RingSync, SyncError
from omi_voice_notes.cli.common import (
    add_sink_args,
    add_transcriber_args,
    fmt_clock,
    setup_logging,
    sinks_from_args,
    transcriber_from_args,
)  # fmt: skip
from omi_voice_notes.pipeline.recorder import RingRecorder, save_recording
from omi_voice_notes.pipeline.transcribe import TranscriptionError, transcribe_recording

log = logging.getLogger("omi_voice_notes.sync")


class Progress:
    def __init__(self) -> None:
        self._last = 0.0
        self._t0 = time.monotonic()

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if done and (now - self._last < 1.0) and done != total:
            return
        self._last = now
        rate = done * 444 / max(now - self._t0, 1e-3) / 1024
        pct = f"{100 * done / total:3.0f}%" if total else "   "
        print(
            f"\r  {pct} {done}/{total or '?'} records  {rate:5.1f} KB/s",
            end="",
            file=sys.stderr,
            flush=True,
        )
        if total and done >= total:
            print(file=sys.stderr)


async def run(args: argparse.Namespace) -> int:
    sinks = sinks_from_args(args)
    transcriber = None if args.no_transcribe else transcriber_from_args(args, live=False)
    disconnected = asyncio.Event()
    try:
        device = await client.find(args.address, args.timeout)
    except ConnectionError:
        if args.if_present:
            # Scheduled run with nobody home, or another client holding the pendant: say nothing.
            log.debug("pendant not in range; nothing to do")
            return 0
        raise
    async with client.OmiDevice(device, disconnected.set) as dev:
        who = await dev.identify()
        print(
            f"{who.name} {who.address}  fw {who.firmware}  battery {who.battery}%{' charging' if who.charging else ''}  "
            f"codec {who.codec_name}  clock {fmt_clock(who.device_time)}",
            file=sys.stderr,
        )
        if not who.ring_capable and not args.force:
            print(
                f"firmware {who.firmware} predates the ring storage protocol (3.0.20). Update the pendant in the Omi app "
                "(Settings → Device Settings → Update) or pass --force to try anyway.",
                file=sys.stderr,
            )
            return 2
        if not args.no_time_sync:
            now = await dev.sync_time()
            log.info("pendant clock set to %s", fmt_clock(now))
        link = await dev.storage_link()
        sync = RingSync(link, Progress())
        if args.status or args.clear:
            info = await sync.info()
            print(
                f"ring: read_seq {info.read_seq}  write_seq {info.write_seq}  unread {info.unread} records "
                f"(~{info.unread_seconds / 60:.1f} min)  capacity {info.capacity_packets}  dropped {info.dropped_packets}  "
                f"record {info.record_size} B"
            )
            if args.clear:
                if not args.yes:
                    print(
                        "--clear needs --yes: this discards every unread record on the pendant.",
                        file=sys.stderr,
                    )
                    return 2
                await sync.clear()
                print("ring cleared")
            return 0

        recorder = RingRecorder(gap_s=args.gap)
        saved = []

        async def on_records(transfer) -> None:
            seq = transfer.start_seq
            for record in transfer.records:
                recorder.feed(record, seq)
                seq += 1
            for rec in recorder.recordings(final=False):
                saved.append(save_recording(rec, sinks.archive.recordings))
                log.info("saved %s (%.0fs)", saved[-1].name, rec.seconds)

        try:
            total = await sync.download_all(on_records, advance=not args.keep, batch=args.batch)
        except SyncError as exc:
            if disconnected.is_set():
                print(
                    f"pendant disconnected mid-sync ({exc}); already-saved recordings are kept, rerun to continue.",
                    file=sys.stderr,
                )
            else:
                print(f"sync failed: {exc}", file=sys.stderr)
            total = -1
        for rec in recorder.recordings(final=True):
            saved.append(save_recording(rec, sinks.archive.recordings))
            log.info("saved %s (%.0fs)", saved[-1].name, rec.seconds)
    if total == 0 and not saved:
        print("nothing new on the pendant")
        return 0
    print(
        f"{len(saved)} recording(s) from {total if total >= 0 else '?'} records → {sinks.archive.recordings}",
        file=sys.stderr,
    )
    if transcriber is not None:
        for path in saved:
            meta = path.with_suffix(".json")
            start = None
            if meta.exists():
                import json

                start = json.loads(meta.read_text()).get("start_epoch")
            try:
                transcribe_recording(path, transcriber, sinks, "ring", start)
            except TranscriptionError as exc:
                print(f"{path.name}: {exc}", file=sys.stderr)
        transcriber.close()
    sinks.close()
    return 0 if total >= 0 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="omi-sync",
        description="Download the Omi pendant's stored audio over Bluetooth and transcribe it.",
    )
    p.add_argument("--address", help="CoreBluetooth id of the pendant (default: first Omi seen)")
    p.add_argument(
        "--timeout", type=float, default=20.0, help="seconds to wait for the pendant to appear"
    )
    p.add_argument(
        "--keep", action="store_true", help="don't free the records on the pendant after download"
    )
    p.add_argument(
        "--batch",
        type=int,
        default=0,
        metavar="N",
        help="records per READ (default 0 = everything)",
    )
    p.add_argument(
        "--gap",
        type=float,
        default=90.0,
        metavar="S",
        help="a time jump bigger than this starts a new recording (default 90)",
    )
    p.add_argument(
        "--no-time-sync", action="store_true", help="don't set the pendant's clock first"
    )
    p.add_argument("--no-transcribe", action="store_true", help="download only")
    p.add_argument("--status", action="store_true", help="print the ring state and exit")
    p.add_argument(
        "--if-present",
        action="store_true",
        help="exit 0 quietly when no pendant is in range (for the scheduled LaunchAgent run)",
    )
    p.add_argument(
        "--clear", action="store_true", help="discard everything on the pendant (with --yes)"
    )
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="try even if the firmware looks too old")
    p.add_argument("-v", "--verbose", action="store_true")
    add_transcriber_args(p, live=False)
    add_sink_args(p)
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"omi-sync: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
