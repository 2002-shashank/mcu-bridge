#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Firmware test: speak the protocol directly over the ESP32's USB serial port
(no QEMU, no driver), so a failure points at the firmware alone.

    tests/test_firmware.py [--tty /dev/cu.usbserial-110] [--touch]

--touch adds a step where a person presses the screen for a few seconds.
"""
import argparse, os, statistics, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish
from esp32_board import Board
from serial_port import pulse_reset
import mcb_proto as P


def data(frames):
    return [f for f, _, _ in frames if f.type == P.TYPE_DATA]


def measure_rate(b, secs):
    frames, errors, times = b.collect(secs)
    d = data(frames)
    lost = sum(l for _, l, _ in frames)
    return len(d) / secs, lost, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tty", default="/dev/cu.usbserial-110")
    ap.add_argument("--touch", action="store_true")
    args = ap.parse_args()
    b = Board(args.tty)

    print("boot")
    pulse_reset(b.fd)
    frames, errors, _ = b.collect(3, until=lambda f: f.type == P.TYPE_HELLO)
    hello = frames[-1][0] if frames else None
    check("ROM/bootloader text discarded as overrun before HELLO", P.RX_OVERRUN in errors, str(errors))
    # Frames still arriving from before the reset carry the old boot_id
    early = [f for f, _, _ in frames[:-1] if hello and f.boot_id == hello.boot_id]
    check("HELLO is the new boot's first frame, seq 0",
          hello and hello.type == P.TYPE_HELLO and hello.seq == 0 and not early,
          f"{len(frames) - 1} frame(s) from the previous boot before it")
    if not hello:
        finish()
    ver, _, rate, fw = __import__("struct").unpack("<BBHI", hello.payload)
    check("HELLO: protocol 1, rate 50, fw 1.0.0", (ver, rate, fw) == (1, 50, 0x00010000),
          f"proto {ver}, rate {rate}, fw {fw >> 16}.{fw >> 8 & 255}.{fw & 255}")
    check("boot_id non-zero", hello.boot_id != 0, str(hello.boot_id))
    first_boot_id = hello.boot_id

    print("stream")
    frames, errors, times = b.collect(2)
    d = data(frames)
    check("DATA at ~50 Hz", 95 <= len(d) <= 105, f"{len(d)} in 2 s")
    check("no gaps, no errors", not errors and all(l == 0 for _, l, _ in frames), f"{errors}")
    gaps = [(d[i + 1].timestamp_us - d[i].timestamp_us) & 0xFFFFFFFF for i in range(len(d) - 1)]
    check("timestamps 20 ms apart (median)", 19500 <= statistics.median(gaps) <= 20500,
          f"median {statistics.median(gaps)} us, min {min(gaps)}, max {max(gaps)}")
    v = P.data_values(d[-1].payload)
    check("4 channels; untouched reads 0,-1,-1,0", len(v) == 4 and (v == [0, -1, -1, 0] or v[0] == 1), str(v))

    print("commands")
    rtts = []
    for i in range(20):
        tok = bytes([i]) * 8
        resp, ms = b.request(P.TYPE_PING, tok)
        if not resp or resp.type != P.TYPE_PONG or resp.payload[4:] != tok:
            break
        rtts.append(ms)
    check("20 PINGs answered with matching req_seq and token", len(rtts) == 20, f"{len(rtts)}/20")
    if rtts:
        print(f"        host-side RTT over USB serial: median {statistics.median(rtts):.1f} ms, "
              f"min {min(rtts):.1f}, max {max(rtts):.1f}")
    check("SET_RATE 0 -> out of range", b.set_rate(0) == P.STATUS_RANGE)
    check("SET_RATE 201 -> out of range", b.set_rate(201) == P.STATUS_RANGE)
    resp, _ = b.request(0x7F)
    check("unknown type -> ACK unsupported",
          resp and resp.type == P.TYPE_ACK and resp.payload[4:] == bytes([0x7F, P.STATUS_UNSUPPORTED]))
    seq = b.send(P.TYPE_PING, b"bad", corrupt=True)
    frames, _, _ = b.collect(0.5)
    check("corrupted command ignored (no reply)",
          not any(f.type in (P.TYPE_PONG, P.TYPE_ACK) and f.payload[:4] == seq.to_bytes(4, "little")
                  for f, _, _ in frames))
    resp, _ = b.request(P.TYPE_PING, b"after")
    check("still answers after the bad frame", resp and resp.type == P.TYPE_PONG)

    print("rates")
    for hz in (20, 200, 1):
        check(f"SET_RATE {hz} -> OK", b.set_rate(hz) == P.STATUS_OK)
        b.collect(0.3)  # settle
        secs = 3 if hz == 1 else 2
        got, lost, errors = measure_rate(b, secs)
        check(f"stream at ~{hz} Hz, no gaps or errors", abs(got - hz) <= max(1, hz * 0.05)
              and lost == 0 and not errors, f"{got:.1f} Hz, lost {lost}, errors {errors}")
    b.set_rate(50)

    if args.touch:
        print(">>> PRESS AND DRAG on the screen for the next 6 seconds", flush=True)
        frames, _, _ = b.collect(6)
        touched = [P.data_values(f.payload) for f in data(frames) if P.data_values(f.payload)[0] == 1]
        check("touch reported", len(touched) > 10, f"{len(touched)} touched samples")
        if touched:
            xs, ys, ps = zip(*[(t[1], t[2], t[3]) for t in touched])
            check("x/y within 12-bit range, pressure above threshold",
                  all(0 <= x <= 4095 for x in xs) and all(0 <= y <= 4095 for y in ys) and min(ps) > 100,
                  f"x {min(xs)}..{max(xs)}, y {min(ys)}..{max(ys)}, pressure {min(ps)}..{max(ps)}")

    print("reset")
    pulse_reset(b.fd)
    frames, _, _ = b.collect(3, until=lambda f: f.type == P.TYPE_HELLO)
    hello = frames[-1][0] if frames and frames[-1][0].type == P.TYPE_HELLO else None
    check("HELLO after reset, seen as a reset", hello and frames[-1][2])
    if hello:
        check("new boot_id", hello.boot_id != first_boot_id,
              f"{first_boot_id} -> {hello.boot_id} (1-in-255 chance of a repeat)")
        check("rate back to default 50", int.from_bytes(hello.payload[2:4], "little") == 50)
    finish()


if __name__ == "__main__":
    main()
