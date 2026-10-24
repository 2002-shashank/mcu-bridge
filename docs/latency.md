# Ping round-trip latency

`scripts/measure-ping.py` times a PING (8-byte token) and its PONG at three
points of the path:

```
guest driver ── QEMU PL011 ── socket ── esp32-bridge.py ── /dev/cu.usbserial ── CH340 ── UART ── ESP32
   │ ping_rtt_us (whole path)                    │ --trace (tty write → tty read)        │
   └─────────────────────────────────────────────┴── direct: host ↔ board, no QEMU ──────┘
```

- **guest**: the driver's `ping_rtt_us`, from just before `serdev_device_write()`
  to the PONG being parsed in `receive_buf()`.
- **bridge trace**: `esp32-bridge.py --trace` decodes both directions and times
  each PING from its last byte written to the tty to the PONG's last byte read.
  The guest figure minus this is the guest + QEMU + bridge overhead.
- **direct**: host ↔ board over the USB serial port with no QEMU
  (`scripts/esp32_board.py`), with and without a token, at 1 Hz and 50 Hz DATA.
- **sim**: the guest against `esp32-sim.py`, which answers instantly: the
  virtual part of the path alone.

Pings are 300 per configuration, spaced by a random 0–20 ms so they do not lock
to the DATA stream's phase. Apple M-series host, macOS, CYD board with a CH340
USB serial chip, 115200 8N1.

## Result: where the ~10 ms went, and 1.9 ms of it removed

Direct round trip, 1 Hz DATA, 8-byte token, median:

| Part | Before | After | How it is known |
|---|---|---|---|
| UART wire time, 27 + 30 bytes at 86.8 µs/byte | 4.95 ms | 4.95 ms | computed; matches the measured slope (below) |
| ESP32 RX idle timeout | **1.9 ms** | ~0 | removed by the fix; the median dropped by exactly this |
| USB (CH340, full-speed frames, macOS driver), both directions, + firmware | ~3.0 ms | ~3.0 ms | remainder |
| **Total, measured** | **9.86 ms** | **7.98 ms** | |
| guest + QEMU + bridge (TCG), added on top | 0.5 ms | 0.5 ms | guest RTT minus bridge trace |

**The ESP32 RX timeout.** Zephyr's ESP32 UART driver raises the RX interrupt
when the FIFO holds 22 bytes (`CONFIG_UART_ESP32_RX_FIFO_THRESH=0x16`), or
when the line has been idle for 22 byte times (`uart_hal_set_rx_timeout(0x16)`,
~1.9 ms at 115200). A PING is 27 bytes on the wire: the first 22 raise the
interrupt, and the last 5 wait in the FIFO for the idle timeout. So every
command reached the firmware ~1.9 ms after its last byte. Setting the
threshold to 1 (`firmware/prj.conf`) raises the interrupt on every byte; at
115200 that is at most 11.5k interrupts/s, which the ESP32 handles easily.
The prediction was 1.91 ms; measured: −1.88 ms (8-byte token) and −1.92 ms
(no token).

**Wire time.** Without a token the PING is 8 bytes shorter and the PONG too:
the median drops from 7.98 to 6.45 ms, 95.6 µs per byte against 86.8 µs for
10 bits at 115200. The UART is the largest single part and is fixed by the baud
rate: at 921600 it would be 0.6 ms.

**USB.** The remaining ~3 ms is the CH340 and the host's USB stack, in both
directions: full-speed USB moves data in 1 ms frames, the CH340 batches UART
bytes into IN packets, and the macOS driver completes the read. Splitting this
by direction would need a clock shared by the host and the ESP32. Since the
guest path adds only 0.5 ms, this is the part that dominates what remains.

## All measurements (after the fix)

| path | n | min | p50 | p90 | p99 | max | lost |
|---|---|---|---|---|---|---|---|
| direct, 1 Hz, 0 B token | 300 | 5.45 | 6.45 | 6.62 | 7.09 | 8.62 | 0 |
| direct, 1 Hz, 8 B token | 300 | 7.45 | 7.98 | 8.22 | 8.83 | 9.62 | 0 |
| direct, 50 Hz, 8 B token | 300 | 5.85 | 7.86 | 8.36 | 9.96 | 10.31 | 0 |
| guest sim, tcg | 300 | 0.19 | 0.29 | 0.49 | 0.66 | 0.78 | 0 |
| guest real, tcg, 1 Hz | 300 | 6.25 | 8.10 | 8.30 | 8.54 | 9.79 | 0 |
| ↳ bridge tty trace of the same pings | 300 | 5.74 | 7.60 | 7.70 | 7.78 | 9.28 | 0 |
| guest real, tcg, 50 Hz | 300 | 6.08 | 8.03 | 8.70 | 10.04 | 10.14 | 0 |
| ↳ bridge tty trace of the same pings | 300 | 5.64 | 7.58 | 8.23 | 9.55 | 9.75 | 0 |
| guest sim, hvf | 300 | 0.70 | 1.67 | 3.26 | 3.61 | 3.69 | 0 |
| guest real, hvf, 1 Hz | 300 | 7.92 | 9.03 | 9.80 | 13.89 | 29.88 | 0 |
| ↳ bridge tty trace of the same pings | 300 | 6.51 | 7.75 | 8.07 | 9.74 | 28.49 | 0 |
| guest real, hvf, 50 Hz | 300 | 6.98 | 9.48 | 10.44 | 13.10 | 15.45 | 0 |
| ↳ bridge tty trace of the same pings | 300 | 5.58 | 7.63 | 8.39 | 9.62 | 12.48 | 0 |

(milliseconds; before the fix, direct 1 Hz 8 B was 9.44 / 9.86 / 10.01 / 10.28 / 10.95)

**50 Hz DATA.** The tail grows (direct p99 8.83 → 9.96 ms): a PONG queued
behind a DATA frame being transmitted waits up to 2.9 ms (34 bytes). The best
case is also faster at 50 Hz (min 5.85 vs 7.45 ms). That is unexplained so far;
it may be the CH340 flushing its IN buffer sooner while bytes are streaming.

## TCG vs hvf

hvf does not make the guest faster here; it makes it slower and less
predictable. The virtual path, simulator, three runs each (p50 / p99, ms):

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| tcg | 0.30 / 0.71 | 0.30 / 0.61 | 0.30 / 0.69 |
| hvf | 1.70 / 3.69 | 0.90 / 1.88 | 1.69 / 3.56 |
| hvf, vCPUs kept busy (`--busy`) | 0.30 / 0.61 | 0.30 / 0.59 | |

With every vCPU spinning, hvf matches TCG exactly, so the extra 0.6–1.4 ms is
waking an idle vCPU: under hvf a guest WFI exits to the hypervisor and the vCPU
thread sleeps in the host until QEMU injects the UART interrupt and kicks it.
Under TCG an idle vCPU waits inside QEMU, which is cheaper to wake. (Busy loops
under TCG lower the median but push p99 to 3.2 ms: they compete with the
emulator for host CPU.) hvf still boots and computes faster; it is the wake-up
latency of an idle guest that is worse.

## Reproducing

```
scripts/measure-ping.py matrix -n 300                  # board on /dev/cu.usbserial-110
scripts/measure-ping.py direct --rate 1 --token 0
scripts/measure-ping.py guest sim --accel hvf --busy
```
