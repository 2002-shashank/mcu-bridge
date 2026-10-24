# SPDX-License-Identifier: MIT
"""Speak the mcu-bridge protocol directly to the ESP32 over its USB serial port
(no QEMU, no driver). Used by tests/test_firmware.py and scripts/measure-ping.py."""
import os, random, select, time

from serial_port import open_tty
import mcb_proto as P


class Board:
    def __init__(self, path):
        self.fd = open_tty(path)
        self.rx, self.seqs = P.Receiver(), P.SeqTracker()
        self.tx_seq = 0
        self.boot_id = random.randrange(1, 256)
        self.pending = []  # parsed events not yet returned by collect()

    def collect(self, secs, until=None):
        """Read for `secs` (or until `until(frame)`); return (frames, errors, arrival times).

        Every byte read is parsed; events after an `until` match stay queued for
        the next call (returning mid-buffer would silently drop the rest)."""
        frames, errors, times = [], [], []
        end = time.monotonic() + secs
        while True:
            while self.pending:
                st, f, t = self.pending.pop(0)
                if st != P.RX_FRAME:
                    errors.append(st)
                    continue
                lost, reset = self.seqs.update(f)
                frames.append((f, lost, reset))
                times.append(t)
                if until and until(f):
                    return frames, errors, times
            if time.monotonic() >= end:
                return frames, errors, times
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                now = time.monotonic()
                self.pending += [(st, f, now) for st, f in self.rx.feed(os.read(self.fd, 4096))]

    def send(self, type, payload=b"", corrupt=False):
        f = P.Frame(type, self.tx_seq, 0, payload, self.boot_id)
        self.tx_seq += 1
        w = bytearray(P.frame_encode(f))
        if corrupt:  # flip a content bit, re-encode: a guaranteed CRC error
            raw = bytearray(P.cobs_decode(bytes(w[:-1])))
            raw[5] ^= 0x01
            w = bytearray(P.cobs_encode(bytes(raw)) + b"\x00")
        os.write(self.fd, b"\x00" + bytes(w))
        return f.seq

    def request(self, type, payload=b"", timeout=1.0):
        """Send a command, return (response frame or None, round trip in ms)."""
        t0 = time.monotonic()
        seq = self.send(type, payload)
        match = lambda f: f.type in (P.TYPE_PONG, P.TYPE_ACK) and f.payload[:4] == seq.to_bytes(4, "little")
        frames, _, _ = self.collect(timeout, until=match)
        resp = next((f for f, _, _ in frames if match(f)), None)
        return resp, (time.monotonic() - t0) * 1000

    def set_rate(self, hz):
        resp, _ = self.request(P.TYPE_SET_RATE, hz.to_bytes(2, "little"))
        return resp.payload[5] if resp else None
