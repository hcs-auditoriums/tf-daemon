"""READ-ONLY client for the TF Editor protocol (TCP 50368): the console's EQ.

The TF5's text protocol (RCP, see tf5.py) has no EQ addresses, so the EQ is
read the way TF Editor / StageMix read it. Protocol details and how they were
established: guides/tf5_api.md.

    -> MPRO subscribe "Property", hash compare (zero data hash), sync open
    <- MPRO hash compare, sync open, full dump (MMSXLIT), sync close
    -> MPRO sync close
    -> EEVT SetClientType "TYPE:StageMix SYNCDIR:2"
    -> MMIX subscribe "Mixing", hash compare, sync open
    <- MMIX ..., full dump of every mix parameter, sync close   (parsed: EQ)
    -> MMIX sync close
    <- MMIX set-param  (a change on the desk / another app / scene recall)
    <-> EEVT KeepAlive

THIS CLIENT NEVER CHANGES ANYTHING ON THE CONSOLE. Every outgoing frame goes
through ``_send``, which passes it to ``check_outbound`` first; that accepts
only the handshake / keepalive messages listed in ALLOWED_* below and raises
ReadOnlyViolation for anything else, notably:

- set-param (``01 11``) and full dump (``01 14``): these write parameters;
- ``SYNCDIR:1``: makes the console take the client's memories (overwrite);
- ``TYPE:Editor``: the console allows only one editor;
- any function other than Property / Mixing, any other event.

There is deliberately no builder for set-param or dump messages here.

The Property dump contains the console password in plain text; it is dropped
unparsed and never logged.
"""

from __future__ import annotations

import argparse
import logging
import math
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_PORT = 50368
MAX_FRAME = 4 * 1024 * 1024      # the Mixing dump is 67 kB; anything near this is garbage
KEEPALIVE_S = 10.0               # our keepalive period (the guide: every <= 15 s)
RX_TIMEOUT_S = 35.0              # no frame from the console for this long: reconnect
SYNC_TIMEOUT_S = 20.0            # connect -> Mixing dump
CLIENT_TYPE_WAIT_S = 3.0         # SetClientType acked but no onSetClientType: go on after this

# Element types: high nibble kind (1 unsigned, 2 signed, 3 string), low nibble width.
T_U8, T_U16, T_U32 = 0x11, 0x12, 0x14
T_S8, T_S16, T_S32 = 0x21, 0x22, 0x24
T_STR = 0x31
_INT_FMT = {T_U16: "H", T_U32: "I", T_S8: "b", T_S16: "h", T_S32: "i"}

# Commands: (class, command, direction). Class 01 = parameter channel, 03 = event.
SUBSCRIBE = (0x01, 0x01, 0x01)
HASH_COMPARE = (0x01, 0x10, 0x01)
SYNC_OPEN = (0x01, 0x04, 0x01)
SYNC_CLOSE = (0x01, 0x04, 0x10)
SET_PARAM = (0x01, 0x11, 0x01)   # received only; NEVER sent
DUMP = (0x01, 0x14, 0x01)        # received only; NEVER sent
EVENT = (0x03, 0x01, 0x01)
EVENT_REPLY = (0x03, 0x01, 0x10)

# Frame tag per function. Only these two are ever subscribed.
TAGS = {"Property": "MPRO", "Mixing": "MMIX"}
EVENT_TAG = "EEVT"
# Descriptor MD5s of the TF5 the protocol was read from (firmware specific).
# Sent in the hash compare; a console with other firmware answers with its own.
DESC_MD5 = {"Property": "3a7c8d4c85f89f1eaa834f96630cec3d", "Mixing": "8d0d0bdd394a9abb80ab12ed1500eddc"}
CLIENT_TYPE = "TYPE:StageMix SYNCDIR:2"   # SYNCDIR 2 = console -> client. NEVER 1.

# What may be sent. Everything else raises ReadOnlyViolation.
ALLOWED_PARAM_CMDS = {SUBSCRIBE, HASH_COMPARE, SYNC_OPEN, SYNC_CLOSE}
ALLOWED_EVENTS = {"KeepAlive": "", "SetClientType": CLIENT_TYPE}   # request name -> exact args
ALLOWED_EVENT_REPLIES = {"KeepAlive", "NotifySyncStatus"}         # console events we acknowledge


class ReadOnlyViolation(RuntimeError):
    """An outgoing frame could change console state; it was not sent."""


class ProtocolError(ValueError):
    pass


# ---------------------------------------------------------------------- elements / messages

@dataclass(frozen=True)
class Element:
    type: int
    value: Any   # list[int] for 0x12/0x14/0x2x, bytes for 0x11, str for 0x31


def element(t: int, value: Any) -> bytes:
    """Encode one element. ``value``: list of ints, bytes (0x11) or str (0x31)."""
    if t == T_STR:
        raw = str(value).encode("utf-8") + b"\0"
        return struct.pack(">BI", t, len(raw)) + raw
    if t == T_U8:
        raw = bytes(value)
        return struct.pack(">BI", t, len(raw)) + raw
    if t not in _INT_FMT:
        raise ValueError(f"unknown element type 0x{t:02x}")
    vals = list(value)
    return struct.pack(f">BI{len(vals)}{_INT_FMT[t]}", t, len(vals), *vals)


def parse_elements(buf: bytes, n: int) -> list[Element]:
    out, i = [], 0
    for _ in range(n):
        if i + 5 > len(buf):
            raise ProtocolError("element header past end of message")
        t = buf[i]
        count = struct.unpack_from(">I", buf, i + 1)[0]
        i += 5
        width = t & 0x0F
        if t not in (T_U8, T_STR) and t not in _INT_FMT:
            raise ProtocolError(f"unknown element type 0x{t:02x}")
        size = count * (width or 1)
        if i + size > len(buf):
            raise ProtocolError("element data past end of message")
        raw = buf[i:i + size]
        i += size
        if t == T_STR:
            out.append(Element(t, raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")))
        elif t == T_U8:
            out.append(Element(t, raw))
        else:
            out.append(Element(t, list(struct.unpack(f">{count}{_INT_FMT[t]}", raw))))
    return out


@dataclass(frozen=True)
class Message:
    cmd: tuple[int, int, int]
    elements: list[Element]


def message(cmd: tuple[int, int, int], elements: Iterable[bytes] = ()) -> bytes:
    """Payload: ``0x11 | u32 n | cmd[3] | element count | elements`` (n = 4 + elements length)."""
    els = list(elements)
    body = bytes(cmd) + bytes([len(els)]) + b"".join(els)
    return struct.pack(">BI", T_U8, len(body)) + body


def parse_message(payload: bytes) -> Message:
    if len(payload) < 9 or payload[0] != T_U8:
        raise ProtocolError("not a message payload")
    n = struct.unpack_from(">I", payload, 1)[0]
    if 5 + n > len(payload) or n < 4:
        raise ProtocolError("message length past end of frame")
    body = payload[5:5 + n]
    return Message((body[0], body[1], body[2]), parse_elements(body[4:], body[3]))


def frame(tag: str, payload: bytes) -> bytes:
    t = tag.encode("ascii")
    if not 1 <= len(t) <= 4:
        raise ValueError(f"bad tag {tag!r}")
    return t.ljust(4, b"\0") + struct.pack(">I", len(payload)) + payload


class FrameReader:
    """Reassembles ``tag[4] | u32 BE length | payload`` frames from a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self._buf += data
        out = []
        while len(self._buf) >= 8:
            tag_raw = bytes(self._buf[:4]).rstrip(b"\0")
            if not tag_raw or not all(0x20 < c < 0x7F for c in tag_raw):
                raise ProtocolError(f"bad frame tag {bytes(self._buf[:4])!r}")
            n = struct.unpack_from(">I", self._buf, 4)[0]
            if n > MAX_FRAME:
                raise ProtocolError(f"frame of {n} bytes")
            if len(self._buf) < 8 + n:
                break
            out.append((tag_raw.decode("ascii"), bytes(self._buf[8:8 + n])))
            del self._buf[:8 + n]
        return out


# ---------------------------------------------------------------------- the only messages we build

def subscribe(func: str) -> bytes:
    return message(SUBSCRIBE, [element(T_STR, func), element(T_U8, b"\x80")])


def hash_compare(func: str) -> bytes:
    """Our descriptor MD5 and an all-zero data hash: never matches, so the console dumps."""
    return message(HASH_COMPARE, [element(T_U8, b"\0"), element(T_STR, func),
                                  element(T_U8, bytes.fromhex(DESC_MD5[func])), element(T_U8, bytes(16))])


def sync(close: bool) -> bytes:
    return message(SYNC_CLOSE if close else SYNC_OPEN)


def event(name: str, args: str = "", reply: bool = False) -> bytes:
    return message(EVENT_REPLY if reply else EVENT,
                   [element(T_U32, [0]), element(T_U32, [0]), element(T_STR, name), element(T_STR, args)])


def check_outbound(tag: str, payload: bytes) -> None:
    """Raise ReadOnlyViolation unless the frame is one of the read-only handshake messages."""
    try:
        msg = parse_message(payload)
    except ProtocolError as e:
        raise ReadOnlyViolation(f"unparseable outgoing {tag} frame: {e}") from None
    els = msg.elements
    if tag == EVENT_TAG:
        if msg.cmd not in (EVENT, EVENT_REPLY):
            raise ReadOnlyViolation(f"EEVT command {bytes(msg.cmd).hex(' ')} not allowed")
        if len(els) != 4 or [e.type for e in els] != [T_U32, T_U32, T_STR, T_STR] \
                or els[0].value != [0] or els[1].value != [0]:
            raise ReadOnlyViolation("malformed event")
        name, args = els[2].value, els[3].value
        if msg.cmd == EVENT:
            if name not in ALLOWED_EVENTS or args != ALLOWED_EVENTS[name]:
                raise ReadOnlyViolation(f"event {name!r} {args!r} not allowed")
        elif name not in ALLOWED_EVENT_REPLIES:
            raise ReadOnlyViolation(f"event reply {name!r} not allowed")
        return
    func = next((f for f, t in TAGS.items() if t == tag), None)
    if func is None:
        raise ReadOnlyViolation(f"frame tag {tag!r} not allowed (only {sorted(TAGS.values())} and EEVT)")
    if msg.cmd not in ALLOWED_PARAM_CMDS:
        raise ReadOnlyViolation(f"{tag} command {bytes(msg.cmd).hex(' ')} not allowed (read-only client)")
    if msg.cmd in (SYNC_OPEN, SYNC_CLOSE):
        if els:
            raise ReadOnlyViolation("sync bracket carries no elements")
    elif msg.cmd == SUBSCRIBE:
        if len(els) != 2 or els[0] != Element(T_STR, func) or els[1] != Element(T_U8, b"\x80"):
            raise ReadOnlyViolation("malformed subscribe")
    elif msg.cmd == HASH_COMPARE:
        if len(els) != 4 or els[0] != Element(T_U8, b"\0") or els[1] != Element(T_STR, func) \
                or els[2].type != T_U8 or len(els[2].value) != 16 \
                or els[3].type != T_U8 or len(els[3].value) != 16:
            raise ReadOnlyViolation("malformed hash compare")


# ---------------------------------------------------------------------- MMSXLIT dump

@dataclass
class Prm:
    name: str
    type: int        # 0 string, 1 signed, 2 unsigned
    elemsize: int
    arraysize: int
    offset: int      # within the owning collection


@dataclass
class Col:
    name: str
    offset: int      # within the parent collection
    datasize: int    # one array element
    arraysize: int
    params: list[Prm] = field(default_factory=list)
    children: list[Col] = field(default_factory=list)

    def child(self, name: str) -> Col:
        for c in self.children:
            if c.name == name:
                return c
        raise KeyError(name)

    def param(self, name: str) -> Prm:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)


class Dump:
    """A function's parameter tree and data image (little-endian), from an MMSXLIT blob.

    Layout: ``"MMSXLIT\\0" | func[36] | descriptor MD5 hex[36] | u32 desc len | u32 data len``
    at 0x00..0x57, then the descriptor (COL0 / "PR " entries), then the data image.
    """

    def __init__(self, blob: bytes) -> None:
        if blob[:8] != b"MMSXLIT\0" or len(blob) < 0x58:
            raise ProtocolError("not an MMSXLIT dump")
        self.func = blob[8:0x2c].split(b"\0", 1)[0].decode("ascii", errors="replace")
        self.desc_md5 = blob[0x2c:0x50].split(b"\0", 1)[0].decode("ascii", errors="replace")
        desc_len, data_len = struct.unpack_from("<II", blob, 0x50)
        if 0x58 + desc_len + data_len > len(blob):
            raise ProtocolError("dump shorter than its header says")
        self._blob = blob
        self._desc_end = 0x58 + desc_len
        root, end = self._col(0x58)
        if end != self._desc_end:
            raise ProtocolError("descriptor length mismatch")
        self.root = root
        self.data = bytearray(blob[self._desc_end:self._desc_end + data_len])
        if root.datasize * root.arraysize > len(self.data):
            raise ProtocolError("data image smaller than the descriptor")
        del self._blob

    def _col(self, pos: int) -> tuple[Col, int]:
        b = self._blob
        if pos + 0x30 > self._desc_end or b[pos:pos + 4] != b"COL0":
            raise ProtocolError(f"expected COL0 at 0x{pos:x}")
        name = b[pos + 4:pos + 0x1c].split(b"\0", 1)[0].decode("ascii", errors="replace")
        pbytes, cbytes, off, size, arr = struct.unpack_from("<5I", b, pos + 0x1c)
        col = Col(name, off, size, arr)
        p, poff = pos + 0x30, 0
        if pbytes % 32 or p + pbytes + cbytes > self._desc_end:
            raise ProtocolError(f"bad collection sizes in {name!r}")
        for _ in range(pbytes // 32):
            if b[p:p + 3] != b"PR ":
                raise ProtocolError(f"expected PR at 0x{p:x}")
            typ, esz, parr = struct.unpack_from("<BHH", b, p + 3)
            pname = b[p + 8:p + 32].split(b"\0", 1)[0].decode("ascii", errors="replace")
            col.params.append(Prm(pname, typ, esz, parr, poff))
            poff += esz * parr
            p += 32
        end = p + cbytes
        while p < end:
            c, p = self._col(p)
            col.children.append(c)
        if p != end:
            raise ProtocolError(f"children of {name!r} overrun")
        return col, p

    # -------------------------------------------------- addressing

    def locate(self, path: str) -> tuple[int, Prm]:
        """``"InputChannel:3/PEQ/Bank/Band:1/Gain"`` -> (data offset, parameter); KeyError if absent."""
        parts = path.split("/")
        node, off = self.root, 0
        for part in parts[:-1]:
            name, i = _split_index(part)
            node = node.child(name)
            if not 0 <= i < node.arraysize:
                raise KeyError(f"{part}: index out of range (size {node.arraysize})")
            off += node.offset + i * node.datasize
        name, i = _split_index(parts[-1])
        prm = node.param(name)
        if not 0 <= i < prm.arraysize:
            raise KeyError(f"{parts[-1]}: index out of range (size {prm.arraysize})")
        return off + prm.offset + i * prm.elemsize, prm

    def get(self, path: str) -> int | str:
        return self._read(*self.locate(path))

    def resolve(self, path: list[int], idx: list[int]) -> tuple[str, int, Prm]:
        """Wire address (child index per level, last = parameter index; array index per
        level) -> (name path, data offset, parameter). ProtocolError if it doesn't exist."""
        if not path or len(path) != len(idx):
            raise ProtocolError("path / index length mismatch")
        node, names, off = self.root, [], 0
        for depth, (p, i) in enumerate(zip(path, idx)):
            if depth < len(path) - 1:
                if not 0 <= p < len(node.children):
                    raise ProtocolError(f"no child {p} under {node.name}")
                node = node.children[p]
                if not 0 <= i < node.arraysize:
                    raise ProtocolError(f"{node.name}: index {i} out of range")
                off += node.offset + i * node.datasize
                names.append(f"{node.name}:{i}" if node.arraysize > 1 else node.name)
            else:
                if not 0 <= p < len(node.params):
                    raise ProtocolError(f"no parameter {p} under {node.name}")
                prm = node.params[p]
                if not 0 <= i < prm.arraysize:
                    raise ProtocolError(f"{prm.name}: index {i} out of range")
                names.append(f"{prm.name}:{i}" if prm.arraysize > 1 else prm.name)
                return "/".join(names), off + prm.offset + i * prm.elemsize, prm
        raise ProtocolError("unreachable")

    def _read(self, off: int, prm: Prm) -> int | str:
        raw = bytes(self.data[off:off + prm.elemsize])
        if len(raw) != prm.elemsize:
            raise ProtocolError("parameter past end of data image")
        if prm.type == 0:
            return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        return int.from_bytes(raw, "little", signed=prm.type == 1)

    def apply(self, path: list[int], idx: list[int], value: int | str) -> str:
        """Update OUR COPY of the data image with a change the console reported.

        This only touches local memory; nothing is sent. Returns the name path.
        """
        name, off, prm = self.resolve(path, idx)
        if prm.type == 0:
            if not isinstance(value, str):
                raise ProtocolError(f"{name}: expected a string")
            raw = value.encode("utf-8")[:max(0, prm.elemsize - 1)].ljust(prm.elemsize, b"\0")
        else:
            if not isinstance(value, int):
                raise ProtocolError(f"{name}: expected an integer")
            try:
                raw = value.to_bytes(prm.elemsize, "little", signed=prm.type == 1)
            except OverflowError:
                raise ProtocolError(f"{name}: {value} doesn't fit {prm.elemsize} byte(s)") from None
        self.data[off:off + prm.elemsize] = raw
        return name


def _split_index(part: str) -> tuple[str, int]:
    name, _, i = part.partition(":")
    return name, int(i) if i else 0


def dump_blob(msg: Message) -> bytes | None:
    for e in msg.elements:
        if e.type == T_U8 and e.value[:8] == b"MMSXLIT\0":
            return e.value
    return None


# ---------------------------------------------------------------------- EQ

# Console source (tf5.py patch kinds) -> Mixing collection.
COLLECTIONS = {"InCh": "InputChannel", "St": "Stereo"}


def channel_eq(dump: Dump, kind: str, x: int) -> dict[str, Any] | None:
    """The PEQ settings of one channel, in Hz / dB / Q; None if the dump has no such channel."""
    col = COLLECTIONS.get(kind)
    if col is None:
        return None
    base = f"{col}:{x}/PEQ"
    try:
        g = lambda p: dump.get(f"{base}/{p}")  # noqa: E731
        bank = "Bank/"
        eq: dict[str, Any] = {
            "on": bool(g("On")),
            "bank": str(g(bank + "Type")),
            "one_knob": bool(_opt(g, bank + "OneKnob/On", 0)),
            "hpf": None,
            "bands": [],
        }
        if _has(dump, f"{base}/Bank/HPF/On"):
            eq["hpf"] = {"on": bool(g(bank + "HPF/On")), "freq": g(bank + "HPF/Frequency") / 10,
                         "slope": int(_opt(g, bank + "HPF/Slope", 12))}
        eq["bands"].append(_band(g, bank + "BandL", "L", low=True))
        n_mid = dump.root.child(col).child("PEQ").child("Bank").child("Band").arraysize \
            if _has_col(dump, col, "Band") else 0
        for i in range(n_mid):
            eq["bands"].append(_band(g, f"{bank}Band:{i}", str(i + 1)))
        eq["bands"].append(_band(g, bank + "BandH", "H", high=True))
    except (KeyError, ProtocolError):
        return None
    return eq


def _has(dump: Dump, path: str) -> bool:
    try:
        dump.locate(path)
        return True
    except KeyError:
        return False


def _has_col(dump: Dump, col: str, name: str) -> bool:
    try:
        dump.root.child(col).child("PEQ").child("Bank").child(name)
        return True
    except KeyError:
        return False


def _opt(g: Callable[[str], Any], path: str, default: Any) -> Any:
    try:
        return g(path)
    except KeyError:
        return default


def _band(g: Callable[[str], Any], path: str, label: str, low: bool = False, high: bool = False) -> dict[str, Any]:
    """Band type enum: 0 bell, 1 shelf, 2 HPF (low band) / LPF (high band); mid bands bell only.
    Some low / high bands also carry an HPFOn / LPFOn switch."""
    t = int(g(path + "/Type"))
    kind = "bell"
    if low and (t == 2 or _opt(g, path + "/HPFOn", 0)):
        kind = "hpf"
    elif high and (t == 2 or _opt(g, path + "/LPFOn", 0)):
        kind = "lpf"
    elif (low or high) and t == 1:
        kind = "low_shelf" if low else "high_shelf"
    return {"label": label, "type": kind, "freq": g(path + "/Frequency") / 10,
            "gain": g(path + "/Gain") / 100, "q": g(path + "/Q") / 1000}


CURVE_FREQS = np.geomspace(20.0, 20000.0, 241)


def eq_curve(eq: dict[str, Any] | None, freqs: np.ndarray = CURVE_FREQS) -> np.ndarray:
    """Magnitude response in dB of the console EQ (analog prototypes: an approximation of
    Yamaha's filters, close but not identical). EQ off: flat."""
    out = np.zeros(len(freqs))
    if not eq or not eq["on"]:
        return out
    s = 1j * np.asarray(freqs, dtype=float)
    hpf = eq.get("hpf")
    if hpf and hpf["on"]:
        out += _pass_db(freqs, hpf["freq"], max(1, round(hpf["slope"] / 6)), high=True)
    for b in eq["bands"]:
        f0, q = float(b["freq"]), max(0.05, float(b["q"]))
        if b["type"] == "hpf":
            out += _pass_db(freqs, f0, 2, high=True)
        elif b["type"] == "lpf":
            out += _pass_db(freqs, f0, 2, high=False)
        elif b["gain"] != 0:
            out += 20 * np.log10(np.abs(_shape(b["type"], s / f0, float(b["gain"]), q)))
    return out


def _pass_db(freqs: np.ndarray, f0: float, order: int, high: bool) -> np.ndarray:
    """Butterworth high/low pass of ``order`` (6 dB/oct per order)."""
    r = (f0 / np.asarray(freqs, dtype=float)) if high else (np.asarray(freqs, dtype=float) / f0)
    return -10 * np.log10(1 + r ** (2 * order))


def _shape(kind: str, s: np.ndarray, gain_db: float, q: float) -> np.ndarray:
    """RBJ analog prototypes with w0 = 1 (s is already divided by w0)."""
    a = 10 ** (gain_db / 40)
    if kind == "bell":
        return (s * s + s * (a / q) + 1) / (s * s + s / (a * q) + 1)
    sa = math.sqrt(a)
    if kind == "low_shelf":
        return a * (s * s + (sa / q) * s + a) / (a * s * s + (sa / q) * s + 1)
    if kind == "high_shelf":
        return a * (a * s * s + (sa / q) * s + 1) / (s * s + (sa / q) * s + a)
    raise ValueError(kind)


# ---------------------------------------------------------------------- client

class TFEditorClient:
    """Keeps a read-only TF Editor protocol session; holds the console's Mixing data.

    ``on_change`` (optional) is called with no arguments after the Mixing data
    changed (dump received or a parameter pushed).
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, on_change: Callable[[], None] | None = None) -> None:
        self.host, self.port = host, port
        self.on_change = on_change
        self._lock = threading.Lock()
        self._mixing: Dump | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.connected = False
        self.synced = False
        self.connects = 0
        self.last_error: str | None = None
        self.updated: float | None = None
        self.changes = 0             # parameter pushes applied
        self.desc_md5: str | None = None
        self.sent: list[tuple[str, tuple[int, int, int]]] = []   # (tag, cmd) of the last frames sent
        self._phase = "idle"
        self._mixing_due: float | None = None

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tfeditor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "connected": self.connected, "synced": self.synced,
                "connects": self.connects, "last_error": self.last_error, "updated": self.updated,
                "changes": self.changes, "descriptor_md5": self.desc_md5,
                "descriptor_known": self.desc_md5 in (None, DESC_MD5["Mixing"]), "read_only": True}

    def eq(self, kind: str, x: int) -> dict[str, Any] | None:
        with self._lock:
            return channel_eq(self._mixing, kind, x) if self._mixing is not None else None

    def get(self, path: str) -> int | str | None:
        """Any Mixing parameter by name path, e.g. ``"InputChannel:0/Label/Name"``."""
        with self._lock:
            if self._mixing is None:
                return None
            try:
                return self._mixing.get(path)
            except KeyError:
                return None

    # ------------------------------------------------------------------ connection thread

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except ReadOnlyViolation as e:
                # A bug in this module: stop for good rather than retry.
                log.error("TF5 editor protocol: refused to send a non-read-only frame (%s); stopped", e)
                self.last_error = f"stopped: {e}"
                self.connected = self.synced = False
                self._sock = None
                return
            except (OSError, ProtocolError) as e:
                if self._stop.is_set():
                    break
                if self.last_error != str(e):
                    log.warning("TF5 editor protocol %s:%d: %s (retrying)", self.host, self.port, e)
                self.last_error = str(e)
            finally:
                self.connected = self.synced = False
                self._sock = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _send(self, sock: socket.socket, tag: str, payload: bytes) -> None:
        """THE ONLY WAY ANYTHING IS SENT. Refuses anything that isn't read-only."""
        check_outbound(tag, payload)
        with self._send_lock:
            sock.sendall(frame(tag, payload))
        self.sent = (self.sent + [(tag, parse_message(payload).cmd)])[-50:]

    def _session(self) -> None:
        reader = FrameReader()
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(1.0)
            self.connected = True
            self.connects += 1
            log.info("TF5 editor protocol: connected to %s:%d (read-only)", self.host, self.port)
            self._phase, self._mixing_due = "property", None
            self._open("Property", sock)
            started = last_rx = last_ka = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if self._mixing_due is not None and now >= self._mixing_due and self._phase == "client_type":
                    self._start_mixing(sock)
                if not self.synced and now - started > SYNC_TIMEOUT_S:
                    raise TimeoutError(f"no Mixing dump within {SYNC_TIMEOUT_S:.0f} s (phase {self._phase})")
                if now - last_rx > RX_TIMEOUT_S:
                    raise TimeoutError("no data from the console")
                if now - last_ka >= KEEPALIVE_S:
                    self._send(sock, EVENT_TAG, event("KeepAlive"))
                    last_ka = now
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("console closed the connection")
                last_rx = time.monotonic()
                for tag, payload in reader.feed(chunk):
                    self._handle(sock, tag, payload)

    def _open(self, func: str, sock: socket.socket) -> None:
        tag = TAGS[func]
        self._send(sock, tag, subscribe(func))
        self._send(sock, tag, hash_compare(func))
        self._send(sock, tag, sync(close=False))

    def _handle(self, sock: socket.socket, tag: str, payload: bytes) -> None:
        msg = parse_message(payload)
        if tag == EVENT_TAG:
            self._on_event(sock, msg)
        elif tag == "MPRO":
            if msg.cmd == DUMP:
                # Contains the console password: drop it unparsed, never log it.
                self._send(sock, "MPRO", sync(close=True))
                if self._phase == "property":
                    self._phase = "client_type"
                    self._send(sock, EVENT_TAG, event("SetClientType", CLIENT_TYPE))
        elif tag == "MMIX":
            if msg.cmd == HASH_COMPARE and len(msg.elements) >= 3 and msg.elements[2].type == T_U8:
                self.desc_md5 = msg.elements[2].value.hex()
                if self.desc_md5 != DESC_MD5["Mixing"]:
                    log.warning("TF5 Mixing descriptor %s differs from the known firmware; "
                                "EQ is read by parameter name and may be incomplete", self.desc_md5)
            elif msg.cmd == DUMP:
                self._on_dump(sock, msg)
            elif msg.cmd == SET_PARAM:
                self._on_set(msg)

    def _on_event(self, sock: socket.socket, msg: Message) -> None:
        if len(msg.elements) < 4 or msg.elements[2].type != T_STR:
            return
        name = msg.elements[2].value
        args = msg.elements[3].value if msg.elements[3].type == T_STR else ""
        if msg.cmd == EVENT_REPLY:
            if name == "SetClientType" and self._phase == "client_type" and self._mixing_due is None:
                # TF Editor waits for onSetClientType; fall back if it never comes.
                self._mixing_due = time.monotonic() + CLIENT_TYPE_WAIT_S
            return
        if msg.cmd != EVENT:
            return
        if name in ALLOWED_EVENT_REPLIES:
            self._send(sock, EVENT_TAG, event(name, args, reply=True))   # TF Editor echoes these
        elif name == "onSetClientType":
            if self._phase == "client_type":
                self._start_mixing(sock)
        else:
            log.info("TF5 editor protocol: console event %r not answered", name)

    def _start_mixing(self, sock: socket.socket) -> None:
        self._phase = "mixing"
        self._mixing_due = None
        self._open("Mixing", sock)

    def _on_dump(self, sock: socket.socket, msg: Message) -> None:
        blob = dump_blob(msg)
        self._send(sock, "MMIX", sync(close=True))
        if blob is None:
            raise ProtocolError("Mixing dump without MMSXLIT data")
        dump = Dump(blob)
        if dump.func != "Mixing":
            raise ProtocolError(f"dump for {dump.func!r} on the Mixing channel")
        with self._lock:
            self._mixing = dump
        if not self.synced:
            log.info("TF5 editor protocol: Mixing data received (%d bytes)", len(dump.data))
        self.synced = True
        self._phase = "synced"
        self.last_error = None
        self.updated = time.time()
        self._changed()

    def _on_set(self, msg: Message) -> None:
        # e0 flags, e1 function, e2 base path, e3 depth, e4 path, e5 index, e6 origin, e7 value
        e = msg.elements
        if len(e) < 8 or e[1].value != "Mixing" or e[4].type != T_U32 or e[5].type != T_U32:
            return
        value = e[7].value
        if e[7].type == T_U8:
            if len(value) != 1:
                return
            value = value[0]
        elif e[7].type != T_STR:
            if len(value) != 1:
                return
            value = value[0]
        with self._lock:
            if self._mixing is None:
                return
            try:
                self._mixing.apply(e[4].value, e[5].value, value)
            except ProtocolError as err:
                log.debug("TF5 editor protocol: push not applied: %s", err)
                return
        self.changes += 1
        self.updated = time.time()
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("TF5 EQ change callback failed")


# ---------------------------------------------------------------------- probe CLI

def _parse_channels(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def main(argv: list[str] | None = None) -> int:
    """Connect once, print the EQ of some input channels, optionally keep watching. Read-only."""
    ap = argparse.ArgumentParser(prog="python -m tf_daemon.tfeditor",
                                 description="Read (never write) the TF5's EQ over the TF Editor protocol.")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--channels", default="1-8", help="input channels, e.g. 1-8,12 (default 1-8)")
    ap.add_argument("--watch", action="store_true", help="keep running and print changes")
    ap.add_argument("--timeout", type=float, default=SYNC_TIMEOUT_S + 5)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    chans = _parse_channels(args.channels)
    changed = threading.Event()
    client = TFEditorClient(args.host, args.port, on_change=changed.set)
    client.start()
    try:
        deadline = time.monotonic() + args.timeout
        while not client.synced and time.monotonic() < deadline:
            time.sleep(0.1)
        if not client.synced:
            print(f"not synchronized: {client.status()}")
            return 1
        print(f"descriptor {client.desc_md5} (known: {client.status()['descriptor_known']})")
        last: dict[int, Any] = {}
        while True:
            for ch in chans:
                eq = client.eq("InCh", ch - 1)
                if eq != last.get(ch):
                    print(f"InCh {ch:2d} {client.get(f'InputChannel:{ch - 1}/Label/Name')!r}: {_fmt_eq(eq)}")
                    last[ch] = eq
            if not args.watch:
                return 0
            changed.wait(1.0)
            changed.clear()
    except KeyboardInterrupt:
        return 0
    finally:
        client.stop()
        print("sent:", ", ".join(f"{t} {bytes(c).hex(' ')}" for t, c in client.sent))


def _fmt_eq(eq: dict[str, Any] | None) -> str:
    if eq is None:
        return "-"
    parts = ["on" if eq["on"] else "OFF", eq["bank"]] + (["1-knob"] if eq["one_knob"] else [])
    if eq["hpf"]:
        parts.append(f"HPF {'on' if eq['hpf']['on'] else 'off'} {eq['hpf']['freq']:g} Hz")
    for b in eq["bands"]:
        parts.append(f"{b['label']}:{b['type']} {b['freq']:g} Hz {b['gain']:+.1f} dB Q{b['q']:g}")
    return " | ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
