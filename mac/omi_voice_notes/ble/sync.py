# ABOUTME: The ring download state machine (INFO → READ → DATA… → DONE → ADVANCE, repeat) over an abstract storage link.
# ABOUTME: Takes any object with `write(bytes)` and an asyncio.Queue of notifications, so tests drive it with a fake pendant.
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from omi_voice_notes.ble import ring

log = logging.getLogger("omi_voice_notes.ble.sync")

FIRST_RESPONSE_S = 12.0  # SD remount after connect can hold INFO/READ for 5 s
NOT_READY_RETRY_S = 2.0  # pause before asking again after a 'storage not ready' ack
NOT_READY_BUDGET_S = (
    40.0  # give up on the SD card after this long (firmware itself waits 5 s per request)
)
DATA_GAP_S = 6.0  # silence mid-transfer longer than this means the link stalled
ADVANCE_S = 6.0


class StorageLink(Protocol):
    """What the sync needs from a connection: write a command, receive notifications in order."""

    notifications: asyncio.Queue[bytes]

    async def write(self, payload: bytes) -> None: ...


class SyncError(RuntimeError):
    pass


@dataclass
class Transfer:
    start_seq: int
    records: list[bytes] = field(default_factory=list)
    next_seq: int = 0
    status: int = 0
    data_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.status == ring.ACK_OK


ProgressFn = Callable[[int, int], None]  # (records so far, records expected)
RecordsFn = Callable[[Transfer], Awaitable[None]]


class RingSync:
    def __init__(self, link: StorageLink, progress: ProgressFn | None = None) -> None:
        self._link = link
        self._progress = progress or (lambda done, total: None)

    async def _next(self, timeout: float) -> ring.Notification:
        while True:
            try:
                raw = await asyncio.wait_for(self._link.notifications.get(), timeout)
            except TimeoutError:
                raise SyncError(f"no answer from the pendant within {timeout:.0f}s") from None
            parsed = ring.parse_notification(raw)
            if parsed is None:
                log.debug("ignoring notification %s", raw.hex())
                continue
            return parsed

    async def _drain(self) -> None:
        while not self._link.notifications.empty():
            self._link.notifications.get_nowait()

    async def _not_ready(self, what: str, msg: ring.Ack, started: float) -> None:
        """The SD card is powered down between connections when the room is quiet and takes a few seconds to
        remount after we connect; the firmware already holds each request 5 s, so keep asking for a while."""
        if msg.status != ring.ACK_NOT_READY:
            raise SyncError(f"{what} refused: {msg}")
        waited = time.monotonic() - started
        if waited > NOT_READY_BUDGET_S:
            raise SyncError(f"{what} refused: {msg} for {waited:.0f}s; the SD card never came up")
        log.info("%s: SD card still mounting, asking again", what)
        await asyncio.sleep(NOT_READY_RETRY_S)

    async def info(self) -> ring.RingInfo:
        started = time.monotonic()
        while True:
            await self._drain()
            await self._link.write(ring.encode_info())
            msg = await self._next(FIRST_RESPONSE_S)
            while not isinstance(msg, ring.RingInfo | ring.Ack):
                msg = await self._next(FIRST_RESPONSE_S)
            if isinstance(msg, ring.RingInfo):
                return msg
            await self._not_ready("INFO", msg, started)

    async def advance(self, new_read_seq: int) -> None:
        await self._link.write(ring.encode_advance(new_read_seq))
        while True:
            msg = await self._next(ADVANCE_S)
            if isinstance(msg, ring.Ack):
                if not msg.ok:
                    raise SyncError(f"ADVANCE to {new_read_seq} refused: {msg}")
                return

    async def clear(self) -> None:
        await self._drain()
        await self._link.write(ring.encode_clear())
        while True:
            msg = await self._next(ADVANCE_S)
            if isinstance(msg, ring.Ack):
                if not msg.ok:
                    raise SyncError(f"CLEAR refused: {msg}")
                return

    async def stop(self) -> None:
        await self._link.write(ring.encode_stop())

    async def read(self, start_seq: int, count: int = 0) -> Transfer:
        """One READ: returns every whole record received plus the DONE status/next_seq. A 'storage not ready'
        ack before any data means the SD card is still mounting: wait and ask again."""
        started = time.monotonic()
        while True:
            await self._drain()
            await self._link.write(ring.encode_read(start_seq, count))
            transfer = Transfer(start_seq)
            reassembler = ring.RecordReassembler()
            expected = count
            timeout = FIRST_RESPONSE_S
            msg = await self._next(timeout)
            if isinstance(msg, ring.Ack) and not msg.ok and not transfer.records:
                await self._not_ready(f"READ from {start_seq}", msg, started)
                continue
            break
        while True:
            if isinstance(msg, ring.Ack):
                if msg.ok and transfer.records:
                    msg = await self._next(timeout)
                    continue  # a stray ack; the transfer is still running
                raise SyncError(f"READ from {start_seq} refused: {msg}")
            if isinstance(msg, ring.ReadBegin):
                expected = msg.count
                transfer.start_seq = msg.start_seq
                timeout = DATA_GAP_S
                self._progress(0, expected)
            elif isinstance(msg, ring.Data):
                transfer.data_bytes += len(msg.payload)
                transfer.records.extend(reassembler.append(msg.payload))
                self._progress(len(transfer.records), expected)
                timeout = DATA_GAP_S
            elif isinstance(msg, ring.Done):
                transfer.status, transfer.next_seq = msg.status, msg.next_seq
                if reassembler.pending:
                    log.warning("transfer ended with %d stray bytes", reassembler.pending)
                return transfer
            # anything else (a late INFO from a previous request) is skipped
            msg = await self._next(timeout)

    async def download_all(
        self, on_records: RecordsFn, advance: bool = True, batch: int = 0
    ) -> int:
        """Pull everything unread, hand each transfer to `on_records` (which must persist it), then
        ADVANCE so the pendant frees the space. Returns the number of records downloaded."""
        total = 0
        while True:
            info = await self.info()
            if info.write_seq <= info.read_seq:
                return total
            log.info(
                "ring: %d unread records (~%.0f s) from seq %d, %d dropped",
                info.unread, info.unread_seconds, info.read_seq, info.dropped_packets,
            )  # fmt: skip
            transfer = await self.read(info.read_seq, batch)
            if not transfer.ok:
                raise SyncError(
                    f"transfer from {info.read_seq} ended with {ring.ACK_NAMES.get(transfer.status, transfer.status)}"
                )
            if not transfer.records:
                return total
            await on_records(transfer)
            total += len(transfer.records)
            if advance:
                await self.advance(transfer.next_seq)
            else:
                return total
            if transfer.next_seq >= info.write_seq and batch == 0:
                # Loop once more only to pick up records written during the transfer.
                follow = await self.info()
                if follow.write_seq <= follow.read_seq:
                    return total
