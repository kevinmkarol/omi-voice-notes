#!/usr/bin/env bash
# ABOUTME: Host-side driver for the Omi CV1 custom firmware: `prepare` downloads the SDK workspace and the shipped release once, `build stock|cwN` builds the unmodified tag or patches/cwN-*.patch on top of it.
# ABOUTME: Everything runs in the Zephyr CI Docker image Omi's own release workflow uses; outputs land in firmware/out/<variant>/. Each cwN patch is cumulative against the tag.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM="$HERE/upstream"
IMAGE="${CI_IMAGE:-ghcr.io/zephyrproject-rtos/ci:v0.26.13}"
TAG_COMMIT=f12031d15ea177bf8f6ac0781d7f28f3dc433eae   # Omi_CV1_v3.0.21
RELEASE_URL=https://github.com/BasedHardware/omi/releases/download/Omi_CV1_v3.0.21
REFERENCE="$HERE/reference"

usage() { echo "usage: $0 prepare | build stock|cwN | diff cwN | reset   (cwN = patches/cwN-*.patch)" >&2; exit 2; }

patch_for() {   # the one patches/<variant>-*.patch for a variant
    local matches=("$HERE"/patches/"$1"-*.patch)
    [ ${#matches[@]} -eq 1 ] && [ -f "${matches[0]}" ] || { echo "need exactly one patches/$1-*.patch" >&2; exit 2; }
    echo "${matches[0]}"
}

ensure_upstream() {
    if [ ! -d "$UPSTREAM/.git" ]; then
        echo "Cloning BasedHardware/omi (omi/firmware only) at $TAG_COMMIT ..."
        mkdir -p "$UPSTREAM"
        git -C "$UPSTREAM" init -q
        git -C "$UPSTREAM" remote add origin https://github.com/BasedHardware/omi.git
        git -C "$UPSTREAM" sparse-checkout init --cone
        git -C "$UPSTREAM" sparse-checkout set omi/firmware
        git -C "$UPSTREAM" fetch -q --depth 1 origin "$TAG_COMMIT"
        git -C "$UPSTREAM" checkout -q FETCH_HEAD
    fi
}

ensure_reference() {   # the shipped release: check.sh compares against it, and it is what to flash to go back to stock
    [ -s "$REFERENCE/ota/omi.signed.bin" ] && [ -s "$REFERENCE/Omi_CV1_v3.0.21_CPUNET.hex" ] && return
    echo "Downloading the shipped Omi_CV1_v3.0.21 release into $REFERENCE ..."
    mkdir -p "$REFERENCE/ota"
    local asset
    for asset in Omi_CV1_OTA_v3.0.21.zip Omi_CV1_v3.0.21.hex Omi_CV1_v3.0.21_CPUNET.hex; do
        curl -fsSL -o "$REFERENCE/$asset" "$RELEASE_URL/$asset"
    done
    unzip -q -o "$REFERENCE/Omi_CV1_OTA_v3.0.21.zip" -d "$REFERENCE/ota"
}

run_container() {
    local stage="$1" out="${2:-$HERE/out/tmp}"
    mkdir -p "$out"
    docker run --rm \
        -v "$UPSTREAM/omi/firmware:/omi/firmware" \
        -v "$HERE/scripts:/cw:ro" \
        -v "$out:/out" \
        -e CMAKE_PREFIX_PATH=/opt/toolchains \
        "$IMAGE" bash /cw/container-build.sh "$stage"
}

reset_tree() {   # back to the pristine tag (keeps the untracked v2.9.0 workspace and prj.conf)
    git -C "$UPSTREAM" checkout -q -- omi/firmware/omi
}

cmd="${1:-}"; shift || true
case "$cmd" in
    prepare)
        ensure_upstream
        ensure_reference
        run_container prepare
        ;;
    build)
        variant="${1:-}"
        [ -n "$variant" ] || usage
        ensure_upstream
        reset_tree
        if [ "$variant" != stock ]; then
            PATCH="$(patch_for "$variant")"
            git -C "$UPSTREAM" apply --check "$PATCH"
            git -C "$UPSTREAM" apply "$PATCH"
        fi
        run_container build "$HERE/out/$variant"
        git -C "$UPSTREAM" diff --stat
        echo "artifacts in $HERE/out/$variant"
        ;;
    diff)   # regenerate a variant's patch from whatever is edited in upstream/ (cumulative against the tag)
        variant="${1:-}"
        [ -n "$variant" ] && [ "$variant" != stock ] || usage
        PATCH="$(patch_for "$variant")"
        git -C "$UPSTREAM" diff -- omi/firmware/omi > "$PATCH"
        echo "wrote $PATCH ($(wc -l < "$PATCH") lines)"
        ;;
    reset)  reset_tree ;;
    *) usage ;;
esac
