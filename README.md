# mcu-bridge

[![CI](https://github.com/2002-shashank/mcu-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/2002-shashank/mcu-bridge/actions/workflows/ci.yml)

An embedded Linux system end to end: a Buildroot ARM64 image running in QEMU,
a serdev kernel driver for an ESP32 on a UART, a CRC-32 framed protocol shared
by the kernel and the firmware, Zephyr firmware on the ESP32, a boot-started
user-space daemon, and CI that boots the image against a simulated ESP32 on
every push.

```
 QEMU virt (aarch64, Linux 6.18.7)                         macOS host          ESP32 (Zephyr 4.4.2)
┌──────────────────────────────────────┐
│ mcbd  ── poll() on sysfs ──┐         │
│                            │         │
│ mcb_bridge (serdev driver) │         │  Unix     esp32-bridge.py   USB      Sunton ESP32-3248S035R
│   COBS + CRC-32 framing,   │         │  socket   (reopens the      CH340    XPT2046 touch over SPI
│   counters, PING/SET_RATE  │         │◄────────► port on replug) ◄──────► DATA at 1..200 Hz,
│ PL011 ttyAMA1 (0x9040000) ─┘         │                               UART  PING/PONG, SET_RATE
└──────────────────────────────────────┘           or esp32-sim.py (CI: simulated ESP32 + fault injection)
```

## What is in it

| Part | Where | What |
|---|---|---|
| Image | `br2-external/`, `scripts/build.sh` | Buildroot 2026.02.3, Linux 6.18.7, Arm GNU Toolchain 14.2.rel1; every download hash-checked. Boots to a login prompt in ~1.1 s under TCG; 12.9 MiB kernel, 12.5 MiB rootfs used. A lockdep/KASAN variant (`VARIANT=debug`). |
| Device tree | `dts/` | An overlay adds the ESP32 node to QEMU's runtime DTB on the second PL011; a YAML binding validated with `dt_binding_check` and `dt-validate`. |
| Protocol | `protocol/`, [docs/protocol.md](docs/protocol.md) | COBS framing with a `0x00` delimiter, 12-byte header (seq, timestamp, per-boot `boot_id`), CRC-32. One header-only C implementation for kernel and Zephyr, a Python mirror, and a conformance test between them under ASan/UBSan. |
| Driver | `driver/mcb_bridge.c` | serdev driver bound from DT: counters (`crc_errors`, `dropped_frames`, `resets`, ...), an atomic `sample` attribute with its `CLOCK_MONOTONIC` receive time, `link_up` notified on both edges, PING/SET_RATE over sysfs. Clean under W=1, sparse and checkpatch `--strict`. |
| Firmware | `firmware/` | Zephyr app: interrupt-driven UART with ring buffers, touch samples on absolute deadlines, commands answered by their own thread. |
| Daemon | `daemon/mcbd.c`, [docs/daemon.md](docs/daemon.md) | One thread, one `poll()` over sysfs and a `signalfd`. Logs every sample and the link state, decides when a sample is stale, and survives module reloads and a missing device. |
| Host tools | `scripts/` | `esp32-bridge.py` (USB serial ↔ QEMU, survives unplugs), `esp32-sim.py` (simulated ESP32 with fault injection), `measure-boot.py`, `measure-ping.py`. |
| CI | `.github/workflows/ci.yml`, `scripts/ci.sh` | Host tests, release and debug images each booted against the simulator, firmware build. Every stage also runs locally. |

## Results

**Ping round trip, real board: 8.0 ms median** ([docs/latency.md](docs/latency.md)).
Measured at three points (guest driver, bridge tty, host ↔ board directly):
4.95 ms UART wire time, ~3 ms USB through the CH340, 0.5 ms guest + QEMU.
That measurement found 1.9 ms spent waiting for the ESP32 UART's RX idle
timeout (the RX FIFO threshold was 22 bytes, a PING is 27). Setting the
threshold to 1 brought the median from 9.86 to 7.98 ms. Under hvf the guest
is slower than under TCG for this, because waking an idle vCPU costs
0.6–1.4 ms.

**Faults** ([docs/faults.md](docs/faults.md)): corrupted frames, wire loss,
line noise, ESP32 reset, cable unplugs shorter and longer than the link
timeout, module reload while streaming, and a missing DT node. Each one is
checked at every layer at once: exact counter deltas, link state, the daemon's
events in order, the longest gap in the data the consumer received, and
recovery. Writing this suite found that the driver counted a framing error
on every module load, because it opened the port mid-frame. It now resyncs
on the first delimiter.

**Reboot detection without HELLO.** After a USB replug the ESP32 boots while
the host is still enumerating the serial port, so its HELLO frame is always
lost. Every frame therefore carries a random per-boot `boot_id`. Sequence
tracking alone would have counted the reboot as thousands of dropped frames.

## Running it

The image builds on Linux. On a Mac this uses a Lima VM; QEMU runs natively
on macOS so the guest can reach the board's USB serial port.

```sh
scripts/build.sh                      # Buildroot image -> out/Image, out/rootfs.ext2
VARIANT=debug scripts/build.sh        # lockdep + KASAN kernel -> out/debug

scripts/run-qemu.sh                   # boot; the ESP32 UART is the socket out/esp32.sock
scripts/esp32-sim.py                  #   ... with a simulated ESP32 (stdin: corrupt 5, unplug 3, reset, ...)
scripts/esp32-bridge.py               #   ... or the real board on /dev/cu.usbserial-110

scripts/fw.sh build && scripts/fw.sh flash    # Zephyr firmware (ZEPHYR_WS, ZEPHYR_SDK)
```

In the guest:

```sh
cat /sys/bus/serial/devices/serial0-0/sample   # boot_id seq ts_us rx_ns count touched x y pressure
echo 1 > /sys/bus/serial/devices/serial0-0/ping && cat /sys/bus/serial/devices/serial0-0/ping_rtt_us
tail -f /var/log/messages                      # mcbd: link up/down, stale, peer reboot, counters
```

## Tests

| Test | Runs on | Covers |
|---|---|---|
| `tests/proto/test_proto.py` | host | COBS vectors, every single-bit flip, overrun, seq wraparound; C vs Python conformance |
| `tests/test_sim.py`, `tests/test_bridge.py` | host | simulator protocol; bridge replug and stall handling (ptys) |
| `tests/test_dt_node.py` | QEMU | overlay hands ttyAMA1 to serdev; without it the port stays a tty |
| `tests/test_link.py` | QEMU | raw link: HELLO, gap-free DATA, PING/PONG, reconnect |
| `tests/test_driver.py [--real]` | QEMU | binding, data, counters, PING, SET_RATE; `--real` end to end with the board |
| `tests/test_daemon.py` | QEMU | CSV, exact sample accounting, rotation, shutdown |
| `tests/test_faults.py [--real [--unplug]]` | QEMU | the fault matrix above |
| `tests/test_firmware.py` | board | firmware alone over USB serial |

`scripts/ci.sh <stage>` runs the CI stages (`host`, `build`, `checks`, `qemu`,
`firmware`) the same way locally as on GitHub Actions. CI builds QEMU 11.0.2
from source: Ubuntu 24.04's QEMU 8.2 creates no second PL011 on the virt
machine, so the ESP32's UART would not exist in the guest.

## Licenses

The C, device tree and firmware sources carry SPDX identifiers: kernel driver
and daemon GPL-2.0-only; firmware Apache-2.0; the DT binding GPL-2.0-only OR
BSD-2-Clause; the shared protocol header GPL-2.0-only OR MIT. There is no
top-level license file yet, and the scripts and tests carry none.
