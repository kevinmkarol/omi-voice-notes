#!/usr/bin/env bash
# ABOUTME: Runs INSIDE ghcr.io/zephyrproject-rtos/ci:v0.26.13 — sets up the NCS 2.9.0 west workspace and builds the Omi CV1 firmware (sysbuild + MCUboot signing).
# ABOUTME: Mirrors upstream omi/firmware/scripts/ci/build-cv1.sh (the script Omi's own release workflow runs), split into `prepare` and `build` so the SDK download happens once.
set -euo pipefail

FW=/omi/firmware            # bind mount of upstream/omi/firmware
OUT=/out                    # bind mount for the artifacts
NCS_VERSION=v2.9.0
BOARD=omi/nrf5340/cpuapp
stage="${1:-all}"

# west/git operate on the bind-mounted tree owned by the host user; the container runs as root.
git config --global --add safe.directory '*'
# MCUboot image signing needs the `ecdsa` python package (per upstream BUILD.md).
pip3 install --quiet ecdsa 2>/dev/null || pip3 install --quiet --break-system-packages ecdsa

prepare() {
    cd "$FW"
    mkdir -p "$NCS_VERSION"
    cd "$NCS_VERSION"
    if [ ! -d .west ]; then
        echo "Initialising nRF Connect SDK $NCS_VERSION workspace..."
        west init -m https://github.com/nrfconnect/sdk-nrf --mr "$NCS_VERSION" .
    fi
    echo "Updating west modules (shallow)..."
    west update -o=--depth=1 -n
    west zephyr-export
    echo "prepare: done"
}

build() {
    cd "$FW/$NCS_VERSION"
    west zephyr-export
    # The production config lives in omi.conf; Zephyr builds prj.conf by default.
    cp "$FW/omi/omi.conf" "$FW/omi/prj.conf"
    echo "Building $BOARD with sysbuild..."
    west build -b "$BOARD" "$FW/omi" --sysbuild -d build --pristine always -- -DBOARD_ROOT="$FW"
    test -s build/dfu_application.zip
    test -s build/merged.hex
    test -s build/merged_CPUNET.hex
    mkdir -p "$OUT"
    cp build/dfu_application.zip build/merged.hex build/merged_CPUNET.hex "$OUT/"
    cp build/omi/zephyr/zephyr.signed.bin "$OUT/omi.signed.bin" 2>/dev/null || true
    cp build/omi/zephyr/.config "$OUT/omi.config" 2>/dev/null || true
    cp build/mcuboot/zephyr/.config "$OUT/mcuboot.config" 2>/dev/null || true
    cp build/build_info.yml build/partitions.yml "$OUT/" 2>/dev/null || true
    (cd build && ls -l dfu_application.zip merged.hex merged_CPUNET.hex)
    echo "build: done"
}

case "$stage" in
    prepare) prepare ;;
    build) build ;;
    all) prepare; build ;;
    *) echo "usage: $0 prepare|build|all" >&2; exit 2 ;;
esac
