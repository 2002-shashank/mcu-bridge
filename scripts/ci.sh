#!/usr/bin/env bash
# CI stages on a Linux host. .github/workflows/ci.yml only calls this script, so
# every stage runs the same way in the Lima VM.
#
#   scripts/ci.sh deps       apt packages (sudo) + dtschema venv
#   scripts/ci.sh qemu-bin   build QEMU $QEMU_VERSION into QEMU_PREFIX unless already there:
#                            the Mac's version, and Ubuntu's 8.2 has no second virt PL011
#   scripts/ci.sh host       tests that need no image: protocol (C under ASan/UBSan),
#                            simulator, bridge, mcbd built with -Werror and run under ASan
#   scripts/ci.sh build      scripts/build.sh (honours VARIANT, CCACHE)
#   scripts/ci.sh checks     driver static checks (W=1, sparse, checkpatch)
#   scripts/ci.sh qemu       boot tests against the simulator, DT validation, boot time
#                            (VARIANT=debug: driver and daemon tests only)
#   scripts/ci.sh firmware   Zephyr workspace (shallow, hal_espressif only) + firmware build
#
# Overridable: WORK, BR2_DL_DIR, QEMU_PREFIX, IMAGES (images dir, default out[/debug]), LOGS (test logs,
# default $IMAGES/logs), ZEPHYR_WS, ZEPHYR_SDK, FW_BUILD. A summary goes to $GITHUB_STEP_SUMMARY if set.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VARIANT="${VARIANT:-}"
export IMAGES="${IMAGES:-$REPO/out${VARIANT:+/$VARIANT}}"
LOGS="${LOGS:-$IMAGES/logs}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
PY=/usr/bin/python3

QEMU_VERSION=11.0.2
QEMU_SHA256=3745f6ea88e2e87fe0dc838b2b1d4e0a770bf48e01a1d5a186842a1fff76ccf5  # = Homebrew's
QEMU_PREFIX="${QEMU_PREFIX:-$HOME/qemu-$QEMU_VERSION}"
export PATH="$QEMU_PREFIX/bin:$PATH"

ZEPHYR_VERSION=v4.4.2
ZEPHYR_SDK_VERSION=1.0.1
export ZEPHYR_WS="${ZEPHYR_WS:-$HOME/zephyrproject}"
export ZEPHYR_SDK="${ZEPHYR_SDK:-$HOME/zephyr-sdk-$ZEPHYR_SDK_VERSION}"

sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

# Run each named test, log it, keep going; fail at the end if any failed.
failed=()
run_tests() {
    mkdir -p "$LOGS"
    local t name rc
    for t in "$@"; do
        name="$(basename "${t%% *}" .py)"
        echo "::group::$name"
        set +e
        # shellcheck disable=SC2086 # test arguments
        $PY "$REPO"/$t 2>&1 | tee "$LOGS/$name.log"
        rc=${PIPESTATUS[0]}
        set -e
        echo "::endgroup::"
        if [ "$rc" -eq 0 ]; then
            echo "- ✅ \`$t\` $(grep -h '^RESULT' "$LOGS/$name.log" || echo ok)" >> "$SUMMARY"
        else
            echo "FAIL: $t (rc=$rc)"
            failed+=("$t")
            echo "- ❌ \`$t\` $(grep -h '^RESULT' "$LOGS/$name.log" || echo "rc=$rc")" >> "$SUMMARY"
            grep -h 'FAIL' "$LOGS/$name.log" | sed 's/^/  - /' >> "$SUMMARY" || true
        fi
    done
}

finish() {
    if [ ${#failed[@]} -gt 0 ]; then
        echo "FAILED: ${failed[*]}"
        exit 1
    fi
    echo "all passed"
}

stage_deps() {
    sudo_ apt-get update -q
    # Buildroot host tools; QEMU build deps and DT tools for the boot tests; sparse for
    # check-driver; flex/bison/yamllint for dt_binding_check; swig/python3-dev for pylibfdt
    # if no wheel; cmake so Buildroot's host-ccache need not build host-cmake.
    sudo_ apt-get install -y -q --no-install-recommends \
        build-essential bc cpio file rsync unzip wget ca-certificates libssl-dev cmake \
        pkg-config libglib2.0-dev libpixman-1-dev libfdt-dev libslirp-dev \
        device-tree-compiler sparse flex bison yamllint \
        python3-dev python3-venv swig ninja-build gperf xz-utils
    local venv="${DTSCHEMA_VENV:-$HOME/dtschema-venv}"
    [ -x "$venv/bin/dt-validate" ] || {
        $PY -m venv "$venv"
        "$venv/bin/pip" install -q dtschema
    }
}

stage_qemu_bin() {
    if [ -x "$QEMU_PREFIX/bin/qemu-system-aarch64" ]; then
        qemu-system-aarch64 --version | head -1
        return
    fi
    local tmp tarball
    tmp="$(mktemp -d)"
    tarball="$tmp/qemu-$QEMU_VERSION.tar.xz"
    wget -q -O "$tarball" "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz"
    echo "$QEMU_SHA256  $tarball" | sha256sum -c --quiet
    tar -xf "$tarball" -C "$tmp"
    (
        cd "$tmp/qemu-$QEMU_VERSION"
        # system emulator only; slirp so the virt board's default NIC matches the Mac
        ./configure --prefix="$QEMU_PREFIX" --target-list=aarch64-softmmu \
            --enable-slirp --enable-fdt=system --disable-docs --disable-user --disable-tools
        make -j"$(nproc)" install
    )
    rm -rf "$tmp"
    qemu-system-aarch64 --version | head -1
}

stage_host() {
    echo "## host tests" >> "$SUMMARY"
    run_tests tests/proto/test_proto.py tests/test_sim.py tests/test_bridge.py

    echo "::group::mcbd (-Werror, ASan/UBSan smoke run)"
    local tmp rc=0
    tmp="$(mktemp -d)"
    make -s -C "$REPO/daemon" -B CC=gcc CFLAGS="-O1 -g -fsanitize=address,undefined" \
        mcbd >/dev/null
    mv "$REPO/daemon/mcbd" "$tmp/"
    # No driver on the host: mcbd must start, keep searching, and exit cleanly on SIGTERM.
    "$tmp/mcbd" -e -o "$tmp/s.csv" -s 1 2> "$tmp/log" &
    sleep 2.5
    kill -TERM $!
    wait $! || rc=$?
    cat "$tmp/log"
    echo "::endgroup::"
    if [ "$rc" -eq 0 ] && grep -q "exiting on signal 15" "$tmp/log" && ! grep -q "ERROR: " "$tmp/log"; then
        echo "- ✅ mcbd: -Werror build, ASan/UBSan clean, clean SIGTERM exit" >> "$SUMMARY"
    else
        failed+=("mcbd smoke (rc=$rc)")
        echo "- ❌ mcbd smoke run (rc=$rc)" >> "$SUMMARY"
    fi
    rm -rf "$tmp"
    finish
}

stage_build() {
    IMAGES_OUT="$IMAGES" "$REPO/scripts/build.sh"
}

stage_checks() {
    "$REPO/scripts/check-driver.sh"
    echo "- ✅ check-driver.sh (W=1, sparse, checkpatch --strict)" >> "$SUMMARY"
}

stage_qemu() {
    echo "## QEMU tests${VARIANT:+ ($VARIANT kernel)}" >> "$SUMMARY"
    if [ "$VARIANT" = debug ]; then
        run_tests tests/test_driver.py tests/test_daemon.py
        finish
        return
    fi
    run_tests tests/test_dt_node.py tests/test_link.py tests/test_driver.py tests/test_daemon.py

    # run-qemu.sh wrote the merged DTB during the boots above
    if "$REPO/scripts/check-dt.sh" "$IMAGES/dtb/virt.dtb" 2>&1 | tee "$LOGS/check-dt.log"; then
        echo "- ✅ check-dt.sh (dt_binding_check + dt-validate)" >> "$SUMMARY"
    else
        failed+=(check-dt.sh)
        echo "- ❌ check-dt.sh" >> "$SUMMARY"
    fi

    echo "::group::boot time and image size"
    if $PY "$REPO/scripts/measure-boot.py" -n 3 2>&1 | tee "$LOGS/measure-boot.log"; then
        { echo '```'; sed -n '/^boot time/,$p' "$LOGS/measure-boot.log"; echo '```'; } >> "$SUMMARY"
    else
        failed+=(measure-boot.py)
    fi
    echo "::endgroup::"
    finish
}

stage_firmware() {
    local venv="$ZEPHYR_WS/.venv"
    if [ ! -d "$ZEPHYR_WS/.west" ]; then
        mkdir -p "$ZEPHYR_WS"
        $PY -m venv "$venv"
        "$venv/bin/pip" install -q west
        "$venv/bin/west" init -m https://github.com/zephyrproject-rtos/zephyr \
            --mr "$ZEPHYR_VERSION" -o=--depth=1 "$ZEPHYR_WS"
    fi
    (
        cd "$ZEPHYR_WS"
        export PATH="$venv/bin:$PATH"
        # only the ESP32 HAL; everything else in the manifest is for other SoCs
        west config manifest.project-filter -- '-.*,+hal_espressif'
        west update --narrow -o=--depth=1
        pip install -q -r zephyr/scripts/requirements-base.txt   # also needed by west blobs
        west packages pip --install -- -q                        # module packages (esptool)
        west blobs fetch hal_espressif
        [ -d "$ZEPHYR_SDK/gnu/xtensa-espressif_esp32_zephyr-elf" ] ||
            west sdk install --version "$ZEPHYR_SDK_VERSION" --install-dir "$ZEPHYR_SDK" \
                --toolchains xtensa-espressif_esp32_zephyr-elf
    )
    "$REPO/scripts/fw.sh" pristine
    local size
    size="$(stat -c %s "${FW_BUILD:-$REPO/out/fw}/zephyr/zephyr.bin")"
    echo "- ✅ firmware: esp32_devkitc/esp32/procpu, zephyr.bin $size bytes" >> "$SUMMARY"
}

# bash reads a script as it runs it: with the dispatch in a function called on the last
# line, together with exit, the whole file is parsed first, so editing ci.sh during a
# long local run cannot break that run.
main() {
    case "${1:-}" in
        deps)     stage_deps ;;
        qemu-bin) stage_qemu_bin ;;
        host)     stage_host ;;
        build)    stage_build ;;
        checks)   stage_checks ;;
        qemu)     stage_qemu ;;
        firmware) stage_firmware ;;
        *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
}

main "$@"; exit
