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
import argparse, os, statistics

from qemu_guest import REPO, Guest

IMAGES = os.environ.get("IMAGES", os.path.join(REPO, "out"))


def boot_once(want_df):
    g = Guest()
    m, t_init = g.expect(rb"\[\s*(\d+\.\d+)\] Run /sbin/init")
    kernel_init = float(m.group(1))
    _, t_login = g.expect(rb"login: ")
    df = None
    if want_df:
        g.login(at_prompt=True)
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
