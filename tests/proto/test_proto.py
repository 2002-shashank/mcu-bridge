#!/usr/bin/python3
"""Protocol tests: Python implementation behavior, plus conformance of the C
header (protocol/mcb_proto.h, built with ASan/UBSan) against Python.

    tests/proto/test_proto.py [-v]          (CC overrides the compiler)
"""
import os, random, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, "..", "..", "protocol")
sys.path.insert(0, PROTO)
import mcb_proto as P

NAMES = {P.RX_CRC_ERR: "CRC", P.RX_FRAMING_ERR: "FRAMING", P.RX_OVERRUN: "OVERRUN"}


def py_events(stream):
    """Python receiver + seq tracker, formatted like `conformance decode`."""
    rx, seq, out = P.Receiver(), P.SeqTracker(), []
    for st, f in rx.feed(stream):
        if st != P.RX_FRAME:
            out.append(NAMES[st])
            continue
        lost, reset = seq.update(f)
        out.append(f"FRAME {f.type} {f.flags} {f.seq} {f.timestamp_us} "
                   f"{f.payload.hex() or '-'} {lost} {int(reset)}")
    return out


def wire(type=P.TYPE_DATA, seq=0, ts=0, payload=b"", flags=0):
    return P.frame_encode(P.Frame(type, seq, ts, payload, flags))


def raw_frame(ver, type, ln, body_payload):
    """Hand-built frame with a valid CRC, for malformed-but-intact cases."""
    raw = bytes([ver, type, ln, 0]) + bytes(8) + body_payload
    return P.cobs_encode(raw + P.crc32(raw).to_bytes(4, "little")) + b"\x00"


class Python(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(P.crc32(b"123456789"), 0xCBF43926)

    def test_cobs_reference_vectors(self):
        # Cheshire & Baker / Wikipedia examples
        run = bytes(range(1, 255))
        vectors = [
            (b"\x00", b"\x01\x01"),
            (b"\x00\x00", b"\x01\x01\x01"),
            (b"\x00\x11\x00", b"\x01\x02\x11\x01"),
            (b"\x11\x22\x00\x33", b"\x03\x11\x22\x02\x33"),
            (b"\x11\x22\x33\x44", b"\x05\x11\x22\x33\x44"),
            (b"\x11\x00\x00\x00", b"\x02\x11\x01\x01\x01"),
            (run, b"\xff" + run),
            (b"\x00" + run, b"\x01\xff" + run),
            (run + b"\xff", b"\xff" + run + b"\x02\xff"),
        ]
        for raw, enc in vectors:
            with self.subTest(raw=raw[:4].hex()):
                self.assertEqual(P.cobs_encode(raw), enc)
                self.assertEqual(P.cobs_decode(enc, cap=len(raw)), raw)

    def test_round_trip(self):
        f = P.Frame(P.TYPE_DATA, 7, 123456, P.data_payload([0, -1, 3300, 2**31 - 1]))
        w = P.frame_encode(f)
        self.assertNotIn(0, w[:-1])
        self.assertEqual(w[-1], 0)
        self.assertEqual(list(P.Receiver().feed(w)), [(P.RX_FRAME, f)])

    def test_max_frame_fits_buffers(self):
        w = wire(payload=bytes(P.MAX_PAYLOAD))      # all zeros: most COBS codes
        self.assertLessEqual(len(w), P.MAX_WIRE)
        w = wire(payload=b"\xff" * P.MAX_PAYLOAD)
        self.assertLessEqual(len(w), P.MAX_WIRE)
        with self.assertRaises(ValueError):
            wire(payload=bytes(P.MAX_PAYLOAD + 1))

    def test_content_bit_flip_is_crc_error(self):
        # Corrupt the decoded frame (header, payload, CRC), not COBS overhead
        good = P.cobs_decode(wire(seq=1, payload=b"\x11\x22\x33\x44")[:-1])
        for pos in range(len(good)):
            bad = bytearray(good)
            bad[pos] ^= 0x10
            with self.subTest(pos=pos):
                self.assertEqual(py_events(P.cobs_encode(bytes(bad)) + b"\x00"), ["CRC"])

    def test_any_single_bit_flip_loses_only_that_frame(self):
        # Flips on the wire may hit COBS code bytes (-> FRAMING) or create a
        # 0x00 that splits the frame; either way it must never be accepted
        # and the following frame must survive.
        a, b = wire(seq=1, payload=b"\x00\x11\x00\x22"), wire(seq=2)
        for pos in range(len(a) - 1):
            for bit in range(8):
                bad = bytearray(a)
                bad[pos] ^= 1 << bit
                ev = py_events(bytes(bad) + b)
                with self.subTest(pos=pos, bit=bit):
                    self.assertEqual(ev[-1], "FRAME 1 0 2 0 - 0 0")
                    self.assertTrue(all(e in ("CRC", "FRAMING") for e in ev[:-1]), ev)

    def test_malformed_but_intact_is_framing_error(self):
        cases = {
            "bad version": raw_frame(2, P.TYPE_DATA, 0, b""),
            "len mismatch": raw_frame(1, P.TYPE_DATA, 3, b"\x01\x02"),
            "too short": P.cobs_encode(b"\x01\x02\x03") + b"\x00",
            "bad cobs": b"\x09\x01\x02\x00",          # code points past the end
        }
        for name, w in cases.items():
            with self.subTest(name):
                self.assertEqual(py_events(w), ["FRAMING"])

    def test_rom_text_before_hello(self):
        # ESP32 ROM log (no 0x00), the firmware's flush 0x00, then HELLO
        rom = b"ets Jul 29 2019 12:21:46\r\n\r\nrst:0x1 (POWERON_RESET)" * 8
        hello = wire(P.TYPE_HELLO, 0, 0, P.hello_payload(50, 0x00010000))
        ev = py_events(rom + b"\x00" + hello)
        self.assertEqual(ev[0], "OVERRUN")
        self.assertEqual(len(ev), 2)
        self.assertTrue(ev[1].startswith("FRAME 2 ") and ev[1].endswith(" 0 1"))

    def test_extra_delimiters_ignored(self):
        self.assertEqual(py_events(b"\x00\x00" + wire(seq=5) + b"\x00\x00"),
                         ["FRAME 1 0 5 0 - 0 0"])

    def test_seq_gap_wrap_and_reset(self):
        s = [wire(seq=n) for n in (10, 11, 14, 0xFFFFFFFF, 0, 1, 0)]
        ev = py_events(b"".join(s))
        lost_reset = [tuple(e.split()[-2:]) for e in ev]
        self.assertEqual(lost_reset, [
            ("0", "0"),            # first frame syncs
            ("0", "0"),
            ("2", "0"),            # 12, 13 lost
            ("0", "1"),            # 15 -> 0xffffffff is 16 *behind*: restart
            ("0", "0"),            # wrap 0xffffffff -> 0 is in order
            ("0", "0"),
            ("0", "1"),            # backwards: sender restarted, HELLO lost
        ])


class Conformance(unittest.TestCase):
    """The C header must produce byte-identical output to Python."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exe = os.path.join(cls.tmp.name, "conformance")
        cc = os.environ.get("CC", "cc")
        subprocess.run([cc, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        "-I", PROTO, os.path.join(HERE, "conformance.c"), "-o", cls.exe],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def c(self, mode, data):
        r = subprocess.run([self.exe, mode], input=data, capture_output=True, check=True)
        return r.stdout.decode()

    def test_crc(self):
        rnd = random.Random(1)
        for n in (0, 1, 9, 80, 1000):
            data = b"123456789" if n == 9 else rnd.randbytes(n)
            self.assertEqual(self.c("crc", data).strip(), f"{P.crc32(data):08x}")

    def test_encode_identical(self):
        rnd = random.Random(2)
        frames = [P.Frame(P.TYPE_DATA, 0, 0, b""),
                  P.Frame(P.TYPE_DATA, 0xFFFFFFFF, 0xFFFFFFFF, bytes(P.MAX_PAYLOAD), 0xFF),
                  P.Frame(P.TYPE_PING, 1, 2, b"\x00" * 8)]
        for _ in range(300):
            payload = bytes(rnd.choice((0, 0, rnd.randrange(256)))
                            for _ in range(rnd.randrange(P.MAX_PAYLOAD + 1)))
            frames.append(P.Frame(rnd.randrange(256), rnd.randrange(2**32),
                                  rnd.randrange(2**32), payload, rnd.randrange(256)))
        lines = "".join(f"{f.type} {f.flags} {f.seq} {f.timestamp_us} {f.payload.hex() or '-'}\n"
                        for f in frames)
        got = self.c("encode", lines.encode()).split()
        self.assertEqual(got, [P.frame_encode(f).hex() for f in frames])

    def test_decode_identical_on_damaged_streams(self):
        rnd = random.Random(3)
        for trial in range(200):
            parts, seq = [], rnd.randrange(2**32)
            for _ in range(rnd.randrange(1, 30)):
                kind = rnd.random()
                if kind < 0.05:
                    parts.append(rnd.randbytes(rnd.randrange(1, 200)))       # noise
                    continue
                if kind < 0.08:
                    seq = rnd.randrange(2**32)                              # restart
                t = P.TYPE_HELLO if kind < 0.1 else P.TYPE_DATA
                w = bytearray(wire(t, seq, rnd.randrange(2**32),
                                   rnd.randbytes(rnd.randrange(P.MAX_PAYLOAD + 1))))
                seq = (seq + rnd.choice((1, 1, 1, 1, 2, 5))) & 0xFFFFFFFF   # gaps
                r = rnd.random()
                if r < 0.1:
                    w[rnd.randrange(len(w))] ^= 1 << rnd.randrange(8)       # bit flip
                elif r < 0.15:
                    w = w[:rnd.randrange(len(w))]                           # truncate
                parts.append(bytes(w))
            stream = b"".join(parts)
            with self.subTest(trial=trial):
                self.assertEqual(self.c("decode", stream).splitlines(), py_events(stream))


if __name__ == "__main__":
    unittest.main()
