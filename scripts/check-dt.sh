#!/usr/bin/env bash
# Validate the DT binding and the merged DTB with dtschema. Runs on a Linux host (VM or CI).
#
#   scripts/check-dt.sh [DTB]     DTB defaults to out/dtb/virt.dtb (written by run-qemu.sh)
#
# Needs dtschema (venv at DTSCHEMA_VENV, default ~/dtschema-venv), flex, bison, yamllint,
# and the kernel source tree from the Buildroot build (KSRC).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${WORK:-$HOME/build}"
KSRC="${KSRC:-$(ls -d "$WORK"/qemu-aarch64/build/linux-[0-9]* | head -1)}"
export PATH="${DTSCHEMA_VENV:-$HOME/dtschema-venv}/bin:$PATH"
DTB="${1:-$REPO/out/dtb/virt.dtb}"
BINDING=mcubridge,esp32.yaml

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp" "$KSRC/Documentation/devicetree/bindings/$BINDING"' EXIT

echo "== binding: make dt_binding_check"
cp "$REPO/dts/bindings/$BINDING" "$KSRC/Documentation/devicetree/bindings/"
make -s -C "$KSRC" O="$tmp/kbuild" ARCH=arm64 dt_binding_check DT_SCHEMA_FILES="$BINDING"

echo "== DTB: dt-validate, ESP32 UART subtree"
# QEMU names its UARTs pl011@..., which fails serial.yaml's node-name rule. A failed
# subschema contributes no evaluated properties, so that also hides the 'mcu' child
# serial.yaml allows. Rename to the upstream form so only problems in our node remain.
dtc -q -I dtb -O dts "$DTB" | sed 's/pl011@9040000 {/serial@9040000 {/' |
    dtc -q -I dts -O dtb -o "$tmp/virt.dtb" -
dt-validate -s "$tmp/kbuild/Documentation/devicetree/bindings/processed-schema.json" \
    "$tmp/virt.dtb" 2>&1 | grep -A1 -E '9040000|mcu' > "$tmp/errors" || true
if [ -s "$tmp/errors" ]; then
    cat "$tmp/errors"
    exit 1
fi
echo "OK"
