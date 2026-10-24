#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Daemon test: mcbd started at boot, against the simulated ESP32.

Checks the sample CSV (rate, contiguous seq, age), exact sample accounting
(rows + missed_by_reader == driver sample count delta) while the daemon is
stopped and at 200 Hz, CSV rotation and clean shutdown. How the daemon reacts
to link faults is in tests/test_faults.py.
"""
import os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish, sim
from qemu_guest import Guest

from mcbd_guest import CSV, DEV, csv_growth, csv_rows, mcbd_log, status, wait_log


def main():
    s = sim(rate=50)
    g = Guest()
    try:
        g.login()
        boot_checks(g)
        accounting_checks(g)
        rotation_checks(g)
        stop_checks(g)
        bad = g.run("dmesg | grep -E 'BUG:|WARNING:|Oops|refcount_t:' || true")
        check("no BUG/WARNING/Oops in dmesg", not bad, bad[:300])
    finally:
        s.stop()
        g.close()

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

    n = csv_growth(g, 2)
    check("CSV at ~50 rows/s", 80 <= n <= 120, f"{n / 2:.0f}/s")
    rows = csv_rows(g, 50)
    gaps = [b["seq"] - a["seq"] for a, b in zip(rows, rows[1:]) if b["seq"] - a["seq"] != 1]
    check("seq contiguous over 50 rows", not gaps, str(gaps[:5]))
    ages = sorted(r["age_us"] for r in rows)
    check("median sample age < 20 ms", ages[len(ages) // 2] < 20000, f"{ages[len(ages) // 2]} us")
    check("4 values per row", all(len(r["values"]) == 4 for r in rows))


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
    n = csv_growth(g, 2)
    check("200 Hz: ~200 rows/s", 300 <= n <= 440, f"{n / 2:.0f}/s")
    a = status(g)
    time.sleep(2)
    b = status(g)
    rows = b["rows"] - a["rows"]
    check("200 Hz: accounting exact", rows + b["missed"] - a["missed"] == b["count"] - a["count"],
          f"rows {rows} + missed {b['missed'] - a['missed']} vs count {b['count'] - a['count']}")
    check("rate change picked up", b["rate"] == 200, str(b["rate"]))
    g.run(f"echo 50 > {DEV}/rate")


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


if __name__ == "__main__":
    main()
