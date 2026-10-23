# mcu-bridge wire protocol, version 1

Link between the ESP32 firmware and the Linux serdev driver over a UART
(115200 8N1, no flow control). Reference implementations:
`protocol/mcb_proto.h` (kernel, Zephyr) and `protocol/mcb_proto.py` (simulator,
tests). `tests/proto/test_proto.py` checks that both behave identically.

## Framing

On the wire each frame is `COBS(frame) 0x00`.

[COBS](https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing)
removes every `0x00` from the frame, so `0x00` only ever marks a frame
boundary. A receiver that loses sync (noise, a frame cut short by an unplug,
an ESP32 reset) is back in sync at the next `0x00`: a damaged frame costs that
frame only. Overhead is at most one byte per 254, so the worst-case size is
fixed.

Rules:

- Receivers ignore empty frames (`0x00 0x00`). A sender may emit `0x00` at any
  time to terminate whatever a receiver has buffered.
- The ESP32 emits `0x00` before its first frame after boot, so its ROM boot log
  (text without `0x00`) is discarded as one overrun instead of corrupting the
  first frame.
- If more than `MCB_MAX_COBS` (81) bytes arrive without a `0x00`, the receiver
  reports one overrun and discards bytes up to the next `0x00`.

## Frame layout

All multi-byte fields are little-endian.

| Offset   | Size | Field          | Notes                                            |
|----------|------|----------------|--------------------------------------------------|
| 0        | 1    | `version`      | `1`                                              |
| 1        | 1    | `type`         | see below                                        |
| 2        | 1    | `len`          | payload length, 0..64                            |
| 3        | 1    | `boot_id`      | sender's random 1..255, fixed for one boot       |
| 4        | 4    | `seq`          | per-direction counter, +1 per frame, wraps at 2³² |
| 8        | 4    | `timestamp_us` | sender's monotonic clock in µs, wraps every ~71.6 min |
| 12       | len  | `payload`      |                                                  |
| 12 + len | 4    | `crc32`        | over bytes `0 .. 11 + len`                       |

Decoded frames are 16..80 bytes; on the wire at most 82 bytes plus the delimiter.

`crc32` is the standard CRC-32 (IEEE 802.3: reflected polynomial `0xedb88320`,
init `0xffffffff`, final XOR `0xffffffff`; check value `0xcbf43926` for
`"123456789"`). Kernel: `~crc32_le(~0, p, n)`. Zephyr: `crc32_ieee(p, n)`.
Python: `zlib.crc32(b)`.

Timestamps are only ever compared by unsigned subtraction
(`(uint32_t)(b - a)`), which stays correct across the wrap.

## Messages

| Type   | Name     | Direction    | Payload                                                     |
|--------|----------|--------------|-------------------------------------------------------------|
| `0x01` | DATA     | ESP32 → host | `n` × `s32` channel values, `n = len / 4`, 1..16; ch0 = light sensor in mV |
| `0x02` | HELLO    | ESP32 → host | `u8 proto_version`, `u8 reserved`, `u16 rate_hz`, `u32 fw_version` |
| `0x10` | PING     | host → ESP32 | 0..8 byte opaque token                                      |
| `0x11` | PONG     | ESP32 → host | `u32 req_seq`, then the PING token echoed                   |
| `0x12` | SET_RATE | host → ESP32 | `u16 rate_hz`, 1..200                                       |
| `0x13` | ACK      | ESP32 → host | `u32 req_seq`, `u8 req_type`, `u8 status`                   |

- HELLO is the first frame after every ESP32 boot. The host must not rely on
  seeing it: after a USB replug the board boots while the host's serial port
  is still enumerating, so its ROM log and HELLO are lost. `boot_id` is what
  makes such a reboot detectable.
- Every host command gets exactly one response: PING gets PONG; anything else
  gets ACK. `req_seq` is the `seq` of the command answered, so responses can be
  matched to requests even when one is lost.
- ACK `status`: `0` OK, `1` argument out of range, `2` unsupported type.
  Unknown command types are answered (status 2), never ignored.
- 200 Hz limit: a 4-channel DATA frame is ~34 bytes on the wire; 200 Hz uses
  ~60% of 115200 baud, leaving room for responses.

## Receiver behavior

Processing a frame, in this order:

1. COBS-decode. Invalid encoding or fewer than 16 bytes → **framing error**.
2. Check the CRC over the decoded size (not the `len` field) → **CRC error**.
3. Check `version == 1` and `len == decoded size - 16` → **framing error**.
4. Accept the frame and update sequence tracking.

Checking the CRC before trusting `version`/`len` means corruption of the
header, payload or CRC is reported as a CRC error. Framing errors are frames
that are structurally broken: corruption of COBS overhead bytes, or a sender
producing malformed frames (bug, version mismatch).

Sequence tracking (per direction), with `d = (int32_t)(seq - expected)`,
first matching row wins:

| Condition                     | Result                                         |
|-------------------------------|------------------------------------------------|
| first frame                   | sync, nothing counted (reset if HELLO)         |
| `boot_id` changed             | **reset**: sender rebooted, even if HELLO lost |
| `type == HELLO`               | **reset**                                      |
| `d < 0`                       | **reset**: seq went back (boot_id repeated by chance, 1 in 255) |
| `d >= 0`                      | **`d` frames dropped**                         |

`expected` becomes `seq + 1` and the stored `boot_id` is updated after every
accepted frame. Without `boot_id`, a reboot whose new `seq` happens to be ahead
of the old one would be miscounted as dropped frames.

## Driver counters

| Counter          | Incremented when                                            |
|------------------|-------------------------------------------------------------|
| `rx_frames`      | a frame is accepted                                         |
| `crc_errors`     | step 2 fails                                                |
| `framing_errors` | step 1 or 3 fails                                           |
| `overruns`       | no delimiter within 81 bytes                                |
| `dropped_frames` | by `d` for each accepted frame                              |
| `resets`         | sender restart detected                                     |
