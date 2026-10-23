"""Drive the mcu-bridge QEMU guest over its serial console (scripts/run-qemu.sh).

    g = Guest(env={"DT_OVERLAY": "none"})
    g.login()
    print(g.run("ls /sys/bus/serial/devices"))
    g.close()
"""
import os, re, select, subprocess, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_QEMU = os.path.join(REPO, "scripts", "run-qemu.sh")
ANSI = re.compile(rb"\x1b\[[0-9;]*m")


class Guest:
    def __init__(self, env=None):
        self.t0 = time.monotonic()
        self.p = subprocess.Popen([RUN_QEMU], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, env={**os.environ, **(env or {})})
        self.buf = b""
        self.first_byte = None

    def expect(self, pattern, timeout=120):
        """Read console until regex `pattern` matches; return (match, seconds since launch)."""
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = rx.search(self.buf)
            if m:
                self.buf = self.buf[m.end():]
                return m, time.monotonic() - self.t0
            r, _, _ = select.select([self.p.stdout], [], [], 0.5)
            if r:
                chunk = os.read(self.p.stdout.fileno(), 4096)
                if not chunk:
                    break
                if self.first_byte is None:
                    self.first_byte = time.monotonic() - self.t0
                self.buf += chunk
        raise TimeoutError(f"waiting for {pattern!r}; console tail:\n"
                           + self.buf[-1500:].decode(errors="replace"))

    def send(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def login(self, at_prompt=False):
        if not at_prompt:
            self.expect(rb"login: ")
        self.send(b"root\n")
        self.expect(rb"# ")

    def run(self, cmd, timeout=30):
        """Run a shell command in the guest and return its output (colors stripped)."""
        # Bracket the output with markers. The quotes keep the terminal's echo of
        # the command line (which may wrap) from matching them.
        self.send(b'echo __B""EGIN__; ' + cmd.encode() + b'; echo __E""ND__\n')
        m, _ = self.expect(rb"(?s)__BEGIN__\r?\n(.*?)__END__\r?\n", timeout)
        return ANSI.sub(b"", m.group(1)).decode(errors="replace").replace("\r", "").strip()

    def close(self):
        self.p.kill()
        self.p.wait()
