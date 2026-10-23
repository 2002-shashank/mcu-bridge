#!/usr/bin/python3
"""Measure boot time and image size of the mcu-bridge image.

usage: scripts/measure-boot.py [-n RUNS]      (ACCEL=hvf etc. are passed to run-qemu.sh)

Per boot, host-side timestamps (seconds since QEMU was launched) for:
  first_byte  first console output (QEMU start + early kernel)
  init        "Run /sbin/init" seen  -> kernel handed off to user space
  login       "login:" prompt        -> system usable
plus kernel_init, the kernel's own printk timestamp of the init handoff.
Medians over all runs are reported because emulated boot times jitter.
"""
import argparse, os, re, select, statistics, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_QEMU = os.path.join(REPO, "scripts", "run-qemu.sh")
IMAGES = os.environ.get("IMAGES", os.path.join(REPO, "out"))


class Guest:
    def __init__(self):
        self.t0 = time.monotonic()
        self.p = subprocess.Popen([RUN_QEMU], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.buf = b""
        self.first_byte = None

    def expect(self, pattern, timeout=120):
        """Read console until regex `pattern` matches; return (match, host time)."""
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
        sys.exit(f"timeout waiting for {pattern!r}; console tail:\n"
                 + self.buf[-1500:].decode(errors="replace"))

    def send(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def run(self, cmd):
        # The quotes keep the echoed command line from matching the marker.
        self.send(cmd.encode() + b'; echo __E""ND__\n')
        m, _ = self.expect(rb"(?s)(.*?)__END__\r?\n", 30)
        lines = m.group(1).decode(errors="replace").splitlines()
        return "\n".join(lines[1:]).strip()  # drop the echoed command

    def close(self):
        self.p.kill()
        self.p.wait()


def boot_once(want_df):
    g = Guest()
    m, t_init = g.expect(rb"\[\s*(\d+\.\d+)\] Run /sbin/init")
    kernel_init = float(m.group(1))
    _, t_login = g.expect(rb"login: ")
    df = None
    if want_df:
        g.send(b"root\n")
        g.expect(rb"# ")
        df = g.run("df -k / | tail -1")
    g.close()
    return dict(first_byte=g.first_byte, kernel_init=kernel_init, init=t_init, login=t_login), df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--runs", type=int, default=5)
    args = ap.parse_args()

    runs, df = [], None
    for i in range(args.runs):
        r, d = boot_once(want_df=(i == 0))
        df = df or d
        runs.append(r)
        print(f"run {i + 1}: " + "  ".join(f"{k}={v:.3f}s" for k, v in r.items()), flush=True)

    med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    print(f"\nboot time, median of {len(runs)} (accel={os.environ.get('ACCEL', 'tcg')}):")
    print(f"  first console byte        {med['first_byte']:.3f}s")
    print(f"  kernel -> init (printk)   {med['kernel_init']:.3f}s")
    print(f"  host: launch -> init      {med['init']:.3f}s")
    print(f"  host: init -> login       {med['login'] - med['init']:.3f}s")
    print(f"  host: launch -> login     {med['login']:.3f}s")

    print("\nimage size:")
    for name in ("Image", "rootfs.ext2"):
        print(f"  {name:<12} {os.path.getsize(os.path.join(IMAGES, name)) / 2**20:7.2f} MiB (file)")
    if df:
        fields = df.split()
        print(f"  rootfs used  {int(fields[2]) / 1024:7.2f} MiB of {int(fields[1]) / 1024:.0f} MiB (df in guest)")


if __name__ == "__main__":
    main()
