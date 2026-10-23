#!/usr/bin/python3
"""Link test without the driver: guest ttyAMA1 is a plain tty (DT_OVERLAY=none).

    tests/test_link.py                 simulated ESP32 (scripts/esp32-sim.py)
    tests/test_link.py --real          real ESP32 via scripts/esp32-bridge.py
    tests/test_link.py --real --unplug   ... and wait for a physical unplug/replug

Simulator: HELLO + gap-free DATA reach the guest, a PING written by the guest
reaches the simulator and its PONG comes back, and an unplug/reconnect is seen
as a fresh boot. Real ESP32 (no mcu-bridge firmware yet): its ROM boot log
reaches the guest after a reset pulse, and again after an unplug/replug (the
bridge reopens the port while QEMU keeps running).
"""
import argparse, json, os, queue, signal, subprocess, sys, threading, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "protocol"))
from qemu_guest import Guest
import mcb_proto as P

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)


class Proc:
    """Child process whose stdout (sim: JSON) or stderr (bridge: log) lines are queued."""

    def __init__(self, argv, stream):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        out = self.p.stdout if stream == "stdout" else self.p.stderr
        threading.Thread(target=lambda: [self.lines.put(l.rstrip()) for l in out], daemon=True).start()

    def log_to(self, path):
        self.log_path = path
        open(path, "w").close()

    def wait_for(self, pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = self.lines.get(timeout=max(0, end - time.monotonic()))
            except queue.Empty:
                break
            if getattr(self, "log_path", None):
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            if pred(line):
                return line
        return None

    def send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def stop(self):
        self.p.kill()
        self.p.wait()
        time.sleep(0.1)
        self.wait_for(lambda l: False, 0.2)  # drain remaining lines into the log


def capture_start(g):
    # raw + no echo before any byte arrives, or the tty bounces frames back
    g.run("stty -F /dev/ttyAMA1 raw -echo; (cat /dev/ttyAMA1 > /tmp/cap &)")


def capture_stop(g, save_as=None):
    g.run("killall cat; sleep 0.1")
    data = bytes.fromhex(g.run("od -An -tx1 -v /tmp/cap | tr -d ' \\n'", timeout=60))
    if save_as:
        with open(os.path.join(ROOT, "out", save_as), "wb") as f:
            f.write(data)
    return data


def guest_write(g, data):
    g.run("printf '" + "".join(f"\\{b:03o}" for b in data) + "' > /dev/ttyAMA1")


def events_after_hello(stream):
    """Receiver events from the last HELLO on (earlier bytes may be a partial frame)."""
    rx, seq, ev = P.Receiver(), P.SeqTracker(), []
    for st, f in rx.feed(stream):
        if st == P.RX_FRAME and f.type == P.TYPE_HELLO:
            ev = []
        ev.append((st, f, seq.update(f) if st == P.RX_FRAME else None))
    return ev


def test_sim(g):
    sim = Proc([os.path.join(ROOT, "scripts", "esp32-sim.py"), "--rate", "50"], "stdout")
    try:
        check("simulator connected", sim.wait_for(lambda l: '"ready"' in l, 10))

        print("stream: reset -> HELLO + DATA, then guest PING -> PONG")
        capture_start(g)
        sim.send("reset")
        time.sleep(2)
        ping = P.frame_encode(P.Frame(P.TYPE_PING, seq=1, payload=b"tok-1234"))
        guest_write(g, b"\x00" + ping)
        cmd = sim.wait_for(lambda l: '"cmd"' in l, 5)
        time.sleep(0.3)
        ev = events_after_hello(capture_stop(g))

        frames = [f for st, f, _ in ev if st == P.RX_FRAME]
        errors = [st for st, _, _ in ev if st != P.RX_FRAME]
        data = [f for f in frames if f.type == P.TYPE_DATA]
        pongs = [f for f in frames if f.type == P.TYPE_PONG]
        check("HELLO received", frames and frames[0].type == P.TYPE_HELLO and frames[0].seq == 0)
        check("DATA frames at ~50 Hz", 80 <= len(data) <= 140, f"{len(data)} in ~2.5 s")
        check("no errors after HELLO", not errors, str(errors))
        check("no gaps", all(lost == (0, False) for st, f, lost in ev[1:] if lost), "")
        check("simulator received PING", cmd and json.loads(cmd)["type"] == P.TYPE_PING
              and json.loads(cmd)["payload"] == b"tok-1234".hex(), cmd or "")
        check("PONG echoes req_seq and token",
              pongs and pongs[0].payload == (1).to_bytes(4, "little") + b"tok-1234")

        print("unplug: board reboots while disconnected, boot output lost")
        capture_start(g)
        time.sleep(0.5)
        sim.send("unplug 1")
        ready = sim.wait_for(lambda l: '"ready"' in l, 10)
        check("simulator reconnected", ready and not json.loads(ready)["visible"])
        time.sleep(0.5)
        rx, seq, frames = P.Receiver(), P.SeqTracker(), []
        for st, f in rx.feed(capture_stop(g)):
            if st == P.RX_FRAME:
                frames.append((f, seq.update(f)))
        resets = [(f, lr) for f, lr in frames[1:] if lr[1]]
        check("exactly one reset, detected without HELLO",
              len(resets) == 1 and resets[0][0].type == P.TYPE_DATA
              and resets[0][0].boot_id == json.loads(ready)["boot_id"], str(len(resets)))
        check("no frames counted as dropped", all(lr[0] == 0 for f, lr in frames[1:]))
    finally:
        sim.stop()


def test_real(g, unplug):
    bridge = Proc([os.path.join(ROOT, "scripts", "esp32-bridge.py")], "stderr")
    bridge.log_to(os.path.join(ROOT, "out", "bridge.log"))
    try:
        check("bridge connected to ESP32 and QEMU",
              bridge.wait_for(lambda l: "ESP32 connected" in l, 10)
              and bridge.wait_for(lambda l: "QEMU connected" in l, 10))

        print("reset pulse via bridge (SIGUSR1) -> ROM boot log in guest")
        capture_start(g)
        bridge.p.send_signal(signal.SIGUSR1)
        bridge.wait_for(lambda l: "reset pulsed" in l, 5)
        time.sleep(1.5)
        cap = capture_stop(g)
        check("ROM boot log received", b"rst:0x1 (POWERON_RESET)" in cap, f"{len(cap)} bytes")

        if unplug:
            # Prompts go to this test's output; wait generously for a human.
            print(">>> UNPLUG the ESP32 USB cable now (waiting up to 300 s)", flush=True)
            check("bridge saw unplug", bridge.wait_for(lambda l: "ESP32 disconnected" in l, 300))
            print(">>> PLUG it back in (waiting up to 300 s)", flush=True)
            check("bridge reopened ESP32", bridge.wait_for(lambda l: "ESP32 connected" in l, 300))
            # The power-on boot log is lost: the ESP32 prints it while the USB
            # serial chip is still enumerating. Prove the reopened port works
            # end to end with a reset pulse instead.
            time.sleep(2)
            capture_start(g)
            bridge.p.send_signal(signal.SIGUSR1)
            bridge.wait_for(lambda l: "reset pulsed" in l, 5)
            time.sleep(1.5)
            cap = capture_stop(g, "link-unplug-capture.bin")
            check("ROM boot log after replug + reset pulse", b"rst:0x1 (POWERON_RESET)" in cap,
                  f"{len(cap)} bytes")
    finally:
        bridge.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--unplug", action="store_true")
    args = ap.parse_args()

    g = Guest(env={"DT_OVERLAY": "none"})
    try:
        g.login()
        (test_real(g, args.unplug) if args.real else test_sim(g))
    finally:
        g.close()
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
