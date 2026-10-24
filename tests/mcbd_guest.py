"""Guest-side helpers for tests that look at mcbd: its syslog lines, its SIGUSR1
status line and its sample CSV."""
import re, time

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


def csv_growth(g, secs):
    """CSV rows added over `secs` of guest time (one guest command: console round
    trips, slow on a KASAN kernel under TCG, are not counted)."""
    a, b = g.run(f"a=$(wc -l < {CSV}); sleep {secs}; echo $a $(wc -l < {CSV})").split()
    return int(b) - int(a)


def csv_rows(g, n):
    rows = []
    for line in g.run(f"tail -n {n} {CSV}").splitlines():
        f = line.split(",")
        rows.append(dict(mono_ns=int(f[0]), boot_id=int(f[1]), seq=int(f[2]), age_us=int(f[4]),
                         values=f[5].split()))
    return rows
