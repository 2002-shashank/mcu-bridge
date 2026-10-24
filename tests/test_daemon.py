#!/usr/bin/python3
"""Daemon test: mcbd started at boot, against the simulated ESP32.

Checks the sample CSV (rate, contiguous seq, age), the stale/link-down event
order on an unplug, recovery after replug and module reload, exact sample
accounting (rows + missed_by_reader == driver sample count delta) while the
daemon is stopped, CSV rotation, clean shutdown, and a boot without the DT node.
"""
import os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, sim
from qemu_guest import Guest

DEV = "/sys/bus/serial/devices/serial0-0"
CSV = "/var/log/mcbd/samples.csv"
STATUS = re.compile(r"status: device (\S+), link (\w+), sample (\w+) seq (\d+) count (\d+) "
                    r"age (-?\d+) ms, rate (\d+) Hz, rows (\d+), missed_by_reader (\d+)")


def mcbd_log(g, pattern=""):
    """mcbd lines from syslog (newest last), optionally filtered by a grep -E pattern."""
    out = g.run(f"grep 'mcbd\\[' /var/log/messages" + (f" | grep -E '{pattern}'" if pattern else "")
                + " || true")
    return out.splitlines()


def wait_log(g, pattern, timeout, after=0):
    """Wait until more than `after` lines match `pattern`; returns the new lines."""
    end = time.monotonic() + timeout
    while True:
        lines = mcbd_log(g, pattern)
        if len(lines) > after or time.monotonic() > end:
            return lines[after:]
        time.sleep(0.3)


def status(g):
    n = len(mcbd_log(g, "status:"))
    g.run("kill -USR1 $(cat /var/run/mcbd.pid)")
    lines = wait_log(g, "status:", 3, after=n)
    m = STATUS.search(lines[-1]) if lines else None
    if not m:
        raise RuntimeError(f"no status line: {lines}")
    dev, link, state, seq, count, age, rate, rows, missed = m.groups()
    return dict(dev=dev, link=link, state=state, seq=int(seq), count=int(count), age=int(age),
                rate=int(rate), rows=int(rows), missed=int(missed))


def csv_rows(g, n):
    rows = []
    for line in g.run(f"tail -n {n} {CSV}").splitlines():
        f = line.split(",")
        rows.append(dict(mono_ns=int(f[0]), boot_id=int(f[1]), seq=int(f[2]), age_us=int(f[4]),
                         values=f[5].split()))
    return rows


def main():
    s = sim(rate=50)
    g = Guest()
    try:
        g.login()
        boot_checks(g)
        unplug_checks(g, s)
        accounting_checks(g)
        reload_checks(g)
        rotation_checks(g)
        stop_checks(g)
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops|refcount_t:' || true")
        check("no BUG/WARNING/Oops in dmesg", not bad, bad[:300])
    finally:
        s.stop()
        g.close()

    no_node_checks()
    finish()


def boot_checks(g):
    print("boot")
    check("mcbd running from S50mcbd", g.run("pidof mcbd") == g.run("cat /var/run/mcbd.pid"))
    up = wait_log(g, "link up", 5)
    check("device found, link up logged", bool(up), up[0] if up else str(mcbd_log(g)))

    fields = g.run(f"cat {DEV}/sample").split()
    check("driver: sample has boot_id seq ts rx_ns count + 4 channels", len(fields) == 9,
          " ".join(fields))
    up_ns = float(g.run("cut -d' ' -f1 /proc/uptime")) * 1e9
    check("driver: rx_ns is CLOCK_MONOTONIC (within 1 s of uptime)",
          abs(int(fields[3]) - up_ns) < 1e9, f"{int(fields[3])} vs {up_ns:.0f}")

    n0 = int(g.run(f"wc -l < {CSV}"))
    time.sleep(2)
    n1 = int(g.run(f"wc -l < {CSV}"))
    check("CSV at ~50 rows/s", 80 <= n1 - n0 <= 120, f"{(n1 - n0) / 2:.0f}/s")
    rows = csv_rows(g, 50)
    gaps = [b["seq"] - a["seq"] for a, b in zip(rows, rows[1:]) if b["seq"] - a["seq"] != 1]
    check("seq contiguous over 50 rows", not gaps, str(gaps[:5]))
    ages = sorted(r["age_us"] for r in rows)
    check("median sample age < 20 ms", ages[len(ages) // 2] < 20000, f"{ages[len(ages) // 2]} us")
    check("4 values per row", all(len(r["values"]) == 4 for r in rows))


def unplug_checks(g, s):
    print("unplug: stale before link down, never shown fresh")
    n_stale, n_down, n_up = (len(mcbd_log(g, p)) for p in ("stale", "link down", "link up"))
    rows_before = int(g.run(f"wc -l < {CSV}"))
    s.drain()
    s.send("unplug 5")
    stale = wait_log(g, "stale", 3, after=n_stale)
    down = wait_log(g, "link down", 6, after=n_down)
    st = status(g)
    check("status while unplugged: link down, sample stale", (st["link"], st["state"]) ==
          ("down", "stale"), str(st))
    m = re.search(r"is (\d+) ms old \(limit (\d+) ms\)", stale[0]) if stale else None
    check("stale event within ~limit of the last sample",
          m and int(m.group(2)) <= int(m.group(1)) < int(m.group(2)) + 300,
          stale[0] if stale else "no stale event")
    m = re.search(r"is (\d+) ms old", down[0]) if down else None
    check("link down event after ~3 s of silence (driver notify)",
          m and 2900 <= int(m.group(1)) <= 3800, down[0] if down else "no link down event")
    order = mcbd_log(g, "stale|link down")[n_stale + n_down:]
    check("stale logged before link down", len(order) >= 2 and "stale" in order[0], str(order))

    s.wait_for(lambda l: '"ready"' in l, 10)
    up = wait_log(g, "link up", 5, after=n_up)
    check("link up after replug", bool(up))
    check("peer reboot detected (boot_id)", bool(wait_log(g, "peer rebooted", 3)))
    check("samples fresh again", bool(wait_log(g, "fresh again", 3)))
    time.sleep(0.5)
    rows = csv_rows(g, int(g.run(f"wc -l < {CSV}")) - rows_before)
    late = [r for r in rows if r["age_us"] > 100000]
    check("no CSV row written for a stale sample", not late, str(late[:2]))


def accounting_checks(g):
    print("reader falls behind: missed_by_reader")
    drops0 = int(g.run(f"cat {DEV}/dropped_frames"))
    a = status(g)
    g.run("kill -STOP $(cat /var/run/mcbd.pid); sleep 1; kill -CONT $(cat /var/run/mcbd.pid)")
    time.sleep(0.5)
    b = status(g)
    missed = b["missed"] - a["missed"]
    check("~50 samples missed while stopped 1 s", 35 <= missed <= 65, str(missed))
    check("rows + missed_by_reader == driver sample count delta (exact)",
          (b["rows"] - a["rows"]) + missed == b["count"] - a["count"],
          f"rows {b['rows'] - a['rows']} + missed {missed} vs count {b['count'] - a['count']}")
    check("wire drops counted separately (dropped_frames unchanged)",
          int(g.run(f"cat {DEV}/dropped_frames")) == drops0)
    lines = wait_log(g, "counters:.*missed_by_reader", 12)
    check("counter report logs missed_by_reader", bool(lines), lines[-1] if lines else "")

    g.run(f"echo 200 > {DEV}/rate")
    time.sleep(0.5)
    a = status(g)
    time.sleep(2)
    b = status(g)
    rows = b["rows"] - a["rows"]
    check("200 Hz: ~200 rows/s", 300 <= rows <= 440, f"{rows / 2:.0f}/s, missed {b['missed'] - a['missed']}")
    check("200 Hz: accounting exact", rows + b["missed"] - a["missed"] == b["count"] - a["count"])
    check("rate change picked up", b["rate"] == 200, str(b["rate"]))
    g.run(f"echo 50 > {DEV}/rate")


def reload_checks(g):
    print("module unload / reload")
    found_rx = "device /sys.*, link"
    n_gone, n_dev = len(mcbd_log(g, "gone")), len(mcbd_log(g, found_rx))
    g.run("rmmod mcb_bridge")
    check("device gone logged", bool(wait_log(g, "gone", 3, after=n_gone)))
    check("still running without a device", g.run("pidof mcbd") != "")
    st = status(g)
    check("status: device none", st["dev"] == "none", str(st))
    g.run("modprobe mcb_bridge")
    found = wait_log(g, found_rx, 4, after=n_dev)
    check("device found again after modprobe", bool(found), found[-1] if found else "")
    time.sleep(0.5)
    st = status(g)
    check("link up and samples fresh after reload", (st["link"], st["state"]) == ("up", "fresh"),
          str(st))
    rows0 = int(g.run(f"wc -l < {CSV}"))
    time.sleep(1)
    check("rows flowing after reload", int(g.run(f"wc -l < {CSV}")) - rows0 >= 30)


def rotation_checks(g):
    print("CSV rotation")
    g.run("mcbd -o /tmp/rot/s.csv -m 2000 & echo $! > /tmp/rot.pid; sleep 2; "
          "kill $(cat /tmp/rot.pid)")
    sizes = g.run("wc -c /tmp/rot/s.csv /tmp/rot/s.csv.1 2>&1")
    m = re.findall(r"(\d+) /tmp/rot/s\.csv(\.1)?\n", sizes + "\n")
    check("rotated to .1, both under the cap + one row",
          len(m) == 2 and all(int(n) < 2100 for n, _ in m), sizes)


def stop_checks(g):
    print("shutdown")
    out = g.run("/etc/init.d/S50mcbd stop")
    check("S50mcbd stop", out.endswith("OK"), out)
    check("exit logged", bool(wait_log(g, "exiting on signal 15", 3)))
    check("pidfile removed", "No such file" in g.run("ls /var/run/mcbd.pid 2>&1"))
    out = g.run("/etc/init.d/S50mcbd start; sleep 1; pidof mcbd")
    check("S50mcbd start again", out.splitlines()[-1].isdigit(), out)


def no_node_checks():
    print("boot without the DT node")
    g = Guest(env={"DT_OVERLAY": "none"})
    try:
        g.login()
        time.sleep(1.5)
        check("mcbd running with no device", g.run("pidof mcbd") != "")
        log = mcbd_log(g)
        check("no device found, no errors", log and "started" in log[0] and
              not any("device" in l or "err" in l.lower() for l in log), str(log))
    finally:
        g.close()


if __name__ == "__main__":
    main()
