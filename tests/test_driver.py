#!/usr/bin/python3
"""Driver test: guest with the DT overlay (mcb_bridge binds via coldplug) talking
to the simulated ESP32.

Counter checks use deltas around one injected fault and require the exact
change in every counter, including zero for the ones that must not move.
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, sim
from qemu_guest import Guest

DEV = "/sys/bus/serial/devices/serial0-0"
COUNTERS = ("rx_frames", "crc_errors", "framing_errors", "overruns", "dropped_frames", "resets")


class Driver:
    def __init__(self, g):
        self.g = g

    def read(self, attr):
        return self.g.run(f"cat {DEV}/{attr} 2>&1")

    def write(self, attr, value):
        """Write to a sysfs attribute; returns '' on success or the shell's error."""
        out = self.g.run(f"echo {value} > {DEV}/{attr} && echo OK", timeout=10)
        return "" if out.endswith("OK") else out

    def counters(self):
        vals = self.g.run(f"cd {DEV} && cat {' '.join(COUNTERS)}").split()
        return dict(zip(COUNTERS, map(int, vals)))


def delta_after(drv, action, settle=0.6):
    before = drv.counters()
    action()
    time.sleep(settle)
    after = drv.counters()
    return {k: after[k] - before[k] for k in COUNTERS}


def expect_delta(name, d, **want):
    """Exact change for the named counters, zero for every other error counter."""
    errors = {k: v for k, v in d.items() if k != "rx_frames"}
    expected = {k: want.get(k, 0) for k in errors}
    check(name, errors == expected, f"got {errors}" if errors != expected else
          ", ".join(f"{k}+{v}" for k, v in want.items()) or "no errors")


def main():
    g = Guest()
    s = sim(rate=50)
    try:
        g.login()
        drv = Driver(g)

        print("binding")
        check("driver bound via modalias coldplug",
              drv.g.run(f"basename $(readlink {DEV}/driver)") == "mcb_bridge")
        check("probe message", "mcu-bridge at 115200 baud" in g.run("dmesg | grep mcb_bridge"))
        check("ttyAMA1 owned by serdev (no tty node)", "No such file" in g.run("ls /dev/ttyAMA1 2>&1"))

        print("data")
        c0 = drv.counters()
        time.sleep(1)
        c1 = drv.counters()
        rate = c1["rx_frames"] - c0["rx_frames"]
        check("frames at ~50 Hz", 40 <= rate <= 60, f"{rate}/s")
        check("link_up", drv.read("link_up") == "1")
        ch = drv.read("channels").split()
        check("4 channels", len(ch) == 4, " ".join(ch))
        seq1 = int(drv.read("sample_seq"))
        time.sleep(0.2)
        check("sample_seq advances", int(drv.read("sample_seq")) > seq1)

        print("fault injection (exact counter deltas)")
        expect_delta("corrupt 5", delta_after(drv, lambda: s.send("corrupt 5")),
                     crc_errors=5, dropped_frames=5)
        expect_delta("drop 7", delta_after(drv, lambda: s.send("drop 7")), dropped_frames=7)
        expect_delta("garbage 200", delta_after(drv, lambda: s.send("garbage 200")),
                     overruns=1, dropped_frames=1)
        expect_delta("reset", delta_after(drv, lambda: s.send("reset")), resets=1, overruns=1)
        check("HELLO: fw_version", drv.read("fw_version") == "0.1.0", drv.read("fw_version"))
        check("HELLO: rate", drv.read("rate") == "50", drv.read("rate"))
        s.drain()
        expect_delta("unplug 1 (HELLO lost, boot_id)", delta_after(
            drv, lambda: (s.send("unplug 1"), s.wait_for(lambda l: '"ready"' in l, 10)),
            settle=0.5), resets=1)

        print("commands")
        check("ping", drv.write("ping", 1) == "")
        rtt = drv.read("ping_rtt_us")
        check("ping_rtt_us", rtt.isdigit() and 0 < int(rtt) < 500000, f"{rtt} us")
        check("rate 20 accepted", drv.write("rate", 20) == "")
        check("rate reads back 20", drv.read("rate") == "20")
        c0 = drv.counters()
        time.sleep(1)
        got = drv.counters()["rx_frames"] - c0["rx_frames"]
        check("stream now ~20 Hz", 15 <= got <= 25, f"{got}/s")
        check("rate 0 rejected locally", "Invalid argument" in drv.write("rate", 0))
        check("rate 500 rejected locally", "Invalid argument" in drv.write("rate", 500))

        print("link loss")
        s.drain()
        s.send("unplug 5")
        time.sleep(3.6)
        check("link_up drops after 3 s of silence", drv.read("link_up") == "0")
        t = time.monotonic()
        err = drv.write("ping", 1)
        check("ping times out while unplugged", "timed out" in err, f"{err!r} in {time.monotonic() - t:.2f}s")
        s.wait_for(lambda l: '"ready"' in l, 10)
        time.sleep(0.5)
        check("link_up again after replug", drv.read("link_up") == "1")

        print("module unload / reload under load")
        out = g.run("rmmod mcb_bridge; echo rc=$?")
        check("rmmod while streaming", out.endswith("rc=0"), out)
        check("unbound", "No such file" in g.run(f"ls {DEV}/driver 2>&1"))
        check("remove() logged stats", "removed:" in g.run("dmesg | grep mcb_bridge | tail -1"))
        out = g.run("modprobe mcb_bridge; echo rc=$?")
        check("modprobe", out.endswith("rc=0"), out)
        time.sleep(1)
        c = drv.counters()
        # the unplug rebooted the sim, so it is back at its boot rate of 50 Hz
        check("rebound with fresh counters, data flowing", 30 <= c["rx_frames"] <= 70,
              f"rx_frames={c['rx_frames']}")

        print("kernel log")
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops|refcount_t:' || true")
        check("no BUG/WARNING/Oops in dmesg", not bad, bad[:300])
    finally:
        s.stop()
        g.close()
    finish()


if __name__ == "__main__":
    main()
