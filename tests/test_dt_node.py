#!/usr/bin/python3
"""DT node test: the overlay hands ttyAMA1 to serdev; without it the port stays a tty.

  with overlay      /dev/ttyAMA1 absent, serdev device serial0-0 with our compatible
  DT_OVERLAY=none   /dev/ttyAMA1 present, no serdev device (the "missing DT node" fault)
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from qemu_guest import Guest

DEV = "/sys/bus/serial/devices/serial0-0"


def probe(env):
    g = Guest(env=env)
    try:
        g.login()
        return {
            "ttys": g.run("ls /dev/ttyAMA*").split(),
            "serdev": g.run(f"cat {DEV}/of_node/compatible 2>/dev/null || echo absent").strip("\x00"),
            "speed": g.run(f"od -An -tx1 {DEV}/of_node/current-speed 2>/dev/null").split(),
            "dt_node": g.run("ls -d /proc/device-tree/pl011@9040000/mcu 2>/dev/null || echo absent"),
        }
    finally:
        g.close()


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main():
    ok = True
    print("with overlay:")
    s = probe({})
    ok &= check("tty devices", s["ttys"], ["/dev/ttyAMA0"])
    ok &= check("serdev compatible", s["serdev"], "mcubridge,esp32")
    ok &= check("current-speed (115200, big-endian)", s["speed"], ["00", "01", "c2", "00"])

    print("DT_OVERLAY=none (missing DT node):")
    s = probe({"DT_OVERLAY": "none"})
    ok &= check("tty devices", s["ttys"], ["/dev/ttyAMA0", "/dev/ttyAMA1"])
    ok &= check("serdev device", s["serdev"], "absent")
    ok &= check("DT node", s["dt_node"], "absent")

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
