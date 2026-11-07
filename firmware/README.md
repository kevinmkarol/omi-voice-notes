# Omi CV1 custom firmware: tap-to-mute

Stock firmware records whenever the pendant is on and unconnected, with no way to mute it. This
rebuilds the shipped `Omi_CV1_v3.0.21` with one patch (`patches/cw1-tap-to-mute.patch`):

- **It powers on muted, and a short press toggles the microphone.** Muted: purple LED, audio is
  dropped before the codec so nothing is streamed or stored, and the microphone sleeps (about
  20 µA). Recording: red LED (blue when connected), as stock. Two haptic pulses when a note
  starts, one when it ends.
- **Clients learn the state** as button codes **6** (recording) and **7** (muted) after each tap.
- **A short buzz every 30 s while recording**, so a tap that resumed recording unnoticed is
  noticed within half a minute.
- **The LED flashes instead of staying lit** when disconnected (red or purple, 100 ms every 3 s).
- **Hold 1.5 s to power off** instead of 3 s.

Same Bluetooth services, storage format, OTA path, signing key and image version (0.0.0), so the
official Omi app can still update the pendant afterwards. The upstream firmware is
[BasedHardware/omi](https://github.com/BasedHardware/omi) (MIT, Based Hardware Contributors);
`build.sh prepare` clones it, nothing of it is vendored here. The firmware string becomes
`3.0.21-cw1`, which `omi-scan --identify` shows.

## Build

Needs Docker and about 6 GB (the Zephyr CI image Omi's own release workflow pins, plus the nRF
Connect SDK 2.9.0 workspace). Nothing is installed on the host.

```sh
cd firmware
./build.sh prepare      # once: clone upstream at Omi_CV1_v3.0.21, west init/update, fetch the shipped release
./build.sh build cw1    # → out/cw1/dfu_application.zip
./check.sh out/cw1      # imgtool verify + version 0.0.0 + slot fit; must print "check: ok"
```

## Flash

Pendant on its dock, battery above 50 %, nothing else connected (Omi app closed, no
`omi-sync`/`omi-stream`, `../mac/schedule.sh remove` if the sync is scheduled).

```sh
uv run --directory ../mac omi-scan      # prints the pendant's id
./flash.sh out/cw1 <pendant id>         # resumable upload of the app image, hash check, mark pending, reset, identify
```

The link drops now and then during the upload; `flash.sh` resumes until it completes, a few
minutes in all. Alternatives that take the whole `dfu_application.zip`: nRF Connect Device Manager
(DFU tab), or the Omi app's Developer Settings → *Flash Custom Firmware*.

Then check: `omi-scan --identify` says `3.0.21-cw1`; the pendant comes up purple and
`omi-sync --status` does not grow; a tap turns it red with two pulses and storage grows again;
a second tap turns it purple with one pulse; `omi-stream` logs `recording_on` / `muted` after each tap. The clock can be stale after an
upgrade reboot until the next client connection writes the time.

## Changing it

`./build.sh build cw1` leaves the patched source under `upstream/omi/firmware/omi/`. Edit it,
create an empty `patches/cw2-<name>.patch`, run `./build.sh diff cw2` to fill it, then
`./build.sh build cw2`. `build stock` rebuilds the unmodified tag as a toolchain check.

## If it goes wrong

- **Boots but misbehaves:** flash stock back the same way with `reference/ota/omi.signed.bin`
  (same version 0.0.0, so downgrade prevention does not object).
- **Never reaches Bluetooth:** the bootloader has no revert, so recovery is a J-Link probe on the
  SWD pads: from `upstream/omi/firmware/FLASH_3.0.8/MAC/`, `JLinkExe -CommanderScript
  program_net.jlink` with `reference/Omi_CV1_v3.0.21_CPUNET.hex`, then `program_app.jlink` with
  `reference/Omi_CV1_v3.0.21.hex`. The patch touches no init code and the watchdog is compiled
  in, so a hang resets rather than freezes.
- **Raising the image version** would lock out official updates forever; `check.sh` fails if it
  is not `0.0.0+0`.
