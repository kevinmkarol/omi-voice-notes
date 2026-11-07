#!/usr/bin/env bash
# ABOUTME: Pre-flash checks for a built Omi CV1 image: imgtool signature verify against the public MCUboot key, header/version dump, slot-size fit, and a byte compare of the net-core image with the shipped release.
# ABOUTME: Usage: ./check.sh out/<variant>   (needs uv; imgtool runs via `uv tool run --from imgtool`).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:?usage: $0 out/<variant>}"
KEY="$HERE/upstream/omi/firmware/bootloader/mcuboot/root-rsa-2048.pem"
REF="$HERE/reference/ota"
imgtool() { uv tool run --from imgtool imgtool "$@"; }

cd "$OUT"
rm -rf zip && mkdir zip && unzip -q -o dfu_application.zip -d zip
echo "== manifest"; python3 -c '
import json,sys
m=json.load(open("zip/manifest.json"))
for f in m["files"]:
    print(f"  {f['"'"'file'"'"']:<16} image {f['"'"'image_index'"'"']}  version {f.get('"'"'version_MCUBOOT'"'"', f.get('"'"'version'"'"'))}  {f['"'"'size'"'"']} B  load 0x{int(f['"'"'load_address'"'"']):x}")'

echo "== imgtool verify (public MCUboot key)"
imgtool verify -k "$KEY" zip/omi.signed.bin

echo "== header / version"
imgtool dumpinfo zip/omi.signed.bin | sed -n '/Image header/,/####*$/p' | grep -E 'img_size|version|hdr_size|flags'
VER="$(imgtool dumpinfo zip/omi.signed.bin | grep -E '^version:' | awk '{print $2}')"
[ "$VER" = "0.0.0+0" ] || { echo "!! image version is $VER, must stay 0.0.0+0 (downgrade prevention)"; exit 1; }

echo "== slot fit (primary slot size from partitions.yml)"
uv run --no-project --with pyyaml python3 - <<'PY'
import re, yaml, os
try:
    p = yaml.safe_load(open("partitions.yml"))
    slot = p.get("mcuboot_primary_app") or p.get("mcuboot_primary")
    size = slot["size"]
except Exception as e:
    size = None
img = os.path.getsize("zip/omi.signed.bin")
print(f"  app image {img} B; primary slot {size if size is None else f'{size} B ({img/size:.0%})'}")
if size is not None and img > size: raise SystemExit("!! image does not fit the slot")
PY

echo "== compare with shipped Omi_CV1_OTA_v3.0.21"
digest() { imgtool dumpinfo "$1" 2>/dev/null | grep -A4 'type: SHA256' | grep -E '^ +(data:)? *0x' | tr -d ' \n' | sed 's/data://; s/0x//g'; }
tlv_offset() { imgtool dumpinfo "$1" 2>/dev/null | grep -oE 'TLV area \(offset: 0x[0-9a-f]+' | grep -oE '0x[0-9a-f]+'; }
for f in omi.signed.bin ipc_radio.bin; do
    if cmp -s "zip/$f" "$REF/$f"; then echo "  $f: byte-identical to shipped"; continue; fi
    first=$( (cmp "zip/$f" "$REF/$f" || true) 2>/dev/null | awk '{print $5}' | tr -d ,)
    n=$( (cmp -l "zip/$f" "$REF/$f" 2>/dev/null || true) | wc -l | tr -d ' ')
    if [ "$(digest zip/$f)" = "$(digest $REF/$f)" ]; then
        echo "  $f: same image digest as shipped (payload identical); only the randomized RSA-PSS signature differs ($n bytes from byte $first)"
    elif [ "$f" = ipc_radio.bin ]; then
        # NSIB validation info sits at the end of the net-core payload: magic(12) addr(4) hash(32) pubkey(64) sig(64).
        # The pubkey is a per-build debug key (nrf keygen.py), so it and the signature always differ; b0n on the
        # nRF5340 net core validates the HASH only (SB_VALIDATE_FW_SIGNATURE defaults n for SOC_NRF5340_CPUNET).
        tlv=$(tlv_offset "zip/$f"); code_end=$((tlv - 0x80))
        if cmp -s -n "$code_end" "zip/$f" "$REF/$f" && [ "$(tlv_offset "$REF/$f")" = "$tlv" ]; then
            echo "  $f: net-core code and its NSIB hash identical to shipped (first $code_end bytes); only the per-build debug signing key + signature differ ($n bytes from byte $first)"
        else
            echo "  $f: net-core code DIFFERS from shipped ($n bytes from byte $first of $(stat -f%z "zip/$f")) — flash both images or rebuild"
        fi
    else
        echo "  $f: differs from shipped: $n bytes from byte $first of $(stat -f%z "zip/$f") (first $((first-1)) bytes identical); image digest differs"
    fi
done
echo "== DIS firmware string baked into the app image"
strings -n 5 zip/omi.signed.bin | grep -E '^3\.0\.21' | sort -u | sed 's/^/  /'
echo "check: ok"
