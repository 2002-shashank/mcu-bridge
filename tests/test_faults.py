#!/usr/bin/python3
"""Fault-test suite: inject one fault at a time and check every layer at once.

    tests/test_faults.py                  simulated ESP32: every fault below
    tests/test_faults.py --real           real board via esp32-bridge.py: reset pulse, module reload
    tests/test_faults.py --real --unplug  ... and a physical unplug: a person pulls the USB
                                          cable when told, and plugs it back ~5 s later

For each fault:
  driver   exact counter deltas, zero for every counter that must not move
  link     whether the link went down (driver link_up edge, seen through mcbd)
  daemon   the mcbd events that must appear, in order, and those that must not
  outage   longest gap between consecutive samples the consumer got, from the
           daemon's CSV in guest time (receive time = read time - age)
then that the system recovered (link up, samples fresh) before the next fault.
A boot without the DT node is the last case. The matrix is printed as markdown
(docs/faults.md; scripts/ci.sh adds it to the CI summary).
"""
import argparse, os, re, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Proc, check, esp32_socket, finish, sim
from mcbd_guest import CSV, DEV, mcbd_log, status
from qemu_guest import Guest

COUNTERS = ("rx_frames", "crc_errors", "framing_errors", "overruns", "dropped_frames", "resets")
EVENTS = {  # mcbd log regex (anchored at the message start) -> short name in the matrix
    r"\]: sample stale": "stale",
    r"\]: link down": "link down",
    r"\]: link up": "link up",
    r"\]: peer rebooted": "peer reboot",
    r"\]: samples fresh again": "fresh",
    r"\]: device \S+ gone": "device gone",
    r"\]: device /sys\S+, link": "device found",
}
rows = []


class Fault:
    def __init__(self, name, inject, want=None, events=(), forbid=(), outage=(0, 60),
                 allow=(), link_down=False):
        self.name, self.inject = name, inject
        self.want = want            # counter deltas; None: counters are reset (module reload)
        self.events, self.forbid = events, forbid
        self.outage, self.allow, self.link_down = outage, allow, link_down


def counters(g):
    vals = g.run(f"cd {DEV} && cat {' '.join(COUNTERS)}").split()
    return dict(zip(COUNTERS, map(int, vals)))


def csv_len(g):
    return int(g.run(f"wc -l < {CSV}"))


def outage_ms(g, first_line):
    """Longest gap between receive times of the CSV rows from `first_line` on."""
    rx = []
    for line in g.run(f"sed -n '{first_line},$p' {CSV} | cut -d, -f1,5").split():
        mono_ns, age_us = map(int, line.split(","))
        rx.append(mono_ns - age_us * 1000)
    return max((b - a for a, b in zip(rx, rx[1:])), default=0) / 1e6


def events_of(lines):
    """Short names of the known events in `lines`, in order of appearance."""
    seen = []
    for line in lines:
        for rx, name in EVENTS.items():
            if re.search(rx, line):
                seen.append(name)
    return seen


def in_order(seen, want):
    it = iter(seen)
    return all(w in it for w in want)  # each wanted event after the previous one


def wait_recovered(g, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = status(g)
        if st["link"] == "up" and st["state"] == "fresh":
            return st
        time.sleep(0.5)
    return status(g)


def run_fault(g, f):
    print(f"fault: {f.name}", flush=True)
    c0 = counters(g) if f.want is not None else None
    log0, line0 = len(mcbd_log(g)), csv_len(g)
    f.inject()
    time.sleep(0.6)  # the simulator applies a fault to its next frames
    st = wait_recovered(g, 15)
    new = [l for l in mcbd_log(g)[log0:] if "status:" not in l]
    seen = events_of(new)
    gap = outage_ms(g, line0)
    c1 = counters(g)
    ok = []

    if f.want is None:
        moved = {k: v for k, v in c1.items() if k != "rx_frames" and v}
        ok.append(check(f"{f.name}: counters reset on rebind, no errors since",
                        0 < c1["rx_frames"] < 1000 and not moved, str(c1)))
        delta_txt = "reset to 0"
    else:
        d = {k: c1[k] - c0[k] for k in COUNTERS if k != "rx_frames"}
        expected = {k: f.want.get(k, 0) for k in d}
        got = {k: v for k, v in d.items() if k not in f.allow}
        want = {k: v for k, v in expected.items() if k not in f.allow}
        ok.append(check(f"{f.name}: exact counter deltas", got == want,
                        f"got {d}" if got != want else ""))
        delta_txt = ", ".join(f"{k} +{v}" for k, v in d.items() if v) or "none"
    ok.append(check(f"{f.name}: daemon events {list(f.events)} in order", in_order(seen, f.events),
                    str(seen)))
    bad = [e for e in f.forbid if e in seen]
    ok.append(check(f"{f.name}: no {list(f.forbid)}", not bad, str(seen)))
    ok.append(check(f"{f.name}: link {'went down' if f.link_down else 'stayed up'}",
                    ("link down" in seen) == f.link_down, str(seen)))
    lo, hi = f.outage
    ok.append(check(f"{f.name}: consumer outage {lo}..{hi} ms", lo <= gap <= hi, f"{gap:.0f} ms"))
    ok.append(check(f"{f.name}: recovered (link up, samples fresh)",
                    st["link"] == "up" and st["state"] == "fresh", str(st)))
    rows.append(f"| {f.name} | {delta_txt} | {'yes' if 'link down' in seen else 'no'} | "
                f"{', '.join(seen) or '—'} | {gap:.0f} | {'PASS' if all(ok) else 'FAIL'} |")


def sim_faults(g, s):
    def sim_cmd(cmd, wait_ready=False):
        def inject():
            s.drain()
            s.send(cmd)
            if wait_ready:
                s.wait_for(lambda l: '"ready"' in l, 15)
        return inject

    quiet = ("stale", "link down", "peer reboot")
    # Outage bounds test the stale policy (limit 100 ms at 50 Hz), not the host's speed:
    # below 100 ms where no stale event is allowed, from 100 ms where one is required,
    # with headroom above for a slow (KASAN, TCG) guest that delivers frames in bunches.
    return [
        Fault("1 corrupted frame", sim_cmd("corrupt 1"), dict(crc_errors=1, dropped_frames=1),
              forbid=quiet, outage=(30, 99)),
        Fault("5 corrupted frames in a row", sim_cmd("corrupt 5"),
              dict(crc_errors=5, dropped_frames=5), events=("stale", "fresh"),
              forbid=("link down", "peer reboot"), outage=(100, 250)),
        Fault("7 frames lost on the wire", sim_cmd("drop 7"), dict(dropped_frames=7),
              events=("stale", "fresh"), forbid=("link down", "peer reboot"), outage=(140, 300)),
        Fault("line noise, 200 bytes", sim_cmd("garbage 200"), dict(overruns=1, dropped_frames=1),
              forbid=quiet, outage=(30, 99)),
        Fault("ESP32 reset", sim_cmd("reset", wait_ready=True), dict(resets=1, overruns=1),
              events=("peer reboot",), forbid=("link down", "stale"), outage=(0, 99)),
        Fault("cable unplug 1 s (< 3 s link timeout)", sim_cmd("unplug 1", wait_ready=True),
              dict(resets=1), events=("stale", "peer reboot", "fresh"), forbid=("link down",),
              outage=(1000, 1800)),
        Fault("cable unplug 5 s", sim_cmd("unplug 5", wait_ready=True), dict(resets=1),
              events=("stale", "link down", "link up", "peer reboot", "fresh"),
              outage=(5000, 6000), link_down=True),
        reload_fault(g),
    ]


def reload_fault(g):
    return Fault("module unload + reload while streaming",
                 lambda: g.run("rmmod mcb_bridge; sleep 1; modprobe mcb_bridge"),
                 want=None, events=("device gone", "device found"), forbid=("peer reboot",),
                 outage=(1000, 4000))


def real_faults(g, bridge, unplug):
    def reset():
        bridge.drain()
        bridge.p.send_signal(signal.SIGUSR1)
        bridge.wait_for(lambda l: "reset pulsed" in l, 5)

    def physical_unplug():
        bridge.drain()
        print("\n>>> UNPLUG the ESP32's USB cable now, wait ~5 s, then plug it back in <<<\n",
              flush=True)
        bridge.wait_for(lambda l: "ESP32 disconnected" in l, 60)
        bridge.wait_for(lambda l: "ESP32 connected" in l, 60)

    faults = [
        # ROM boot text arrives as one overrun before the 0x00 that precedes HELLO
        Fault("ESP32 reset (RTS pulse)", reset, dict(resets=1, overruns=1),
              events=("peer reboot",), forbid=("link down",), outage=(50, 1500)),
        reload_fault(g),
    ]
    if unplug:
        # a frame cut off mid-way at the unplug may show up as any one receive error
        faults.append(Fault("physical cable unplug ~5 s", physical_unplug, dict(resets=1),
                            events=("stale", "link down", "link up", "peer reboot", "fresh"),
                            allow=("crc_errors", "framing_errors", "overruns", "dropped_frames"),
                            outage=(3000, 60000), link_down=True))
    return faults


def no_node_case():
    print("fault: missing DT node", flush=True)
    g = Guest(env={"DT_OVERLAY": "none"})
    try:
        g.login()
        time.sleep(1.5)
        ok = [
            check("missing DT node: ttyAMA1 stays a plain tty", g.run("ls /dev/ttyAMA1 2>&1") ==
                  "/dev/ttyAMA1"),
            check("missing DT node: no serdev device", "No such file" in g.run(f"ls {DEV} 2>&1")),
            check("missing DT node: module not loaded (no modalias)",
                  "mcb_bridge" not in g.run("lsmod")),
        ]
        out = g.run("modprobe mcb_bridge; echo rc=$?; sleep 1.5")
        ok.append(check("missing DT node: manual modprobe binds nothing",
                        out.endswith("rc=0") and "No such file" in g.run(f"ls {DEV} 2>&1"), out))
        log = mcbd_log(g)
        ok.append(check("missing DT node: mcbd running, searching, no errors",
                        g.run("pidof mcbd") != "" and log and "started" in log[0]
                        and not events_of(log) and not any("err" in l.lower() for l in log),
                        str(log)))
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops' || true")
        ok.append(check("missing DT node: no BUG/WARNING/Oops", not bad, bad[:200]))
        rows.append(f"| missing DT node (boot without overlay) | no device | — | none (searching) "
                    f"| — | {'PASS' if all(ok) else 'FAIL'} |")
    finally:
        g.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="real ESP32 via scripts/esp32-bridge.py")
    ap.add_argument("--unplug", action="store_true", help="with --real: physical unplug step")
    a = ap.parse_args()
    if a.real:
        s = Proc([os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                               "esp32-bridge.py"), "--socket", esp32_socket()], "stderr")
    else:
        s = sim(rate=50)
    g = Guest()
    try:
        g.login()
        if a.real:
            check("bridge connected", s.wait_for(lambda l: "QEMU connected" in l, 10))
        st = wait_recovered(g, 10)
        check("link up and samples fresh before the first fault",
              st["link"] == "up" and st["state"] == "fresh", str(st))
        for f in (real_faults(g, s, a.unplug) if a.real else sim_faults(g, s)):
            run_fault(g, f)
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops|refcount_t:' || true")
        check("no BUG/WARNING/Oops in dmesg after all faults", not bad, bad[:300])
    finally:
        s.stop()
        g.close()
    if not a.real:
        no_node_case()

    print("\n| fault | driver counters | link down | daemon events | outage (ms) | result |")
    print("|---|---|---|---|---|---|")
    print("\n".join(rows))
    finish()


if __name__ == "__main__":
    main()
