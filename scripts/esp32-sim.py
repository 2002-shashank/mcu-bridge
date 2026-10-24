#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Simulated mcu-bridge ESP32: speaks docs/protocol.md on QEMU's ESP32 socket.

    scripts/esp32-sim.py [--socket out/esp32.sock] [--rate 50] [--channels 4]

Behaves like the firmware: on boot sends ROM-like text, a 0x00 flush and HELLO,
then DATA at --rate; answers PING/SET_RATE, and unknown commands with ACK
status 2. If the socket is not there yet (QEMU still starting) it keeps retrying; if
QEMU goes away it reconnects and carries on (the board did not reboot).

Fault injection, one command per line on stdin:
    reset          reboot (EN pulse): ROM text, HELLO, seq 0, new boot_id
    unplug SECS    disconnect for SECS; the USB-powered board reboots, and what it
                   sends while the host's USB serial port enumerates is lost, so
                   it reconnects mid-stream: no ROM text or HELLO, seq > 0,
                   new boot_id
    corrupt N      flip one content bit in each of the next N frames
    drop N         skip sending the next N frames (seq still advances)
    garbage N      send N random non-zero bytes
    rate HZ        change the DATA rate (as SET_RATE would)
    stats          print counters
    quit
Events are printed to stdout as JSON lines: ready, cmd, stats, error.
"""
import argparse, json, math, os, random, select, socket, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "protocol"))
import mcb_proto as P

FW_VERSION = 0x00000100  # 0.1.0, simulator
ENUM_S = 1.0  # after a replug, output is lost until the host's serial port exists
ROM_TEXT = (b"ets Jul 29 2019 12:21:46\r\n\r\n"
            b"rst:0x1 (POWERON_RESET),boot:0x17 (SPI_FAST_FLASH_BOOT)\r\n"
            b"configsip: 0, SPIWP:0xee\r\nmode:DIO, clock div:2\r\n"
            b"entry 0x400837e0\r\nI (soc_init): ESP Simple boot\r\n")


def emit(event, **kw):
    print(json.dumps({"event": event, **kw}), flush=True)


class Sim:
    def __init__(self, args):
        self.path = args.socket
        self.channels = args.channels
        self.boot_rate = args.rate
        self.rnd = random.Random(args.seed)
        self.sock = None
        self.stats = dict(boots=0, data_sent=0, corrupted=0, dropped=0, cmds=0,
                          tx_bytes_lost=0,
                          rx_crc_errors=0, rx_framing_errors=0, rx_overruns=0)
        self.corrupt_left = self.drop_left = 0
        self.boot_id = 0
        self.pending_boot = "visible"

    # --- connection -------------------------------------------------------
    def connect(self):
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
                s.setblocking(False)
                self.sock = s
                return
            except OSError:
                s.close()
                time.sleep(0.2)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, data):
        # Like a UART, never block: if QEMU isn't reading (guest port closed,
        # driver unloaded) bytes are lost. On error mark the link down; run()
        # reconnects.
        if not self.sock:
            self.stats["tx_bytes_lost"] += len(data)
            return
        try:
            n = self.sock.send(data)
        except BlockingIOError:
            n = 0
        except OSError:
            self.disconnect()
            return
        self.stats["tx_bytes_lost"] += len(data) - n

    # --- firmware behavior -----------------------------------------------
    def boot(self, visible=True):
        # Firmware picks a random non-zero boot_id; never repeat here so tests
        # are deterministic (real firmware: 1-in-255 repeat, seq rule covers it).
        self.boot_id = self.rnd.choice([b for b in range(1, 256) if b != self.boot_id])
        self.seq = 0
        self.rate = self.boot_rate
        self.t_boot = time.monotonic()
        self.next_tick = time.monotonic()
        self.rx = P.Receiver()
        self.stats["boots"] += 1
        if visible:
            self.send(ROM_TEXT + b"\x00")
            self.send_frame(P.TYPE_HELLO, P.hello_payload(self.rate, FW_VERSION))
        else:
            # Booted ENUM_S ago; HELLO and the DATA since were sent into the void
            self.t_boot -= ENUM_S
            self.seq = 1 + int(ENUM_S * self.rate)
        emit("ready", boot=self.stats["boots"], boot_id=self.boot_id, visible=visible)

    def now_us(self):
        return int((time.monotonic() - self.t_boot) * 1e6)

    def send_frame(self, type, payload):
        f = P.Frame(type, self.seq, self.now_us(), payload, self.boot_id)
        self.seq = (self.seq + 1) & P.U32
        if self.drop_left:
            self.drop_left -= 1
            self.stats["dropped"] += 1
            return
        w = bytearray(P.frame_encode(f))
        if self.corrupt_left:
            self.corrupt_left -= 1
            self.stats["corrupted"] += 1
            w = self.corrupt(w)
        self.send(bytes(w))

    def corrupt(self, w):
        # Flip a bit in the decoded content and re-encode, so the receiver sees
        # exactly one CRC error (a raw wire flip may hit COBS overhead instead).
        raw = bytearray(P.cobs_decode(bytes(w[:-1])))
        raw[self.rnd.randrange(len(raw))] ^= 1 << self.rnd.randrange(8)
        return P.cobs_encode(bytes(raw)) + b"\x00"

    def tick(self):
        # Same channels as the firmware: touched, x, y, pressure. A virtual
        # finger presses for 1 s in every 4 and slides across the panel.
        t = time.monotonic() - self.t_boot
        phase = t % 4.0
        if phase < 1.0:
            values = [1, int(300 + 3400 * phase), int(2000 + 1200 * math.sin(2 * math.pi * phase)),
                      600]
        else:
            values = [0, -1, -1, 0]
        values = values[:self.channels]
        self.send_frame(P.TYPE_DATA, P.data_payload(values))
        self.stats["data_sent"] += 1
        self.next_tick += 1 / self.rate
        if self.next_tick < time.monotonic():  # fell behind (e.g. after unplug)
            self.next_tick = time.monotonic() + 1 / self.rate

    def handle(self, f):
        self.stats["cmds"] += 1
        emit("cmd", type=f.type, seq=f.seq, payload=f.payload.hex())
        if f.type == P.TYPE_PING:
            self.send_frame(P.TYPE_PONG, f.seq.to_bytes(4, "little") + f.payload[:P.PING_MAX_TOKEN])
            return
        status = P.STATUS_UNSUPPORTED
        if f.type == P.TYPE_SET_RATE:
            status = P.STATUS_RANGE
            if len(f.payload) == 2:
                hz = int.from_bytes(f.payload, "little")
                if P.RATE_MIN_HZ <= hz <= P.RATE_MAX_HZ:
                    self.rate, status = hz, P.STATUS_OK
                    self.next_tick = time.monotonic()
        self.send_frame(P.TYPE_ACK, P.ack_payload(f.seq, f.type, status))

    def on_rx(self, data):
        for st, f in self.rx.feed(data):
            if st == P.RX_FRAME:
                self.handle(f)
            else:
                key = {P.RX_CRC_ERR: "rx_crc_errors", P.RX_FRAMING_ERR: "rx_framing_errors",
                       P.RX_OVERRUN: "rx_overruns"}[st]
                self.stats[key] += 1

    # --- fault injection ----------------------------------------------------
    def control(self, line):
        cmd, *arg = line.split()
        try:
            n = int(arg[0]) if arg and cmd != "unplug" else 0
            secs = float(arg[0]) if cmd == "unplug" else 0.0
        except ValueError:
            emit("error", msg=f"bad argument in {line.strip()!r}")
            return True
        if cmd == "reset":
            self.boot()
        elif cmd == "unplug":
            self.disconnect()
            time.sleep(secs)
            self.pending_boot = "replug"  # run() reconnects, then boots
        elif cmd == "corrupt":
            self.corrupt_left += n
        elif cmd == "drop":
            self.drop_left += n
        elif cmd == "garbage":
            self.send(bytes(self.rnd.randrange(1, 256) for _ in range(n)))
        elif cmd == "rate":
            if not P.RATE_MIN_HZ <= n <= P.RATE_MAX_HZ:
                emit("error", msg=f"rate must be {P.RATE_MIN_HZ}..{P.RATE_MAX_HZ}")
            else:
                self.rate = n
        elif cmd == "stats":
            emit("stats", **self.stats)
        elif cmd == "quit":
            return False
        else:
            emit("error", msg=f"unknown command {cmd!r}")
        return True

    def run(self):
        stdin_fd, stdin_buf = sys.stdin.fileno(), b""
        while True:
            if not self.sock:  # first start, unplug, or QEMU went away
                self.connect()
                if self.pending_boot:
                    self.boot(visible=self.pending_boot == "visible")
                    self.pending_boot = None
            fds = [self.sock] + ([stdin_fd] if stdin_fd is not None else [])
            timeout = max(0.0, self.next_tick - time.monotonic())
            readable, _, _ = select.select(fds, [], [], timeout)
            if self.sock in readable:
                try:
                    data = self.sock.recv(4096)
                except BlockingIOError:  # spurious wakeup, not a disconnect
                    data = None
                except OSError:
                    data = b""
                if data is None:
                    pass
                elif not data:
                    self.disconnect()
                    continue
                self.on_rx(data)
            if stdin_fd in readable:
                # os.read, not readline: select() can't see Python's buffer
                chunk = os.read(stdin_fd, 4096)
                if not chunk:
                    stdin_fd = None
                stdin_buf += chunk
                *lines, stdin_buf = stdin_buf.split(b"\n")
                for line in lines:
                    if line.strip() and not self.control(line.decode()):
                        return
            if self.sock and time.monotonic() >= self.next_tick:
                self.tick()


def main():
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--socket", default=os.path.join(repo, "out", "esp32.sock"))
    ap.add_argument("--rate", type=int, default=50)
    ap.add_argument("--channels", type=int, default=4, choices=range(1, 5))
    ap.add_argument("--seed", type=int, default=1)
    try:
        Sim(ap.parse_args()).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
