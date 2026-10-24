#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Bridge recovery test without hardware or QEMU.

The ESP32 port is a symlink to a pty, the test plays QEMU's socket server.
Re-pointing the symlink at a second pty reproduces what macOS does on a USB
replug: the old fd stays valid and silent while a new device appears under the
same name. Removing the symlink is an unplug.
"""
import fcntl, os, socket, subprocess, sys, tempfile, time, tty, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.link = os.path.join(self.tmp.name, "cu.fake")
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(os.path.join(self.tmp.name, "esp32.sock"))
        self.srv.listen(1)
        self.ptys = []
        self.plug()
        self.log_path = os.path.join(self.tmp.name, "bridge.log")
        self.log_file = open(self.log_path, "w")
        self.bridge = subprocess.Popen(
            [os.path.join(ROOT, "scripts", "esp32-bridge.py"), "--tty", self.link,
             "--socket", self.srv.getsockname()],
            stderr=self.log_file)
        self.srv.settimeout(5)
        self.conn, _ = self.srv.accept()
        self.conn.settimeout(3)

    def tearDown(self):
        self.bridge.kill()
        self.bridge.wait()
        self.log_file.close()
        self.conn.close()
        self.srv.close()
        for m, s in self.ptys:
            os.close(m)
            os.close(s)
        self.tmp.cleanup()

    def plug(self):
        """A new device instance appears under the port name."""
        master, slave = os.openpty()
        tty.setraw(master)
        self.ptys.append((master, slave))
        tmp_link = self.link + ".new"
        os.symlink(os.ttyname(slave), tmp_link)
        os.replace(tmp_link, self.link)  # atomic, like a node that is simply there
        return master

    def assert_flows(self, master, tag):
        """ESP32 -> guest and guest -> ESP32 through the bridge."""
        deadline = time.monotonic() + 3
        got = b""
        while tag not in got and time.monotonic() < deadline:
            os.write(master, tag)       # bridge may still be reopening: resend
            try:
                got += self.conn.recv(4096)
            except socket.timeout:
                pass
        self.assertIn(tag, got)
        self.conn.sendall(b"to-esp32:" + tag)
        time.sleep(0.3)
        self.assertIn(b"to-esp32:" + tag, os.read(master, 4096))

    def test_replug_same_name_new_device(self):
        self.assert_flows(self.ptys[0][0], b"before")
        new = self.plug()               # old pty stays open and silent
        time.sleep(1.2)
        self.assert_flows(new, b"after-replug")

    def wait_log(self, text, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with open(self.log_path) as f:
                if text in f.read():
                    return True
            time.sleep(0.1)
        return False

    def test_stalled_guest_does_not_freeze_bridge(self):
        # Guest not reading ttyAMA1 -> QEMU stops reading the socket. A blocking
        # bridge would hang in send() and never notice the replug.
        master = self.ptys[0][0]
        fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
        for _ in range(200):                     # ~200 KB, far beyond socket buffers
            try:
                os.write(master, b"x" * 1024)
            except BlockingIOError:
                time.sleep(0.005)
        new = self.plug()
        self.assertTrue(self.wait_log("device replaced", 3),
                        "replug not seen while the guest was stalled")
        self.conn.setblocking(False)             # now drain what was queued
        try:
            while self.conn.recv(65536):
                pass
        except BlockingIOError:
            pass
        self.conn.settimeout(3)
        self.assert_flows(new, b"after-stall")

    def test_unplug_then_replug(self):
        self.assert_flows(self.ptys[0][0], b"before")
        os.unlink(self.link)
        time.sleep(1.2)
        self.conn.sendall(b"lost")      # guest bytes while unplugged: dropped
        time.sleep(0.3)
        new = self.plug()
        time.sleep(1.2)
        self.assert_flows(new, b"after-unplug")


if __name__ == "__main__":
    unittest.main()
