#!/usr/bin/env bash
# Boot the image in QEMU (native on macOS so the guest can reach the USB serial port).
#
#   scripts/run-qemu.sh                                  ESP32 UART (guest ttyAMA1) unconnected
#   ESP32=/dev/cu.usbserial-110 scripts/run-qemu.sh      ESP32 UART on a host tty
#
#   ACCEL=hvf        Apple Hypervisor.framework (default tcg = software emulation, same as CI)
#   SNAPSHOT=off     persist rootfs writes (default on: every boot starts clean)
#   IMAGES=dir       where Image and rootfs.ext2 are (default out/)
#   DT_OVERLAY=file  overlay applied to QEMU's DTB (default dts/esp32-bridge.dtso);
#                    "none" boots QEMU's stock DTB, i.e. no ESP32 node
#
# Console is on stdio; Ctrl-A x quits, Ctrl-A c toggles the QEMU monitor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES="${IMAGES:-$REPO/out}"
ACCEL="${ACCEL:-tcg}"
DT_OVERLAY="${DT_OVERLAY:-$REPO/dts/esp32-bridge.dtso}"

if [ "$ACCEL" = hvf ]; then cpu=host; else cpu=cortex-a53; fi

# Everything that shapes the generated DTB. The second serial port must exist
# for QEMU to create the second PL011 (0x9040000, guest ttyAMA1).
machine=(-accel "$ACCEL" -cpu "$cpu" -smp 2 -m 256 -display none)

if [ -n "${ESP32:-}" ]; then
    esp=(-chardev "serial,id=esp,path=$ESP32" -serial chardev:esp)
else
    esp=(-serial null)
fi

dtb=()
if [ "$DT_OVERLAY" != none ]; then
    # QEMU builds the virt DTB at runtime, so dump it with the same machine
    # options (null serials: don't open the real ESP32 port), then merge.
    dtdir="$IMAGES/dtb"
    mkdir -p "$dtdir"
    qemu-system-aarch64 -M virt,dumpdtb="$dtdir/virt-base.dtb" "${machine[@]}" \
        -serial null -serial null >/dev/null
    dtc -q -I dts -O dtb -o "$dtdir/overlay.dtbo" "$DT_OVERLAY"
    fdtoverlay -i "$dtdir/virt-base.dtb" -o "$dtdir/virt.dtb" "$dtdir/overlay.dtbo"
    dtb=(-dtb "$dtdir/virt.dtb")
fi

exec qemu-system-aarch64 \
    -M virt "${machine[@]}" ${dtb[@]+"${dtb[@]}"} \
    -kernel "$IMAGES/Image" \
    -append "rootwait root=/dev/vda console=ttyAMA0" \
    -drive "file=$IMAGES/rootfs.ext2,if=none,format=raw,id=hd0,snapshot=${SNAPSHOT:-on}" \
    -device virtio-blk-device,drive=hd0 \
    -serial mon:stdio "${esp[@]}"
