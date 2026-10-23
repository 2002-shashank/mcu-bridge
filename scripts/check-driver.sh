#!/usr/bin/env bash
# Static checks for the driver on a Linux host (VM or CI), against the
# Buildroot-built kernel: W=1 warnings, sparse (C=1) and checkpatch --strict.
# Builds from a scratch copy so no objects land in the repo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/build}"
OUT="$WORK/qemu-aarch64"
KSRC="${KSRC:-$(ls -d "$OUT"/build/linux-[0-9]* | head -1)}"
CROSS="$(ls "$OUT"/host/bin/*-gcc | grep -v -- '-cc' | head -1)"
CROSS="${CROSS%gcc}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cp "$REPO"/driver/*.c "$REPO"/driver/Kbuild "$tmp/"

echo "== build: W=1 C=1 (sparse)"
make -s -C "$KSRC" M="$tmp" ARCH=arm64 CROSS_COMPILE="$CROSS" \
    MCB_PROTO_DIR="$REPO/protocol" W=1 C=1 modules 2>&1 | tee "$tmp/build.log"
if grep -qE "warning|error" "$tmp/build.log"; then
    echo "FAIL: compiler/sparse diagnostics above"
    exit 1
fi

echo "== checkpatch --strict"
cp="$KSRC/scripts/checkpatch.pl --strict --no-tree --terse --show-types -f"
# DEVICE_ATTR helper macros: 'name' is only token-pasted and 'field' is a
# member name, so these two checks are false positives here.
$cp --ignore MACRO_ARG_REUSE,MACRO_ARG_PRECEDENCE "$REPO"/driver/mcb_bridge.c
# The protocol header is shared with Zephyr and host C, which have no u8/u32.
$cp --ignore PREFER_KERNEL_TYPES "$REPO"/protocol/mcb_proto.h
echo "OK"
