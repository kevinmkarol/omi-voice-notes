# ABOUTME: The ring download state machine against a scripted fake pendant: INFO → READ (unaligned DATA) → DONE → ADVANCE, errors, late answers.
import asyncio
import struct

import pytest

from omi_voice_notes.ble import ring
from omi_voice_notes.ble.sync import RingSync, SyncError


class FakePendant:
    """Answers commands the way storage.c does; `records` is the ring content indexed by seq."""

    def __init__(self, read_seq: int, records: list[bytes], chunk: int = 500, delay: float = 0.0, refuse_read: int | None = None,
                 not_ready: int = 0):  # fmt: skip
        self.read_seq = read_seq
        self.write_seq = read_seq + len(records)
        self.records = {read_seq + i: r for i, r in enumerate(records)}
        self.chunk = chunk
        self.delay = delay
        self.refuse_read = refuse_read
        # answer this many INFO commands, and this many READ commands, with 'storage not ready' first (SD remounting)
        self.not_ready = {ring.CMD_INFO: not_ready, ring.CMD_READ: not_ready}
        self.notifications: asyncio.Queue[bytes] = asyncio.Queue()
        self.commands: list[bytes] = []
        self.advanced: list[int] = []

    def _info(self) -> bytes:
        return struct.pack(
            ">BQQIQH", ring.NOTIFY_INFO, self.read_seq, self.write_seq, 1_000_000, 0, 444
        )

    async def write(self, payload: bytes) -> None:
        self.commands.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        cmd = payload[0]
        if self.not_ready.get(cmd, 0) > 0:
            self.not_ready[cmd] -= 1
            self.notifications.put_nowait(bytes([ring.NOTIFY_ACK, ring.ACK_NOT_READY]))
            return
        if cmd == ring.CMD_INFO:
            self.notifications.put_nowait(self._info())
        elif cmd == ring.CMD_READ:
            start = struct.unpack_from(">Q", payload, 1)[0]
            if self.refuse_read is not None:
                self.notifications.put_nowait(bytes([ring.NOTIFY_ACK, self.refuse_read]))
                return
            if start < self.read_seq or start > self.write_seq:
                self.notifications.put_nowait(bytes([ring.NOTIFY_ACK, ring.ACK_SEQ_OUT_OF_RANGE]))
                return
            count = self.write_seq - start
            self.notifications.put_nowait(struct.pack(">BQI", ring.NOTIFY_READ_BEGIN, start, count))
            blob = b"".join(self.records[s] for s in range(start, self.write_seq))
            for i in range(0, len(blob), self.chunk):
                self.notifications.put_nowait(bytes([ring.NOTIFY_DATA]) + blob[i : i + self.chunk])
            self.notifications.put_nowait(struct.pack(">BBQ", ring.NOTIFY_DONE, 0, self.write_seq))
        elif cmd == ring.CMD_ADVANCE:
            seq = struct.unpack_from(">Q", payload, 1)[0]
            self.advanced.append(seq)
            ok = self.read_seq <= seq <= self.write_seq
            if ok:
                self.read_seq = seq
            self.notifications.put_nowait(
                bytes([ring.NOTIFY_ACK, 0 if ok else ring.ACK_SEQ_OUT_OF_RANGE])
            )
        elif cmd == ring.CMD_CLEAR:
            self.read_seq = self.write_seq
            self.notifications.put_nowait(bytes([ring.NOTIFY_ACK, 0]))


def _records(n: int, seed: int = 0) -> list[bytes]:
    return [bytes([(seed + i) % 256]) * 444 for i in range(n)]


async def test_download_all_collects_records_and_advances():
    pendant = FakePendant(read_seq=100, records=_records(7))
    got = []

    async def on_records(transfer):
        got.append(transfer)

    total = await RingSync(pendant).download_all(on_records)
    assert total == 7
    assert len(got) == 1
    assert got[0].start_seq == 100 and got[0].next_seq == 107 and got[0].ok
    assert got[0].records == _records(7)
    assert pendant.advanced == [107]
    assert pendant.read_seq == 107
    # INFO, READ, ADVANCE, INFO (follow-up), nothing more
    assert [c[0] for c in pendant.commands] == [
        ring.CMD_INFO,
        ring.CMD_READ,
        ring.CMD_ADVANCE,
        ring.CMD_INFO,
    ]


async def test_keep_mode_does_not_advance():
    pendant = FakePendant(read_seq=5, records=_records(3))
    seen = []

    async def on_records(transfer):
        seen.extend(transfer.records)

    total = await RingSync(pendant).download_all(on_records, advance=False)
    assert total == 3 and len(seen) == 3
    assert pendant.advanced == [] and pendant.read_seq == 5


async def test_empty_ring_returns_zero():
    pendant = FakePendant(read_seq=42, records=[])
    assert await RingSync(pendant).download_all(lambda t: asyncio.sleep(0)) == 0
    assert [c[0] for c in pendant.commands] == [ring.CMD_INFO]


async def test_refused_read_raises():
    pendant = FakePendant(read_seq=0, records=_records(2), refuse_read=ring.ACK_SEQ_OUT_OF_RANGE)
    with pytest.raises(SyncError, match="seq out of range"):
        await RingSync(pendant).download_all(lambda t: asyncio.sleep(0))


async def test_sd_card_still_mounting_is_retried(monkeypatch):
    """Seen on the real pendant: right after a reconnect the SD card is still remounting and INFO (or READ)
    is acked 'storage not ready'. The sync must ask again rather than give up."""
    import omi_voice_notes.ble.sync as sync_mod

    monkeypatch.setattr(sync_mod, "NOT_READY_RETRY_S", 0.01)
    pendant = FakePendant(
        read_seq=5, records=_records(3), not_ready=2
    )  # first two INFOs and first two READs refused
    got = []

    async def on_records(transfer):
        got.extend(transfer.records)

    total = await RingSync(pendant).download_all(on_records)
    assert total == 3 and len(got) == 3 and pendant.advanced == [8]
    cmds = [c[0] for c in pendant.commands]
    assert (
        cmds.count(ring.CMD_INFO) == 4 and cmds.count(ring.CMD_READ) == 3
    )  # 2 refused + 1 ok (+ the follow-up INFO)


async def test_sd_card_that_never_mounts_gives_up(monkeypatch):
    import omi_voice_notes.ble.sync as sync_mod

    monkeypatch.setattr(sync_mod, "NOT_READY_RETRY_S", 0.01)
    monkeypatch.setattr(sync_mod, "NOT_READY_BUDGET_S", 0.05)
    pendant = FakePendant(read_seq=0, records=_records(2), not_ready=10_000)
    with pytest.raises(SyncError, match="never came up"):
        await RingSync(pendant).info()


async def test_late_answers_within_budget_are_fine(monkeypatch):
    monkeypatch.setattr("omi_voice_notes.ble.sync.FIRST_RESPONSE_S", 0.5)
    pendant = FakePendant(read_seq=0, records=_records(2), delay=0.1)
    assert await RingSync(pendant).download_all(lambda t: asyncio.sleep(0)) == 2


async def test_no_answer_times_out(monkeypatch):
    monkeypatch.setattr("omi_voice_notes.ble.sync.FIRST_RESPONSE_S", 0.2)

    class Mute:
        notifications: asyncio.Queue = asyncio.Queue()

        async def write(self, payload: bytes) -> None:
            pass

    with pytest.raises(SyncError, match="no answer"):
        await RingSync(Mute()).info()


async def test_progress_callback_sees_counts():
    calls = []
    pendant = FakePendant(read_seq=0, records=_records(5), chunk=300)
    await RingSync(pendant, lambda d, t: calls.append((d, t))).download_all(
        lambda t: asyncio.sleep(0)
    )
    assert calls[0] == (0, 5)
    assert calls[-1] == (5, 5)
