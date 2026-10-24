#!/usr/bin/python3
"""Driver test: guest with the DT overlay (mcb_bridge binds via coldplug) talking
to the simulated ESP32, or with --real to the ESP32 firmware through the bridge.

Binding, data, counters, PING and SET_RATE. Injected faults (corruption, drops,
resets, unplugs, module reload) are in tests/test_faults.py; --real also runs
a reset pulse and a module reload against the real board end to end.
"""
import argparse, os, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, Proc, check, esp32_socket, finish, sim
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

    def delta(self, attr, secs=1):
        """Change of a counter over `secs` of guest time. One guest command, so the
        console round trips (slow on a KASAN kernel under TCG) are not counted."""
        a, b = self.g.run(f"a=$(cat {DEV}/{attr}); sleep {secs}; echo $a $(cat {DEV}/{attr})").split()
        return int(b) - int(a)

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
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="real ESP32 via scripts/esp32-bridge.py")
    real = ap.parse_args().real

    g = Guest()
    if real:
        s = Proc([os.path.join(ROOT, "scripts", "esp32-bridge.py"), "--socket", esp32_socket()],
                 "stderr")
        s.log_to(os.path.join(ROOT, "out", "bridge.log"))
    else:
        s = sim(rate=50)
    try:
        g.login()
        drv = Driver(g)
        if real:
            check("bridge connected", s.wait_for(lambda l: "QEMU connected" in l, 10))
            time.sleep(1)

        print("binding")
        check("driver bound via modalias coldplug",
              drv.g.run(f"basename $(readlink {DEV}/driver)") == "mcb_bridge")
        check("probe message", "mcu-bridge at 115200 baud" in g.run("dmesg | grep mcb_bridge"))
        check("ttyAMA1 owned by serdev (no tty node)", "No such file" in g.run("ls /dev/ttyAMA1 2>&1"))

        print("data")
        rate = drv.delta("rx_frames")
        check("frames at ~50 Hz", 40 <= rate <= 60, f"{rate}/s")
        check("link_up", drv.read("link_up") == "1")
        ch = drv.read("channels").split()
        check("4 channels", len(ch) == 4, " ".join(ch))
        seq1 = int(drv.read("sample_seq"))
        time.sleep(0.2)
        check("sample_seq advances", int(drv.read("sample_seq")) > seq1)

        if real:
            real_board_checks(drv, s)

        print("commands")
        check("ping", drv.write("ping", 1) == "")
        rtt = drv.read("ping_rtt_us")
        check("ping_rtt_us", rtt.isdigit() and 0 < int(rtt) < 500000, f"{rtt} us")
        check("rate 20 accepted", drv.write("rate", 20) == "")
        check("rate reads back 20", drv.read("rate") == "20")
        got = drv.delta("rx_frames")
        check("stream now ~20 Hz", 15 <= got <= 25, f"{got}/s")
        check("rate 0 rejected locally", "Invalid argument" in drv.write("rate", 0))
        check("rate 500 rejected locally", "Invalid argument" in drv.write("rate", 500))

        check("rate back to 50", drv.write("rate", 50) == "")
        if real:
            module_reload_checks(g, drv)

        print("kernel log")
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops|refcount_t:' || true")
        check("no BUG/WARNING/Oops in dmesg", not bad, bad[:300])
    finally:
        s.stop()
        g.close()
    finish()


def real_board_checks(drv, bridge):
    print("real board")
    d = delta_after(drv, lambda: (bridge.drain(), bridge.p.send_signal(signal.SIGUSR1),
                                  bridge.wait_for(lambda l: "reset pulsed" in l, 5)), settle=1.5)
    expect_delta("reset pulse: ROM text + HELLO", d, resets=1, overruns=1)
    check("HELLO: fw_version 1.0.0 (real firmware)", drv.read("fw_version") == "1.0.0",
          drv.read("fw_version"))
    check("HELLO: rate 50", drv.read("rate") == "50", drv.read("rate"))
    ch = drv.read("channels").split()
    check("untouched channels: touched=0 x=-1 y=-1 pressure=0", ch == ["0", "-1", "-1", "0"], " ".join(ch))
    c0 = drv.counters()
    time.sleep(3)
    d = {k: v - c0[k] for k, v in drv.counters().items()}
    check("3 s at 50 Hz with no errors or drops",
          140 <= d["rx_frames"] <= 160 and not any(v for k, v in d.items() if k != "rx_frames"), str(d))


def module_reload_checks(g, drv):
    print("module unload / reload under load")
    before = drv.counters()["rx_frames"]
    out = g.run("rmmod mcb_bridge; echo rc=$?")
    check("rmmod while streaming", out.endswith("rc=0"), out)
    check("unbound", "No such file" in g.run(f"ls {DEV}/driver 2>&1"))
    check("remove() logged stats", "removed:" in g.run("dmesg | grep mcb_bridge | tail -1"))
    out = g.run("modprobe mcb_bridge; echo rc=$?")
    check("modprobe", out.endswith("rc=0"), out)
    time.sleep(1)
    fresh = drv.counters()["rx_frames"]
    check("rebound with fresh counters", 0 < fresh < before, f"rx_frames {fresh} (was {before})")
    # rate is 50 again: the sim rebooted on unplug, the real board was set back
    rate = drv.delta("rx_frames")
    check("data flowing at ~50 Hz after rebind", 40 <= rate <= 60, f"{rate}/s")


if __name__ == "__main__":
    main()
