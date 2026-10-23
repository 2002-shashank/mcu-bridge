"""Shared helpers for the QEMU-level tests: result reporting and child processes."""
import os, queue, subprocess, sys, threading, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "protocol"))

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)
    return bool(cond)


def finish():
    ok = all(results)
    print("RESULT:", "PASS" if ok else "FAIL", f"({sum(results)}/{len(results)})")
    sys.exit(0 if ok else 1)


class Proc:
    """Child process whose stdout (sim: JSON) or stderr (bridge: log) lines are queued."""

    def __init__(self, argv, stream):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        self.log_path = None
        out = self.p.stdout if stream == "stdout" else self.p.stderr
        threading.Thread(target=lambda: [self.lines.put(l.rstrip()) for l in out],
                         daemon=True).start()

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
            if self.log_path:
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            if pred(line):
                return line
        return None

    def drain(self):
        """Discard queued lines, so the next wait_for sees only new events."""
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                return

    def send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def stop(self):
        self.p.kill()
        self.p.wait()
        time.sleep(0.1)
        self.wait_for(lambda l: False, 0.2)  # drain remaining lines into the log


def sim(rate=50):
    return Proc([os.path.join(ROOT, "scripts", "esp32-sim.py"), "--rate", str(rate)], "stdout")
