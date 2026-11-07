#!/usr/bin/env bash
# ABOUTME: Flash a built Omi CV1 app image over Bluetooth from the Mac with smpmgr: resumable upload (the link drops now and then), hash check on the pendant, mark pending, reset, then identify.
# ABOUTME: Usage: ./flash.sh out/<variant> <pendant CoreBluetooth UUID, as omi-scan prints it>. Uploads the APP CORE ONLY (image 0); the net core is untouched. Needs uv.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: $0 out/<variant> <pendant uuid>}"
UUID="${2:?usage: $0 out/<variant> <pendant uuid>   (omi-scan prints it)}"
IMG="$HERE/$OUT/zip/omi.signed.bin"
[ -f "$IMG" ] || { unzip -q -o "$HERE/$OUT/dfu_application.zip" -d "$HERE/$OUT/zip"; }
smp() { uv tool run smpmgr --ble "$UUID" --timeout 10 "$@"; }
smp_retry() {   # another client (e.g. the scheduled omi-sync) may hold the pendant for a few seconds: try a few times
    local i; for i in 1 2 3 4 5 6; do smp "$@" && return 0; echo "  ($1 $2: pendant busy or link dropped, retry $i)" >&2; sleep 5; done; return 1
}
HASH="$(uv tool run --from imgtool imgtool verify -k "$HERE/upstream/omi/firmware/bootloader/mcuboot/root-rsa-2048.pem" "$IMG" | awk '/Image digest/ {print toupper($3)}')"
[ -n "$HASH" ] || { echo "image does not verify"; exit 1; }
echo "image $IMG"; echo "digest $HASH"

# 1. Upload. The pendant keeps a partial upload across disconnects (until it reboots), so just resume.
#    smpmgr's own `upgrade` is NOT used: for image 0 it skips the pending mark and only resets.
for i in $(seq 1 30); do
    if smp --loglevel INFO image upload "$IMG" > "$HERE/$OUT/upload-$i.log" 2>&1; then echo "upload complete (attempt $i)"; break; fi
    echo "attempt $i: $(grep -oE 'Upload offset=[0-9]+' "$HERE/$OUT/upload-$i.log" | tail -1 || true), link dropped; resuming"
    [ "$i" -lt 30 ] || { echo "upload did not complete"; exit 1; }
done

# 2. The pendant hashes what landed in its flash; it must match the build.
states="$(smp_retry image state-read 2>&1)"
echo "$states" | grep -E 'slot=|hash=|HashBytes|active=|pending=' | tr -d ' ' | paste -sd' ' - | fold -w 120 || true
echo "$states" | tr -d ' \n' | grep -q "slot=1,version='[^']*',image=0,hash=HashBytes('$HASH')" || { echo "slot 1 does not hold $HASH; not marking"; exit 1; }

# 3. Mark pending (test swap; this MCUboot is overwrite-only so there is no revert) and reset.
smp_retry image state-write "$HASH" > /dev/null 2>&1 || { echo "state-write failed"; exit 1; }
echo "marked pending; resetting"
smp_retry os reset > /dev/null 2>&1 || echo "reset command failed; the pending image is applied at the next reboot (long-press off, then on)"

# 4. MCUboot copies the image (seconds), the pendant boots and advertises again.
cd "$HERE/../mac"
for i in 1 2 3 4 5 6 7 8; do
    out="$(uv run omi-scan --identify --timeout 12 2>&1 || true)"
    if echo "$out" | grep -q '^firmware'; then echo "$out" | grep -E '^(firmware|battery|device clock|ring)'; exit 0; fi
    echo "waiting for the pendant to come back ($i)"
done
echo "pendant not seen after reset; check its LED"; exit 1
