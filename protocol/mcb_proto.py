"""mcu-bridge wire protocol in Python, mirroring protocol/mcb_proto.h.

Used by the simulated ESP32, the host bridge and the tests. See docs/protocol.md.
"""
import struct
import zlib
from dataclasses import dataclass

VERSION = 1
HDR_LEN = 12
CRC_LEN = 4
MAX_PAYLOAD = 64
MIN_FRAME = HDR_LEN + CRC_LEN
MAX_FRAME = MIN_FRAME + MAX_PAYLOAD
MAX_COBS = MAX_FRAME + MAX_FRAME // 254 + 1
MAX_WIRE = MAX_COBS + 1

TYPE_DATA = 0x01
TYPE_HELLO = 0x02
TYPE_PING = 0x10
TYPE_PONG = 0x11
TYPE_SET_RATE = 0x12
TYPE_ACK = 0x13

HELLO_LEN = 8
PING_MAX_TOKEN = 8
ACK_LEN = 6
DATA_MAX_CHANNELS = MAX_PAYLOAD // 4
RATE_MIN_HZ = 1
RATE_MAX_HZ = 200

STATUS_OK = 0
STATUS_RANGE = 1
STATUS_UNSUPPORTED = 2

# mcb_rx_status
RX_NONE, RX_FRAME, RX_CRC_ERR, RX_FRAMING_ERR, RX_OVERRUN = range(5)

U32 = 0xFFFFFFFF


def crc32(data: bytes) -> int:
    """Standard CRC-32 (IEEE 802.3); check value 0xcbf43926 for b"123456789"."""
    return zlib.crc32(data) & U32


@dataclass
class Frame:
    type: int
    seq: int = 0
    timestamp_us: int = 0
    payload: bytes = b""
    flags: int = 0


def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_at, code = 0, 1
    for i, b in enumerate(data):
        if b:
            out.append(b)
            code += 1
        if not b or code == 0xFF:
            out[code_at] = code
            code = 1
            # a full block ending the input needs no trailing empty block
            if b and i + 1 == len(data):
                return bytes(out)
            code_at = len(out)
            out.append(0)
    out[code_at] = code
    return bytes(out)


def cobs_decode(data: bytes, cap: int = MAX_FRAME):
    """Decode COBS bytes (no delimiter). Returns bytes, or None if invalid."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i]
        i += 1
        if not code or code - 1 > n - i or code - 1 > cap - len(out):
            return None
        out += data[i:i + code - 1]
        i += code - 1
        if code != 0xFF and i < n:
            if len(out) == cap:
                return None
            out.append(0)
    return bytes(out)


def frame_encode(f: Frame) -> bytes:
    """Frame -> wire bytes, including the 0x00 delimiter."""
    if len(f.payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    raw = struct.pack("<BBBBII", VERSION, f.type, len(f.payload), f.flags,
                      f.seq & U32, f.timestamp_us & U32) + bytes(f.payload)
    raw += struct.pack("<I", crc32(raw))
    return cobs_encode(raw) + b"\x00"


def frame_decode(cobs: bytes):
    """Classify a COBS frame (delimiter stripped). Returns (status, Frame or None)."""
    raw = cobs_decode(cobs)
    if raw is None or len(raw) < MIN_FRAME:
        return RX_FRAMING_ERR, None
    body = len(raw) - CRC_LEN
    if struct.unpack_from("<I", raw, body)[0] != crc32(raw[:body]):
        return RX_CRC_ERR, None
    ver, typ, ln, flags, seq, ts = struct.unpack_from("<BBBBII", raw)
    if ver != VERSION or ln != body - HDR_LEN:
        return RX_FRAMING_ERR, None
    return RX_FRAME, Frame(typ, seq, ts, raw[HDR_LEN:body], flags)


class Receiver:
    """Byte-stream receiver, same state machine as mcb_rx_byte()."""

    def __init__(self):
        self.buf = bytearray()
        self.discarding = False

    def byte(self, b: int):
        """Feed one byte. Returns (status, Frame or None)."""
        if b:
            if self.discarding:
                return RX_NONE, None
            if len(self.buf) == MAX_COBS:
                self.discarding = True
                self.buf.clear()
                return RX_OVERRUN, None
            self.buf.append(b)
            return RX_NONE, None
        if self.discarding:
            self.discarding = False
            return RX_NONE, None
        data, self.buf = bytes(self.buf), bytearray()
        if not data:
            return RX_NONE, None
        return frame_decode(data)

    def feed(self, data: bytes):
        """Feed bytes; yield (status, Frame or None) for every non-NONE event."""
        for b in data:
            ev = self.byte(b)
            if ev[0] != RX_NONE:
                yield ev


class SeqTracker:
    """Per-direction sequence tracking, same rules as mcb_seq_update()."""

    def __init__(self):
        self.synced = False
        self.next = 0

    def update(self, f: Frame):
        """Returns (frames lost before f, sender reset?)."""
        d = (f.seq - self.next) & U32
        backwards = d >= 0x80000000
        reset = f.type == TYPE_HELLO or (self.synced and backwards)
        lost = d if self.synced and not reset else 0
        self.synced = True
        self.next = (f.seq + 1) & U32
        return lost, reset


# Payload helpers
def data_payload(values) -> bytes:
    return struct.pack(f"<{len(values)}i", *values)


def data_values(payload: bytes):
    return list(struct.unpack(f"<{len(payload) // 4}i", payload))


def hello_payload(rate_hz: int, fw_version: int) -> bytes:
    return struct.pack("<BBHI", VERSION, 0, rate_hz, fw_version)


def ack_payload(req_seq: int, req_type: int, status: int) -> bytes:
    return struct.pack("<IBB", req_seq, req_type, status)
