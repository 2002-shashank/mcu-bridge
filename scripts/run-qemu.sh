#!/usr/bin/env bash
# Boot the image in QEMU (native on macOS so the guest can reach the USB serial port).
#
#   scripts/run-qemu.sh                                  ESP32 UART (guest ttyAMA1) unconnected
#   ESP32=/dev/cu.usbserial-110 scripts/run-qemu.sh      ESP32 UART on a host tty
#
#   ACCEL=hvf     Apple Hypervisor.framework (default tcg = software emulation, same as CI)
#   SNAPSHOT=off  persist rootfs writes (default on: every boot starts clean)
#   IMAGES=dir    where Image and rootfs.ext2 are (default out/)
#
# Console is on stdio; Ctrl-A x quits, Ctrl-A c toggles the QEMU monitor.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES="${IMAGES:-$REPO/out}"
ACCEL="${ACCEL:-tcg}"

if [ "$ACCEL" = hvf ]; then cpu=host; else cpu=cortex-a53; fi

# The second -serial always exists so the guest always has ttyAMA1 at 0x9040000.
if [ -n "${ESP32:-}" ]; then
    esp=(-chardev "serial,id=esp,path=$ESP32" -serial chardev:esp)
else
    esp=(-serial null)
fi

exec qemu-system-aarch64 \
    -M virt -accel "$ACCEL" -cpu "$cpu" -smp 2 -m 256 \
    -display none \
    -kernel "$IMAGES/Image" \
    -append "rootwait root=/dev/vda console=ttyAMA0" \
    -drive "file=$IMAGES/rootfs.ext2,if=none,format=raw,id=hd0,snapshot=${SNAPSHOT:-on}" \
    -device virtio-blk-device,drive=hd0 \
    -serial mon:stdio "${esp[@]}"
