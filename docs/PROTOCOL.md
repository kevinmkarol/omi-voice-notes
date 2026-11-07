# Omi BLE protocol

What the consumer Omi (CV1) pendant speaks over Bluetooth LE, read from the firmware source
(`src/lib/core/{transport,storage,button}.c`, 3.0.20+). `mac/omi_voice_notes/ble/` implements it.
Multi-byte values are **big-endian** unless a row says otherwise.

The pendant advertises as `Omi` with the audio service UUID. It accepts **one** connection: while
the Omi app is connected nobody else can, and vice versa. No characteristic needs encryption or
bonding; if a connect fails after an old pairing, forget the device in the OS Bluetooth settings.

## 1. Services and characteristics

| Service | UUID | Characteristic | UUID | Props | Payload |
| --- | --- | --- | --- | --- | --- |
| Audio | `19B10000-E8F2-537E-4F6C-D104768A1214` | Audio data | `19B10001-…` | read, notify | live audio packets (§2) |
| | | Codec | `19B10002-…` | read | `u8`: 20 = Opus 16 kHz mono, 160-sample frames; **21** = 320-sample (20 ms) frames, what 3.0.21 reports |
| | | Speaker | `19B10003-…` | write, notify | absent on CV1 (`CONFIG_OMI_ENABLE_SPEAKER=n`) |
| Settings | `19B10010-…` | Dim ratio | `19B10011-…` | read, write | LED brightness |
| | | Mic gain | `19B10012-…` | read, write | |
| | | Charging status | `19B10013-…` | read, notify | `u8` 1 = on charger |
| Features | `19B10020-…` | Features | `19B10021-…` | read | feature bitmask |
| Time sync | `19B10030-…` | Time write | `19B10031-…` | write | `u32 LE` epoch seconds (memcpy'd on the nRF, hence LE) |
| | | Time read | `19B10032-…` | read | `u32 LE` device epoch seconds |
| Storage | `30295780-4301-EABD-2904-2849ADFEAE43` | Control | `30295781-…` | write, notify | ring commands in, control/data notifications out (§3) |
| | | Status | `30295782-…` | read, notify | 16 bytes, `u32 LE` ×4: used_bytes, unread_packets, free_bytes, rtc_valid |
| Button | `23BA7924-0000-1000-7450-346EAC492E92` | Trigger | `23BA7925-…` | read, notify | 8 bytes = `int32 LE` ×2: `[event, 0]` (§4) |
| Battery (BAS) | `0x180F` | Level | `0x2A19` | read, notify | `u8` percent |
| Device Info (DIS) | `0x180A` | Firmware rev | `0x2A26` | read | e.g. `3.0.21`; model `0x2A24` = `Omi`, manufacturer `0x2A29` = `Based Hardware` |

Write the time (`u32 LE` epoch seconds to `19B10031`) right after connecting: until the RTC is
set the LED blinks red and ring records carry uptime stamps. Ask for the largest MTU (the firmware
exchanges to 498); live frames are ≤ 160 bytes, so each fits one notification once MTU ≥ 163.

## 2. Live audio packets (notify on `19B10001`)

```
+----------------+-------+------------------------+
| packet id u16 LE | index u8 | codec payload … |
+----------------+-------+------------------------+
```

- `packet id` increments per notification and wraps at 65535; a jump means lost notifications.
- `index` is the fragment number inside one frame: `0` starts a frame, `1, 2, …` continue it. A
  frame is split only when it exceeds `MTU - 3`; with a normal MTU every frame arrives whole.
  Reassemble by starting a new frame on `index 0` and emitting a pending one after 50 ms.
- Payload: one **Opus** packet (16 kHz mono, 20 ms, 32 kbps VBR, low-delay, no FEC/DTX). Decode
  with libopus at 16000 Hz mono; each packet yields 320 samples.

## 3. Storage ring (service `30295780`)

The 512 MB SD NAND holds a ring of fixed **444-byte records** addressed by a 64-bit sequence
number: `read_seq` is the oldest unread, `write_seq` the next to be written. The client reads a
range, stores it, then *advances* `read_seq` to free space. The firmware also auto-advances past
records whose notifications completed, so a dropped link resumes close to where it stopped.

**Record layout (444 bytes):**

```
+------------------+------------------------------------------+
| timestamp u32 BE | audio payload 440 bytes                  |
+------------------+------------------------------------------+
audio payload = [len u8][opus frame len bytes] … zero padding
```

- `timestamp`: epoch seconds of the first frame; bit 31 set means seconds since boot (RTC unset).
- Frames are packed back to back, each preceded by its length; `0` is padding, and a length that
  would run past byte 440 means the frame continues in the next record. One record ≈ 100 ms.

**Commands** (write to `30295781`, with response):

| Cmd | Bytes | Meaning |
| --- | --- | --- |
| `0x10` INFO | `[0x10]` | reply NOTIFY_INFO |
| `0x11` READ | `[0x11][start_seq u64]` or `[0x11][start_seq u64][count u32]` | stream records from `start_seq`; `count` 0/absent = everything up to `write_seq` |
| `0x12` ADVANCE | `[0x12][new_read_seq u64]` | free records below `new_read_seq`; reply ACK |
| `0x13` CLEAR | `[0x13]` | drop everything; reply ACK |
| `0x03` STOP | `[0x03]` | abort a running READ; reply ACK 0 |

**Notifications** (on `30295781`, first byte is the opcode):

| Op | Layout | Meaning |
| --- | --- | --- |
| `0x01` ACK | `[01][status u8]` | 0 ok, 6 invalid command, 9 storage not ready, 10 seq out of range |
| `0x02` INFO | `[02][read_seq u64][write_seq u64][capacity u32][dropped u64][record_size u16]` = 31 bytes (newer firmware appends caps/contract/ring-id bytes; ignore extras) | |
| `0x05` READ_BEGIN | `[05][start_seq u64][count u32]` = 13 bytes | the transfer that follows covers `count` records from `start_seq` |
| `0x03` DATA | `[03][bytes …]` | raw ring bytes, **not aligned** to record boundaries; concatenate and cut every 444 bytes |
| `0x04` DONE | `[04][status u8][next_seq u64]` = 10 bytes | transfer over; `next_seq` is where the next READ should start, and what to ADVANCE to once the data is safely on disk |

A sync: subscribe to `30295781` → INFO → READ `read_seq` → collect DATA until DONE → write to
disk → ADVANCE `next_seq` → repeat while `write_seq > read_seq`. The first INFO/READ after
connecting can take up to 10 s while the SD card remounts (acked `storage not ready` meanwhile).

## 4. Button events (notify on `23BA7925`)

8 bytes, two `int32 LE`; the first is the event: 1 single tap, 2 double tap, 3 long tap,
4 button down, 5 button up. Holding 3 s powers the pendant off (it buzzes once, then goes dark);
the custom firmware in `firmware/` needs 1.5 s.

The custom firmware (DIS revision `3.0.21-cw1`) adds **6 recording on** and **7 muted** right
after each single tap, since a tap toggles the microphone. Stock firmware never sends them.

## 5. Behaviour worth knowing

- **Battery:** under 3500 mV the firmware powers off a few seconds after boot, charger or not, with
  no LED; only the button wakes it.
- **The ring is written only while nothing is connected.** Connected and subscribed → frames go
  to GATT; connected but not subscribed → dropped. Live and ring audio never overlap, and every
  sync is a gap in the recording for as long as it is connected.
- **Silence stops the audio.** The microphone's hardware acoustic-activity detection sleeps it in
  a quiet room: live notifications stop and the ring is not written until the next sound.
- **SD-write clicks.** Ring audio carries clipped 6–15 ms thumps every 7–11 s (the SD-NAND write
  current reaching the microphone); live audio has none. `pipeline/filters.py` removes them.
