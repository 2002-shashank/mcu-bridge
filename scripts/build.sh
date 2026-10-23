#!/usr/bin/env bash
# Build the mcu-bridge image. Runs on a Linux host (Lima VM locally, Ubuntu in CI).
#
#   scripts/build.sh            fetch pinned Buildroot if needed, configure, build
#   scripts/build.sh <target>   pass a make target through (menuconfig, linux-rebuild, ...)
#
# Overridable: WORK (build root, must be on a case-sensitive Linux fs), BR2_DL_DIR.
set -euo pipefail

BR_VERSION=2026.02.3
BR_SHA256=5a59e7501b0b4ec52c41f4bfa79412320e0b37eae5f719605a258e8d0c6fc7fb
DEFCONFIG=mcu_bridge_qemu_aarch64_defconfig

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/build}"
BR_DIR="$WORK/buildroot-$BR_VERSION"
OUT="$WORK/qemu-aarch64"
export BR2_DL_DIR="${BR2_DL_DIR:-$HOME/br-dl}"

mkdir -p "$WORK" "$BR2_DL_DIR"

if [ ! -d "$BR_DIR" ]; then
    tarball="$BR2_DL_DIR/buildroot-$BR_VERSION.tar.xz"
    [ -f "$tarball" ] || wget -q -O "$tarball" "https://buildroot.org/downloads/buildroot-$BR_VERSION.tar.xz"
    echo "$BR_SHA256  $tarball" | sha256sum -c --quiet
    tar -xf "$tarball" -C "$WORK"
fi

br_make() { make -C "$BR_DIR" O="$OUT" BR2_EXTERNAL="$REPO/br2-external" "$@"; }

if [ $# -gt 0 ]; then
    br_make "$@"
    exit
fi

# Re-apply the defconfig every time so the committed file is the single source of truth.
br_make "$DEFCONFIG"
br_make

mkdir -p "$REPO/out"
cp "$OUT/images/Image" "$OUT/images/rootfs.ext2" "$REPO/out/"
echo "images copied to $REPO/out"
