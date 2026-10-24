#!/usr/bin/python3
"""Measure PING round-trip latency at three points of the path (docs/latency.md).

    scripts/measure-ping.py direct [-n N] [--rate HZ] [--token BYTES]
        host <-> ESP32 over USB serial, no QEMU: CH340 + UART wire + firmware
    scripts/measure-ping.py guest {sim,real} [-n N] [--rate HZ] [--accel tcg|hvf] [--busy]
        the driver's ping_rtt_us in the guest; with "real", esp32-bridge.py --trace
        also times each PING/PONG at the tty, splitting guest + QEMU + bridge off.
        --busy spins a shell loop per guest CPU, so no vCPU is idle (WFI) when the
        response arrives: separates vCPU wake-up cost from the rest
    scripts/measure-ping.py matrix [-n N]
        all of the above (real board needed), JSON to out/latency.json, markdown table

Pings are spaced by a random 0..20 ms so they do not lock to the 50 Hz DATA
stream's phase (a PONG may queue behind a DATA frame being transmitted).
"""
import argparse, json, math, os, random, re, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(REPO, "protocol"), os.path.join(REPO, "tests")]
import mcb_proto as P

TTY = "/dev/cu.usbserial-110"
DEV = "/sys/bus/serial/devices/serial0-0"


def summary(us):
    s = sorted(us)
    pct = lambda p: s[max(0, math.ceil(p / 100 * len(s)) - 1)]
    return dict(n=len(s), min=s[0], p50=pct(50), p90=pct(90), p99=pct(99), max=s[-1],
                mean=round(statistics.fmean(s)))


def direct(n, rate, token, tty=TTY):
    from esp32_board import Board
    b = Board(tty)
    try:
        if b.set_rate(rate) != P.STATUS_OK:
            raise RuntimeError(f"SET_RATE {rate} failed")
        b.collect(0.3)
        rtts, lost = [], 0
        for i in range(n):
            resp, ms = b.request(P.TYPE_PING, bytes([i & 0xFF]) * token)
            if resp is None:
                lost += 1
            else:
                rtts.append(round(ms * 1000))
            time.sleep(random.uniform(0, 0.02))
        b.set_rate(50)
        return dict(rtt_us=rtts, lost=lost)
    finally:
        os.close(b.fd)


def guest(link, n, rate, accel, busy=False):
    from harness import Proc, esp32_socket, sim
    from qemu_guest import Guest
    if link == "real":
        s = Proc([os.path.join(HERE, "esp32-bridge.py"), "--trace", "--socket", esp32_socket()],
                 "stderr")
    else:
        s = sim(rate=rate)
    g = Guest(env={"ACCEL": accel})
    try:
        g.login()
        if link == "real" and not s.wait_for(lambda l: "QEMU connected" in l, 10):
            raise RuntimeError("bridge did not connect")
        time.sleep(1)
        if g.run(f"echo {rate} > {DEV}/rate && echo OK") != "OK":
            raise RuntimeError(f"rate {rate} rejected")
        if busy:
            g.run("for c in $(seq $(nproc)); do (while :; do :; done) & done")
        time.sleep(0.3)
        s.drain()
        g.run(f": > /tmp/rtt; i=0; while [ $i -lt {n} ]; do "
              f"echo 1 > {DEV}/ping 2>/dev/null && cat {DEV}/ping_rtt_us >> /tmp/rtt; "
              f"usleep $((RANDOM % 20000)); i=$((i+1)); done", timeout=30 + n * 0.2)
        rtts = [int(v) for v in g.run("cat /tmp/rtt").split()]
        out = dict(rtt_us=rtts, lost=n - len(rtts))
        if link == "real":
            tty = []
            s.wait_for(lambda l: bool(m := re.search(r"trace pong rtt_us=(\d+)", l))
                       and tty.append(int(m.group(1))) and False, 0.5)
            out["tty_rtt_us"] = tty
        return out
    finally:
        s.stop()
        g.close()


def fmt_row(name, r, key="rtt_us"):
    s = summary(r[key])
    return (f"| {name} | {s['n']} | {s['min'] / 1000:.2f} | {s['p50'] / 1000:.2f} | "
            f"{s['p90'] / 1000:.2f} | {s['p99'] / 1000:.2f} | {s['max'] / 1000:.2f} | {r['lost']} |")


def matrix(n):
    runs = {}
    for rate in (1, 50):
        for tok in (0, 8):
            if rate == 50 and tok == 0:
                continue
            print(f"direct rate={rate} token={tok}", flush=True)
            runs[f"direct, {rate} Hz, {tok} B token"] = direct(n, rate, tok)
    for accel in ("tcg", "hvf"):
        print(f"guest sim {accel}", flush=True)
        runs[f"guest sim, {accel}"] = guest("sim", n, 50, accel)
        for rate in (1, 50):
            print(f"guest real {accel} rate={rate}", flush=True)
            runs[f"guest real, {accel}, {rate} Hz"] = guest("real", n, rate, accel)
    os.makedirs(os.path.join(REPO, "out"), exist_ok=True)
    with open(os.path.join(REPO, "out", "latency.json"), "w") as f:
        json.dump(runs, f)

    print("\n| path | n | min | p50 | p90 | p99 | max | lost |")
    print("|---|---|---|---|---|---|---|---|")
    for name, r in runs.items():
        print(fmt_row(name, r))
        if "tty_rtt_us" in r and r["tty_rtt_us"]:
            print(fmt_row(f"&nbsp;&nbsp;↳ bridge tty trace of the same pings", r, "tty_rtt_us"))
    print("(milliseconds)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("direct")
    d.add_argument("--token", type=int, default=8)
    g = sub.add_parser("guest")
    g.add_argument("link", choices=("sim", "real"))
    g.add_argument("--accel", default=os.environ.get("ACCEL", "tcg"))
    g.add_argument("--busy", action="store_true")
    m = sub.add_parser("matrix")
    for p in (d, g, m):
        p.add_argument("-n", type=int, default=300)
    for p in (d, g):
        p.add_argument("--rate", type=int, default=50)
    a = ap.parse_args()

    if a.cmd == "matrix":
        matrix(a.n)
        return
    r = direct(a.n, a.rate, a.token) if a.cmd == "direct" else guest(a.link, a.n, a.rate, a.accel, a.busy)
    print(json.dumps({k: summary(v) if isinstance(v, list) and v else v for k, v in r.items()}))


if __name__ == "__main__":
    main()
