#!/usr/bin/env bash
# Build the mcu-bridge image. Runs on a Linux host (Lima VM locally, Ubuntu in CI).
#
#   scripts/build.sh            fetch pinned Buildroot if needed, configure, build
#   scripts/build.sh <target>   pass a make target through (menuconfig, linux-rebuild, ...)
#
#   VARIANT=debug scripts/build.sh   lockdep/KASAN kernel, own output dir, images to out/debug
#   CCACHE=1 scripts/build.sh        compiler cache in ~/.buildroot-ccache (CI; off locally so
#                                    the committed defconfig stays what is built)
#
# Overridable: WORK (build root, must be on a case-sensitive Linux fs), BR2_DL_DIR,
# IMAGES_OUT (where Image and rootfs.ext2 are copied).
set -euo pipefail

BR_VERSION=2026.02.3
BR_SHA256=5a59e7501b0b4ec52c41f4bfa79412320e0b37eae5f719605a258e8d0c6fc7fb
DEFCONFIG=mcu_bridge_qemu_aarch64_defconfig

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/build}"
BR_DIR="$WORK/buildroot-$BR_VERSION"
VARIANT="${VARIANT:-}"
OUT="$WORK/qemu-aarch64${VARIANT:+-$VARIANT}"
IMAGES_OUT="${IMAGES_OUT:-$REPO/out${VARIANT:+/$VARIANT}}"
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
if [ "$VARIANT" = debug ]; then
    frag='$(BR2_EXTERNAL_MCU_BRIDGE_PATH)/board/qemu-aarch64'
    sed -i "s|^BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=.*|BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=\"$frag/linux.fragment $frag/linux-debug.fragment\"|" "$OUT/.config"
    br_make olddefconfig
fi
if [ "${CCACHE:-0}" = 1 ]; then
    printf '%s\n' 'BR2_CCACHE=y' 'BR2_CCACHE_INITIAL_SETUP="--max-size=2G"' >> "$OUT/.config"
    br_make olddefconfig
fi
br_make

# Kconfig silently drops symbols whose dependencies are unmet: check that every
# option our fragments request made it into the kernel's final .config.
kconfig="$(ls -d "$OUT"/build/linux-[0-9]*)/.config"
frags=("$REPO/br2-external/board/qemu-aarch64/linux.fragment")
[ "$VARIANT" = debug ] && frags+=("$REPO/br2-external/board/qemu-aarch64/linux-debug.fragment")
missing=$(grep -hE '^CONFIG_[A-Z0-9_]+=' "${frags[@]}" | while read -r want; do
    grep -qxF "$want" "$kconfig" || echo "  $want"
done)
if [ -n "$missing" ]; then
    echo "ERROR: kernel config fragment options not in $kconfig (unmet dependencies?):"
    echo "$missing"
    exit 1
fi

mkdir -p "$IMAGES_OUT"
cp "$OUT/images/Image" "$OUT/images/rootfs.ext2" "$IMAGES_OUT/"
echo "images copied to $IMAGES_OUT"
