#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build/flash the Zephyr firmware in a clean environment: ~/.zprofile sources
# ESP-IDF, which conflicts with Zephyr, so nothing from the login shell leaks in.
#
#   scripts/fw.sh build      build into out/fw
#   scripts/fw.sh flash      flash at 115200 (ESP32_TTY, default /dev/cu.usbserial-110)
#   scripts/fw.sh pristine   clean rebuild
#
#   ZEPHYR_WS (default ~/zephyrproject), ZEPHYR_SDK (default ~/zephyr-sdk-1.0.1),
#   FW_BUILD (build dir, default out/fw)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WS="${ZEPHYR_WS:-$HOME/zephyrproject}"
SDK="${ZEPHYR_SDK:-$HOME/zephyr-sdk-1.0.1}"
TTY="${ESP32_TTY:-/dev/cu.usbserial-110}"
BOARD=esp32_devkitc/esp32/procpu
BUILD="${FW_BUILD:-$REPO/out/fw}"

clean_env() {
    env -i HOME="$HOME" TERM="${TERM:-dumb}" \
        PATH="$WS/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin" \
        ZEPHYR_SDK_INSTALL_DIR="$SDK" \
        bash -c 'cd "$1" && shift && "$@"' _ "$WS" "$@"
}

case "${1:-build}" in
    build)    clean_env west build -b "$BOARD" -d "$BUILD" "$REPO/firmware" ;;
    pristine) clean_env west build -p always -b "$BOARD" -d "$BUILD" "$REPO/firmware" ;;
    flash)    clean_env west flash -d "$BUILD" --esp-device "$TTY" --esp-baud-rate 115200 ;;
    *)        echo "usage: $0 build|pristine|flash" >&2; exit 2 ;;
esac
