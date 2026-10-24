# Fault tests

`tests/test_faults.py` injects one fault at a time into a running system
(guest kernel + `mcb_bridge` + `mcbd`) and checks every layer for that fault:

| Layer | Checked |
|---|---|
| driver | exact change of every counter: the expected ones, and zero for all others |
| link | whether `link_up` went down (its notification, as logged by mcbd) |
| daemon | mcbd events that must appear, in order, and those that must not |
| consumer | longest gap between samples the daemon received ("outage"), from its CSV in guest time |
| recovery | link up and samples fresh again before the next fault |

The outage bounds test the daemon's stale policy (a sample is stale after
`max(3 periods, 100 ms)`, so 100 ms at 50 Hz), not the speed of the host: a
fault that must not raise a stale event has to keep the gap under 100 ms, and
one that must raise it has to open a gap of at least 100 ms.

```
tests/test_faults.py                  simulated ESP32 (CI, release and debug kernel)
tests/test_faults.py --real           real board: reset pulse, module reload
tests/test_faults.py --real --unplug  ... plus a physical unplug (a person pulls the cable)
```

## Simulated ESP32 (50 Hz)

| fault | driver counters | link down | daemon events | outage (ms) | result |
|---|---|---|---|---|---|
| 1 corrupted frame | crc_errors +1, dropped_frames +1 | no | — | 36 | PASS |
| 5 corrupted frames in a row | crc_errors +5, dropped_frames +5 | no | stale, fresh | 120 | PASS |
| 7 frames lost on the wire | dropped_frames +7 | no | stale, fresh | 161 | PASS |
| line noise, 200 bytes | overruns +1, dropped_frames +1 | no | — | 40 | PASS |
| ESP32 reset | overruns +1, resets +1 | no | peer reboot | 25 | PASS |
| cable unplug 1 s (< 3 s link timeout) | resets +1 | no | stale, peer reboot, fresh | 1019 | PASS |
| cable unplug 5 s | resets +1 | yes | stale, link down, link up, peer reboot, fresh | 5022 | PASS |
| module unload + reload while streaming | reset to 0 | no | device gone, device found | 2001 | PASS |
| missing DT node (boot without overlay) | no device | — | none (searching) | — | PASS |

What each row shows:

- **Corruption** is counted twice, on purpose: the bad frame is a CRC error,
  and the sequence gap it leaves is a dropped frame. One bad frame (a 40 ms gap)
  stays under the stale limit; five in a row (120 ms) cross it, and the daemon
  reports the stall and the recovery.
- **Wire loss** shows up only as a sequence gap (`dropped_frames`).
- **Line noise** with no delimiter in it overruns the receiver once and costs
  the frame it ran into.
- **ESP32 reset**: the ROM boot text is one overrun (the firmware sends a 0x00
  before HELLO to end it), and the reboot is a reset. With the simulator the
  stream resumes in 25 ms, so the daemon sees a peer reboot but never a stale
  sample.
- **Unplug shorter than the link timeout** (1 s < 3 s): the link stays up, the
  daemon reports the stall, and the reboot is detected by `boot_id`, because
  after a replug the board's HELLO is lost (it boots while USB enumerates).
- **Unplug of 5 s**: the stale event comes first (100 ms), then link down
  (3 s), then link up, peer reboot and fresh samples after the replug.
- **Module reload while streaming**: counters start from zero with no receive
  error. The driver opens the port mid-stream, and the first bytes it sees are
  the tail of a frame. It used to count that as a framing error on every load
  or rebind; it now skips to the first delimiter (docs/protocol.md).
- **Missing DT node**: no serdev device and no module autoload; ttyAMA1 stays a
  plain tty, a manual `modprobe` binds nothing, and mcbd keeps searching
  without errors.

## Real board

| fault | driver counters | link down | daemon events | outage (ms) | result |
|---|---|---|---|---|---|
| ESP32 reset (RTS pulse) | overruns +1, resets +1 | no | peer reboot | 133 | PASS |
| module unload + reload while streaming | reset to 0 | no | device gone, device found | 1100 | PASS |
| physical cable unplug ~5 s | resets +1 | yes | stale, link down, link up, peer reboot, fresh | 10762 | PASS |

The 133 ms is the ESP32's real boot time, from the reset pulse to its first
sample.

The physical unplug (`--unplug`) needs a person at the cable; on macOS the test
says when to unplug and, 5 s after the bridge sees the port go, when to plug
back in. It allows any one receive error, because the frame being sent when the
cable is pulled is cut off at an arbitrary byte; in this run there was none.
The reboot is detected through `boot_id`: the board boots while macOS
enumerates the USB device, so its HELLO is lost. The 10.8 s outage is the whole
procedure (5 s wait, the person's reaction, USB enumeration, ESP32 boot), not
a recovery time of the system.

## Elsewhere

- Exact counters for each injected fault at the protocol level:
  `tests/proto/test_proto.py` (every single-bit flip, overrun, wraparound).
- The reader falling behind (daemon stopped): `tests/test_daemon.py`
  (`missed_by_reader`, exact accounting).
- The bridge's own faults (macOS reusing the device name after a replug, the
  guest not reading): `tests/test_bridge.py`.
