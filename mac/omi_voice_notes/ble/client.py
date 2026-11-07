# ABOUTME: The bleak side: find the pendant, connect, read its identity, set its clock, subscribe to audio/button/battery, expose the storage link.
# ABOUTME: Everything protocol-shaped is delegated to uuids/audio/ring/sync so this file is only glue around BleakClient.
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from omi_voice_notes.ble import audio, ring, uuids

log = logging.getLogger("omi_voice_notes.ble")


@dataclass
class Identity:
    name: str
    address: str
    firmware: str | None
    model: str | None
    manufacturer: str | None
    codec: int | None
    battery: int | None
    charging: bool | None
    device_time: int | None
    ring_status: ring.RingStatus | None

    @property
    def codec_name(self) -> str:
        return uuids.CODEC_NAMES.get(self.codec or -1, f"codec {self.codec}")

    @property
    def ring_capable(self) -> bool:
        return uuids.has_ring_storage(self.firmware)


async def scan(timeout: float = 8.0) -> list[BLEDevice]:
    """Every pendant in range: advertises the audio service, usually named Omi."""
    found: dict[str, BLEDevice] = {}

    def on_adv(device: BLEDevice, adv) -> None:
        name = (adv.local_name or device.name or "").lower()
        services = {u.lower() for u in (adv.service_uuids or [])}
        if uuids.AUDIO_SERVICE in services or name in ("omi", "omi devkit 2", "friend"):
            found[device.address] = device

    scanner = BleakScanner(on_adv)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    return list(found.values())


async def find(address: str | None, timeout: float = 15.0) -> BLEDevice:
    """A specific pendant by address (CoreBluetooth UUID on macOS) or the first one advertising."""

    def match(device: BLEDevice, adv) -> bool:
        if address:
            return device.address.lower() == address.lower()
        services = {u.lower() for u in (adv.service_uuids or [])}
        name = (adv.local_name or device.name or "").lower()
        return uuids.AUDIO_SERVICE in services or name == "omi"

    device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if device is None:
        where = address or "any Omi"
        raise ConnectionError(
            f"{where} not seen within {timeout:.0f}s. Is it on (red/blue LED), in range, and NOT connected "
            "to the Omi phone app? It accepts one connection at a time."
        )
    return device


class StorageLinkImpl:
    """StorageLink over the control characteristic; notifications land in `notifications`."""

    def __init__(self, client: BleakClient) -> None:
        self._client = client
        self.notifications: asyncio.Queue[bytes] = asyncio.Queue()

    async def start(self) -> None:
        await self._client.start_notify(
            uuids.STORAGE_CONTROL, lambda _c, data: self.notifications.put_nowait(bytes(data))
        )

    async def write(self, payload: bytes) -> None:
        await self._client.write_gatt_char(uuids.STORAGE_CONTROL, payload, response=True)

    async def status(self) -> ring.RingStatus | None:
        try:
            return ring.parse_status(bytes(await self._client.read_gatt_char(uuids.STORAGE_STATUS)))
        except Exception as exc:  # noqa: BLE001 - older firmware has no ring status
            log.debug("ring status read failed: %s", exc)
            return None


class OmiDevice:
    def __init__(self, device: BLEDevice, disconnected: Callable[[], None] | None = None) -> None:
        self.device = device
        self._on_disconnect = disconnected
        self.client = BleakClient(device, disconnected_callback=self._disconnected, timeout=20.0)
        self.storage: StorageLinkImpl | None = None
        self._closing = False

    def _disconnected(self, _client: BleakClient) -> None:
        log.log(logging.DEBUG if self._closing else logging.WARNING, "pendant disconnected")
        if self._on_disconnect:
            self._on_disconnect()

    @property
    def connected(self) -> bool:
        return self.client.is_connected

    async def __aenter__(self) -> Self:
        await self.client.connect()
        log.info(
            "connected to %s (%s), MTU %s",
            self.device.name,
            self.device.address,
            self.client.mtu_size,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        self._closing = True
        try:
            await self.client.disconnect()
        except Exception as err:  # noqa: BLE001
            log.debug("disconnect: %s", err)

    async def _read_text(self, uuid: str) -> str | None:
        try:
            return (
                bytes(await self.client.read_gatt_char(uuid))
                .decode(errors="replace")
                .strip("\x00 ")
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("read %s failed: %s", uuid, exc)
            return None

    async def _read_u8(self, uuid: str) -> int | None:
        try:
            data = bytes(await self.client.read_gatt_char(uuid))
            return data[0] if data else None
        except Exception as exc:  # noqa: BLE001
            log.debug("read %s failed: %s", uuid, exc)
            return None

    async def identify(self) -> Identity:
        firmware = await self._read_text(uuids.DIS_FIRMWARE_REV)
        model = await self._read_text(uuids.DIS_MODEL)
        manufacturer = await self._read_text(uuids.DIS_MANUFACTURER)
        codec = await self._read_u8(uuids.AUDIO_CODEC)
        battery = await self._read_u8(uuids.BATTERY_LEVEL)
        charging = await self._read_u8(uuids.SETTINGS_CHARGING)
        device_time = None
        try:
            device_time = audio.parse_time_sync(
                bytes(await self.client.read_gatt_char(uuids.TIME_READ))
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("time read failed: %s", exc)
        status = None
        if self.storage is None:
            self.storage = StorageLinkImpl(self.client)
        status = await self.storage.status()
        return Identity(
            self.device.name or "Omi", self.device.address, firmware, model, manufacturer, codec,
            battery, None if charging is None else bool(charging), device_time, status,
        )  # fmt: skip

    async def sync_time(self, epoch_s: int | None = None) -> int:
        now = int(epoch_s if epoch_s is not None else time.time())
        await self.client.write_gatt_char(
            uuids.TIME_WRITE, audio.encode_time_sync(now), response=True
        )
        return now

    async def subscribe_audio(self, on_packet: Callable[[bytes], None]) -> None:
        await self.client.start_notify(uuids.AUDIO_DATA, lambda _c, data: on_packet(bytes(data)))

    async def unsubscribe_audio(self) -> None:
        try:
            await self.client.stop_notify(uuids.AUDIO_DATA)
        except Exception as exc:  # noqa: BLE001
            log.debug("stop_notify: %s", exc)

    async def subscribe_button(self, on_event: Callable[[int], None]) -> None:
        def cb(_c, data) -> None:
            event = audio.parse_button(bytes(data))
            if event is not None:
                on_event(event)

        try:
            await self.client.start_notify(uuids.BUTTON_TRIGGER, cb)
        except Exception as exc:  # noqa: BLE001
            log.warning("button notifications unavailable: %s", exc)

    async def subscribe_battery(self, on_level: Callable[[int], None]) -> None:
        try:
            await self.client.start_notify(
                uuids.BATTERY_LEVEL, lambda _c, d: on_level(bytes(d)[0]) if d else None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("battery notifications unavailable: %s", exc)

    async def subscribe_charging(self, on_state: Callable[[bool], None]) -> None:
        try:
            await self.client.start_notify(
                uuids.SETTINGS_CHARGING, lambda _c, d: on_state(bool(bytes(d)[0])) if d else None
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("charging notifications unavailable: %s", exc)

    async def storage_link(self) -> StorageLinkImpl:
        if self.storage is None:
            self.storage = StorageLinkImpl(self.client)
        await self.storage.start()
        return self.storage
