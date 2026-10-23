#!/usr/bin/python3
"""Pump bytes between the real ESP32's USB serial port and QEMU's ESP32 socket.

    scripts/esp32-bridge.py [--tty /dev/cu.usbserial-110] [--socket out/esp32.sock]

Either side may disappear: an unplugged ESP32 is reopened when its device node
comes back, and a restarted QEMU is reconnected. Like a wire, the bridge never
blocks: bytes the other side can't take right now (ESP32 unplugged, or the
guest not reading ttyAMA1 so QEMU stops reading the socket) are dropped and
counted. A blocking write would freeze the bridge and hide an unplug.

    kill -USR1 <pid>   reset the ESP32 (CYD auto-reset circuit: RTS -> EN)
"""
import argparse, errno, os, select, signal, socket, sys, time

from serial_port import open_tty, pulse_reset

POLL_S = 0.5


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, tty_path, sock_path):
        self.tty_path, self.sock_path = tty_path, sock_path
        self.tty = self.sock = None
        self.tty_rdev = None
        self.reset_requested = False
        self.stats = dict(to_guest=0, to_esp32=0, dropped_to_guest=0, dropped_to_esp32=0,
                          unplugs=0)
        self.waiting_logged = set()

    def ensure_tty(self):
        if self.tty is not None:
            return
        try:
            self.tty = open_tty(self.tty_path)
            self.tty_rdev = os.fstat(self.tty).st_rdev
            log(f"ESP32 connected: {self.tty_path}")
            self.waiting_logged.discard("tty")
        except OSError as e:
            if "tty" not in self.waiting_logged:
                log(f"waiting for {self.tty_path} ({e.strerror})")
                self.waiting_logged.add("tty")

    def ensure_sock(self):
        if self.sock is not None:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.sock_path)
            s.setblocking(False)
            self.sock = s
            log(f"QEMU connected: {self.sock_path}")
            self.waiting_logged.discard("sock")
        except OSError as e:
            s.close()
            if "sock" not in self.waiting_logged:
                log(f"waiting for QEMU at {self.sock_path} ({e.strerror})")
                self.waiting_logged.add("sock")

    def tty_replaced(self):
        """Is our fd still the device the path names? On macOS an unplugged
        USB serial fd can stay silent (no error, no select wakeup), and after a
        replug a new device instance appears under the same name, so compare
        device numbers instead of waiting for read() to fail."""
        try:
            rdev = os.stat(self.tty_path).st_rdev
        except FileNotFoundError:
            self.tty_lost("device node removed")
            return True
        if rdev != self.tty_rdev:
            self.tty_lost(f"device replaced: {os.major(self.tty_rdev)},{os.minor(self.tty_rdev)}"
                          f" -> {os.major(rdev)},{os.minor(rdev)}")
            return True
        return False

    def tty_lost(self, why):
        os.close(self.tty)
        self.tty = None
        self.stats["unplugs"] += 1
        log(f"ESP32 disconnected ({why}); {self.stats}")

    def sock_lost(self):
        self.sock.close()
        self.sock = None
        log("QEMU disconnected")

    def tty_write(self, data):
        if self.tty is None:
            self.stats["dropped_to_esp32"] += len(data)
            return
        try:
            n = os.write(self.tty, data)
        except BlockingIOError:
            n = 0
        except OSError as e:
            self.tty_lost(e.strerror)
            return
        self.stats["to_esp32"] += n
        self.stats["dropped_to_esp32"] += len(data) - n

    def sock_write(self, data):
        try:
            n = self.sock.send(data)
        except BlockingIOError:
            n = 0
        except OSError:
            self.sock_lost()
            return
        self.stats["to_guest"] += n
        self.stats["dropped_to_guest"] += len(data) - n

    def run(self):
        signal.signal(signal.SIGUSR1, lambda *_: setattr(self, "reset_requested", True))
        log(f"bridge pid {os.getpid()}: {self.tty_path} <-> {self.sock_path}")
        while True:
            self.ensure_tty()
            self.ensure_sock()
            if self.reset_requested and self.tty is not None:
                self.reset_requested = False
                pulse_reset(self.tty)
                log("ESP32 reset pulsed")
            fds = [f for f in (self.tty, self.sock) if f is not None]
            # select() resumes after SIGUSR1 (PEP 475): a reset waits <= POLL_S
            readable, _, _ = select.select(fds, [], [], POLL_S)
            if self.tty is not None and self.tty_replaced():
                continue
            if self.tty is not None and self.tty in readable:
                try:
                    data = os.read(self.tty, 4096)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EINTR):
                        continue
                    self.tty_lost(e.strerror)  # macOS: ENXIO "Device not configured"
                    continue
                if not data:
                    self.tty_lost("EOF")
                    continue
                if self.sock is not None:
                    self.sock_write(data)
                else:
                    self.stats["dropped_to_guest"] += len(data)
            if self.sock is not None and self.sock in readable:
                try:
                    data = self.sock.recv(4096)
                except BlockingIOError:  # spurious wakeup, not a disconnect
                    continue
                except OSError:
                    data = b""
                if not data:
                    self.sock_lost()
                    continue
                self.tty_write(data)


def main():
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tty", default="/dev/cu.usbserial-110")
    ap.add_argument("--socket", default=os.path.join(repo, "out", "esp32.sock"))
    args = ap.parse_args()
    try:
        Bridge(args.tty, args.socket).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
