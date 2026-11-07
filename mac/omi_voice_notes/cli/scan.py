# ABOUTME: omi-scan: list Omi pendants in Bluetooth range and, with --identify, connect to read firmware, battery, codec, clock and ring status.
# ABOUTME: The first thing to run when the pendant misbehaves; it also tells you whether the firmware has the ring storage protocol.
from __future__ import annotations

import argparse
import asyncio
import sys

from omi_voice_notes.ble import client
from omi_voice_notes.ble.uuids import has_ring_storage
from omi_voice_notes.cli.common import fmt_clock, setup_logging


async def run(args: argparse.Namespace) -> int:
    devices = await client.scan(args.timeout)
    if not devices:
        print(
            "No Omi pendant seen. It must be on (red or blue LED), in range, and not connected to the phone app."
        )
        return 1
    for d in devices:
        print(f"{d.name or '(no name)':<16} {d.address}")
    if not args.identify:
        return 0
    target = next(
        (d for d in devices if not args.address or d.address.lower() == args.address.lower()),
        devices[0],
    )
    async with client.OmiDevice(target) as dev:
        who = await dev.identify()
        print()
        print(f"name         {who.name}")
        print(f"address      {who.address}")
        print(f"model        {who.model} by {who.manufacturer}")
        print(
            f"firmware     {who.firmware}  (ring storage: {'yes' if has_ring_storage(who.firmware) else 'NO, needs 3.0.20+'})"
        )
        print(f"codec        {who.codec_name}")
        print(f"battery      {who.battery}%  charging: {who.charging}")
        print(f"device clock {fmt_clock(who.device_time)}")
        if who.ring_status:
            s = who.ring_status
            print(
                f"ring         {s.unread_packets} unread records (~{s.unread_packets * 0.1:.0f}s), {s.used_bytes / 1e6:.1f} MB used, "
                f"{s.free_bytes / 1e6:.1f} MB free, rtc {'valid' if s.rtc_valid else 'NOT set'}"
            )
        print(f"MTU          {dev.client.mtu_size}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="omi-scan", description="Find Omi pendants over Bluetooth LE.")
    p.add_argument("--timeout", type=float, default=8.0, help="scan seconds (default 8)")
    p.add_argument(
        "--identify",
        action="store_true",
        help="connect to the first (or --address) pendant and print its details",
    )
    p.add_argument("--address", help="CoreBluetooth id of the pendant to identify")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"omi-scan: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
