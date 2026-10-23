#!/usr/bin/python3
"""Simulator self-test (no QEMU): the test listens on the socket like QEMU does
and checks that each fault injection has exactly the documented effect, since
the driver fault tests derive their expected counters from it."""
import json, os, select, socket, subprocess, sys, tempfile, time, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "protocol"))
import mcb_proto as P


class SimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "esp32.sock")
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(1)
        self.sim = subprocess.Popen([os.path.join(ROOT, "scripts", "esp32-sim.py"),
                                     "--socket", path, "--rate", "200"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.accept()

    def tearDown(self):
        self.sim.kill()
        self.sim.communicate()  # reap and close pipes
        self.conn.close()
        self.srv.close()
        self.tmp.cleanup()

    def accept(self):
        # Receiver state persists across connections, like the guest's driver
        self.srv.settimeout(5)
        self.conn, _ = self.srv.accept()
        if not hasattr(self, "rx"):
            self.rx, self.seq = P.Receiver(), P.SeqTracker()
            self.counts = dict(frames=0, crc=0, framing=0, overrun=0, lost=0, resets=0)
            self.frames = []

    def ctl(self, line):
        self.sim.stdin.write(line.encode() + b"\n")
        self.sim.stdin.flush()

    def pump(self, secs):
        """Receive and classify for `secs` seconds, like the driver would."""
        names = {P.RX_CRC_ERR: "crc", P.RX_FRAMING_ERR: "framing", P.RX_OVERRUN: "overrun"}
        end = time.monotonic() + secs
        self.conn.settimeout(0.05)
        while time.monotonic() < end:
            try:
                data = self.conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            for st, f in self.rx.feed(data):
                if st != P.RX_FRAME:
                    self.counts[names[st]] += 1
                    continue
                lost, reset = self.seq.update(f)
                self.counts["frames"] += 1
                self.counts["lost"] += lost
                self.counts["resets"] += reset
                self.frames.append(f)

    def event(self, kind, timeout=2):
        """Next simulator event of `kind`, or fail; raw fd reads with a deadline
        (readline + select would hang on a frozen simulator)."""
        fd, end = self.sim.stdout.fileno(), time.monotonic() + timeout
        if not hasattr(self, "evbuf"):
            self.evbuf = b""
        while True:
            *lines, self.evbuf = self.evbuf.split(b"\n")
            for i, line in enumerate(lines):
                ev = json.loads(line)
                if ev["event"] == kind:
                    self.evbuf = b"\n".join(lines[i + 1:] + [self.evbuf])
                    return ev
            r, _, _ = select.select([fd], [], [], max(0, end - time.monotonic()))
            if not r:
                self.fail(f"no {kind!r} event from simulator within {timeout} s")
            self.evbuf += os.read(fd, 65536)

    def sim_stats(self):
        self.ctl("stats")
        return self.event("stats")

    def test_boot_sequence(self):
        self.pump(0.3)
        # ROM text (no 0x00, > MAX_COBS bytes) is one overrun, then HELLO
        self.assertEqual(self.counts["overrun"], 1)
        self.assertEqual(self.frames[0].type, P.TYPE_HELLO)
        self.assertEqual(self.counts["resets"], 1)
        self.assertEqual(self.counts["crc"] + self.counts["framing"] + self.counts["lost"], 0)

    def test_corrupt_is_exactly_n_crc_errors(self):
        self.pump(0.2)
        self.ctl("corrupt 5")
        self.pump(0.5)
        self.assertEqual(self.counts["crc"], 5)
        # a corrupted frame is lost, so the next good one shows a gap of 1
        self.assertEqual(self.counts["lost"], 5)
        self.assertEqual(self.counts["framing"], 0)

    def test_drop_is_exactly_n_lost(self):
        self.pump(0.2)
        self.ctl("drop 7")
        self.pump(0.5)
        self.assertEqual(self.counts["lost"], 7)
        self.assertEqual(self.counts["crc"] + self.counts["framing"], 0)
        self.assertEqual(self.sim_stats()["dropped"], 7)

    def test_reset_is_visible_boot(self):
        self.pump(0.2)
        self.ctl("reset")
        self.pump(0.3)
        self.assertEqual(self.counts["resets"], 2)
        self.assertEqual(self.counts["overrun"], 2)   # ROM text each boot
        hellos = [f for f in self.frames if f.type == P.TYPE_HELLO]
        self.assertEqual([h.seq for h in hellos], [0, 0])
        self.assertNotEqual(hellos[0].boot_id, hellos[1].boot_id)

    def test_unplug_reboots_with_boot_output_lost(self):
        self.pump(0.3)
        n, old_boot = len(self.frames), self.frames[-1].boot_id
        self.ctl("unplug 0.3")
        self.pump(0.6)                     # sim closes the connection
        self.accept()                      # ... and comes back mid-stream
        self.pump(0.3)
        first = self.frames[n]
        self.assertEqual(first.type, P.TYPE_DATA)        # HELLO was lost
        self.assertGreater(first.seq, 0)
        self.assertNotEqual(first.boot_id, old_boot)
        self.assertEqual(self.counts["resets"], 2)       # detected via boot_id
        self.assertEqual(self.counts["overrun"], 1)      # no ROM text this time
        self.assertEqual(self.counts["lost"], 0)

    def test_qemu_reconnect_is_not_a_reboot(self):
        self.pump(0.3)
        old_boot = self.frames[-1].boot_id
        self.conn.close()                  # QEMU went away; the board did not
        time.sleep(0.3)
        self.accept()
        self.pump(0.3)
        self.assertEqual(self.frames[-1].boot_id, old_boot)
        self.assertEqual(self.counts["resets"], 1)       # only the initial boot
        # (no loss assertion: our listening socket queues the reconnect in its
        # backlog; a real QEMU restart would lose frames while it is gone)

    def send_cmd(self, type, seq, payload):
        self.conn.sendall(b"\x00" + P.frame_encode(P.Frame(type, seq, 0, payload)))
        self.pump(0.3)
        return [f for f in self.frames if f.type in (P.TYPE_PONG, P.TYPE_ACK)][-1]

    def test_commands(self):
        self.pump(0.2)
        pong = self.send_cmd(P.TYPE_PING, 10, b"abc")
        self.assertEqual((pong.type, pong.payload), (P.TYPE_PONG, (10).to_bytes(4, "little") + b"abc"))
        for hz, status in ((20, P.STATUS_OK), (0, P.STATUS_RANGE), (201, P.STATUS_RANGE)):
            ack = self.send_cmd(P.TYPE_SET_RATE, 11, hz.to_bytes(2, "little"))
            self.assertEqual(ack.payload, P.ack_payload(11, P.TYPE_SET_RATE, status))
        ack = self.send_cmd(0x7F, 12, b"")
        self.assertEqual(ack.payload, P.ack_payload(12, 0x7F, P.STATUS_UNSUPPORTED))

    def test_stalled_reader_does_not_freeze_sim(self):
        # Driver unloaded / port closed: QEMU stops reading. A UART never
        # blocks its sender, so the sim must keep running and count the loss.
        time.sleep(3)                            # 200 Hz x ~34 B >> socket buffer
        self.assertGreater(self.sim_stats()["tx_bytes_lost"], 0)
        self.pump(0.5)                           # resumes; damage limited to the stall
        self.assertGreater(self.counts["frames"], 0)

    def test_set_rate_changes_rate(self):
        self.pump(0.2)
        self.send_cmd(P.TYPE_SET_RATE, 1, (20).to_bytes(2, "little"))
        n = self.counts["frames"]
        self.pump(1.0)
        data = self.counts["frames"] - n
        self.assertTrue(17 <= data <= 23, data)


if __name__ == "__main__":
    unittest.main()
