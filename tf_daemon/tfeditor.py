"""Client for the TF Editor protocol (TCP 50368): reads the console's EQ.

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

READ-ONLY UNLESS WRITES ARE ENABLED, and then only for what a person asked for.
Every outgoing frame goes through ``_send``, which passes it to ``check_outbound``
first; that accepts only the handshake / keepalive messages listed in ALLOWED_*
below and raises ReadOnlyViolation for anything else, notably:

- set-param (``01 11``), except as below, and full dump (``01 14``);
- ``SYNCDIR:1``: makes the console take the client's memories (overwrite);
- ``TYPE:Editor``: the console allows only one editor;
- any function other than Property / Mixing, any other event.

The one exception: with ``writes=True`` (config ``tf5.eq_write``), ``write()``
sends set-params for input-channel PEQ parameters (WRITABLE, values within
WRITE_LIMITS). It is called only from an explicit user request (the vocal
assist's "Apply to desk"); nothing in tf-daemon writes on its own. While a
``write()`` runs, ``check_outbound`` lets through exactly the frames of that
request and nothing else; at any other time a set-param is refused as before.

The Property dump contains the console password in plain text; it is dropped
unparsed and never logged.
"""

from __future__ import annotations

import argparse
import logging
import math
import re
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
WRITE_CONFIRM_S = 3.0            # write(): how long to wait for the console to echo each change

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
SET_PARAM = (0x01, 0x11, 0x01)   # received; sent only by write() (see the module docstring)
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

# The only parameters write() may change: an input channel's PEQ switch, high-pass
# and bands (guides/tf5_api.md section 5). Raw limits per parameter name.
ORIGIN_SELF = 0xA0   # "the sender's own change": the console applies and echoes it (guide 4.4)
WRITABLE = re.compile(r"InputChannel:\d+/PEQ/(On|Bank/HPF/(On|Frequency)"
                      r"|Bank/(BandL|Band:\d|BandH)/(Type|Frequency|Gain|Q|LPFOn))")
WRITE_LIMITS = {
    "On": (0, 1), "LPFOn": (0, 1), "Type": (0, 2),
    "HPF/Frequency": (200, 6000),     # 20-600 Hz (raw Hz x 10)
    "Frequency": (200, 200000),       # 20 Hz - 20 kHz
    "Gain": (-1800, 1800),            # +-18 dB (raw dB x 100)
    "Q": (100, 10000),                # 0.1 - 10 (raw x 1000)
}


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


def set_param_message(path: list[int], idx: list[int], prm: Prm, value: int) -> bytes:
    """A Mixing set-param as TF Editor sends it (guide 4.4); used only by TFEditorClient.write."""
    v = element(T_S32 if prm.type == 1 else T_U32, [value])
    return message(SET_PARAM, [element(T_U8, b"\0"), element(T_STR, "Mixing"), element(T_STR, "Mixing"),
                               element(T_U16, [len(path)]), element(T_U32, path), element(T_U32, idx),
                               element(T_U32, [ORIGIN_SELF]), v])


def write_limits(path: str) -> tuple[int, int]:
    """Raw (min, max) for a WRITABLE parameter path."""
    name = path.rsplit("/", 1)[-1]
    return WRITE_LIMITS["HPF/Frequency"] if path.endswith("HPF/Frequency") else WRITE_LIMITS[name]


def check_outbound(tag: str, payload: bytes, allow_set: Callable[[Message], bool] | None = None) -> None:
    """Raise ReadOnlyViolation unless the frame is one of the read-only handshake messages,
    or a Mixing set-param that ``allow_set`` (given only while a user-requested write runs)
    accepts."""
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
    if msg.cmd == SET_PARAM and tag == TAGS["Mixing"] and allow_set is not None:
        if not allow_set(msg):
            raise ReadOnlyViolation("set-param isn't one of the changes the user asked for")
        return
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
        """Wire address -> (name path, data offset, parameter). ProtocolError if it doesn't exist.

        ``path`` has one index per level into that level's parameters followed by its child
        collections (one index space, parameters first); the address ends at a parameter.
        ``idx`` is the array index at each level. E.g. InputChannel/PEQ/Bank/BandL/Gain is
        [0, 7, 1, 3, 2]: PEQ's 0 is its On parameter and 1 its Bank collection (seen on the
        wire, guides/tf5_api.md 4.4).
        """
        if not path or len(path) != len(idx):
            raise ProtocolError("path / index length mismatch")
        node, names, off = self.root, [], 0
        for depth, (p, i) in enumerate(zip(path, idx)):
            last = depth == len(path) - 1
            if 0 <= p < len(node.params):
                if not last:
                    raise ProtocolError(f"{node.name}: parameter {p} in the middle of an address")
                prm = node.params[p]
                if not 0 <= i < prm.arraysize:
                    raise ProtocolError(f"{prm.name}: index {i} out of range")
                names.append(f"{prm.name}:{i}" if prm.arraysize > 1 else prm.name)
                return "/".join(names), off + prm.offset + i * prm.elemsize, prm
            c = p - len(node.params)
            if not 0 <= c < len(node.children):
                raise ProtocolError(f"no entry {p} under {node.name}")
            if last:
                raise ProtocolError(f"address ends at collection {node.children[c].name}")
            node = node.children[c]
            if not 0 <= i < node.arraysize:
                raise ProtocolError(f"{node.name}: index {i} out of range")
            off += node.offset + i * node.datasize
            names.append(f"{node.name}:{i}" if node.arraysize > 1 else node.name)
        raise ProtocolError("unreachable")

    def address(self, path: str) -> tuple[list[int], list[int], Prm]:
        """Name path -> (wire path, index per level, parameter): the inverse of ``resolve``.
        KeyError if the path doesn't exist."""
        node, wp, idx = self.root, [], []
        parts = path.split("/")
        for part in parts[:-1]:
            name, i = _split_index(part)
            k = next((n for n, c in enumerate(node.children) if c.name == name), None)
            if k is None:
                raise KeyError(part)
            wp.append(len(node.params) + k)   # parameters first, then child collections
            node = node.children[k]
            if not 0 <= i < node.arraysize:
                raise KeyError(f"{part}: index out of range (size {node.arraysize})")
            idx.append(i)
        name, i = _split_index(parts[-1])
        k = next((n for n, p in enumerate(node.params) if p.name == name), None)
        if k is None:
            raise KeyError(parts[-1])
        if not 0 <= i < node.params[k].arraysize:
            raise KeyError(f"{parts[-1]}: index out of range")
        return wp + [k], idx + [i], node.params[k]

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

    As TF Editor maps it (FUN_0051a080): a type-2 band is a 12 dB/oct filter that is on
    only while the band's HPFOn / LPFOn switch is set; ``on`` is False otherwise.
    """
    t = int(g(path + "/Type"))
    kind, on = "bell", True
    if (low or high) and t == 2:
        kind = "hpf" if low else "lpf"
        on = bool(_opt(g, path + ("/HPFOn" if low else "/LPFOn"), 0))
    elif (low or high) and t == 1:
        kind = "low_shelf" if low else "high_shelf"
    return {"label": label, "type": kind, "on": on, "freq": g(path + "/Frequency") / 10,
            "gain": g(path + "/Gain") / 100, "q": g(path + "/Q") / 1000}


# The curve is computed the way TF Editor draws it (decompiled: designer FUN_005196e0,
# band table FUN_0051a080, response FUN_00546ae0): one digital biquad per section at the
# console's 48 kHz, bilinear transform with K = tan(pi f / fs), magnitudes summed in dB.
CURVE_FREQS = np.geomspace(20.0, 20000.0, 241)
FS = 48000.0
BUTTERWORTH_Q = 0.707106


def eq_curve(eq: dict[str, Any] | None, freqs: np.ndarray = CURVE_FREQS) -> np.ndarray:
    """Magnitude response in dB of the console EQ, as TF Editor computes it. EQ off: flat."""
    out = np.zeros(len(freqs))
    if not eq or not eq["on"]:
        return out
    hpf = eq.get("hpf")
    if hpf and hpf["on"]:
        kind = "hpf1" if hpf["slope"] == 6 else "hpf"
        out += section_db(kind, hpf["freq"], 0.0, BUTTERWORTH_Q, freqs)
    for b in eq["bands"]:
        if b["type"] in ("hpf", "lpf"):
            if b.get("on", True):
                out += section_db(b["type"], b["freq"], 0.0, BUTTERWORTH_Q, freqs)
        elif b["gain"] != 0:   # a zero-gain band is flat (TF Editor skips it)
            out += section_db(b["type"], b["freq"], b["gain"], b["q"], freqs)
    return out


def section_db(kind: str, freq: float, gain_db: float, q: float, freqs: np.ndarray = CURVE_FREQS,
               fs: float = FS) -> np.ndarray:
    """|H| in dB of one section at ``freqs``; ``kind``: bell, low_shelf, high_shelf,
    hpf / lpf (2nd order, Q), hpf1 / lpf1 (1st order)."""
    b, a = biquad(kind, freq, gain_db, q, fs)
    z = np.exp(-1j * 2 * np.pi * np.asarray(freqs, dtype=float) / max(fs, 44100.0))   # z^-1
    h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))


def biquad(kind: str, freq: float, gain_db: float, q: float, fs: float = FS) -> tuple[list[float], list[float]]:
    """(b, a) of TF Editor's design for one section (a not normalised)."""
    fs = max(fs, 44100.0)
    q = max(q, 0.1)
    k = math.tan(math.pi * max(freq, 16.0) / fs)
    kk = k * k
    v = 10 ** (abs(gain_db) / 20)
    if kind == "bell":   # Zoelzer: the cut mirrors the boost
        num, den = (v * k / q, k / q) if gain_db > 0 else (k / q, v * k / q)
        return [1 + kk + num, 2 * (kk - 1), 1 + kk - num], [1 + kk + den, 2 * (kk - 1), 1 + kk - den]
    if kind in ("low_shelf", "high_shelf"):
        # Two identical first-order shelves (Q not used), corner at freq.
        sv = math.sqrt(v)
        plain = [1 + k, k - 1]
        shelf = [1 + k * sv, k * sv - 1] if kind == "low_shelf" else [k + sv, k - sv]
        num, den = (shelf, plain) if gain_db > 0 else (plain, shelf)
        return [float(x) for x in np.convolve(num, num)], [float(x) for x in np.convolve(den, den)]
    if kind in ("hpf", "lpf"):
        d = [1 + k / q + kk, 2 * (kk - 1), 1 - k / q + kk]
        return ([kk, 2 * kk, kk] if kind == "lpf" else [1.0, -2.0, 1.0]), d
    if kind in ("hpf1", "lpf1"):
        return ([k, k, 0.0] if kind == "lpf1" else [1.0, -1.0, 0.0]), [1 + k, k - 1, 0.0]
    raise ValueError(kind)


# ---------------------------------------------------------------------- client

class TFEditorClient:
    """Keeps a TF Editor protocol session; holds the console's Mixing data.

    ``on_change`` (optional) is called with no arguments after the Mixing data
    changed (dump received or a parameter pushed). With ``writes`` False (the
    default) the client is read-only; with True, ``write()`` may change EQ
    parameters when a person asks for it.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, on_change: Callable[[], None] | None = None,
                 writes: bool = False) -> None:
        self.host, self.port = host, port
        self.writes = writes
        self._write_lock = threading.Lock()                     # one write() at a time
        self._authorized: dict[tuple[tuple[int, ...], tuple[int, ...]], tuple[int, int]] = {}  # addr -> (type, value)
        self._echo = threading.Condition()
        self._awaiting: dict[tuple[tuple[int, ...], tuple[int, ...]], int | str | None] = {}  # addr -> echoed value
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
        self.rejected = 0            # Mixing pushes that didn't fit our copy of the data (should stay 0)
        self.last_rejected: str | None = None
        self.received: dict[str, int] = {}   # "TAG cc dd ee" -> frames received (diagnostics)
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
                "changes": self.changes, "rejected": self.rejected, "last_rejected": self.last_rejected,
                "received": dict(self.received), "descriptor_md5": self.desc_md5,
                "descriptor_known": self.desc_md5 in (None, DESC_MD5["Mixing"]), "read_only": not self.writes}

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

    def write(self, changes: list[tuple[str, int]], timeout: float = WRITE_CONFIRM_S) -> list[dict[str, Any]]:
        """CHANGE PARAMETERS ON THE CONSOLE: ``[(name path, raw value), ...]``.

        Only for an explicit request by a person; nothing calls this on its own. Only
        WRITABLE paths, values within WRITE_LIMITS, and only with ``writes`` enabled;
        anything else raises before a byte is sent. Returns per change whether the
        console echoed it back within ``timeout`` and the value it reported.
        """
        if not self.writes:
            raise PermissionError("writing to the console is off (tf5.eq_write = false)")
        sock = self._sock
        plan = []
        with self._lock:
            dump = self._mixing
            if not self.synced or sock is None or dump is None:
                raise ConnectionError("not connected to the console's editor protocol")
            for path, value in changes:
                if not WRITABLE.fullmatch(path):
                    raise ValueError(f"{path}: not a writable EQ parameter")
                lo, hi = write_limits(path)
                if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                    raise ValueError(f"{path}: {value!r} outside {lo}..{hi}")
                try:
                    wp, idx, prm = dump.address(path)
                except KeyError:
                    raise ValueError(f"{path}: not on this console") from None
                if prm.type not in (1, 2) or (prm.type == 2 and value < 0):
                    raise ValueError(f"{path}: not a numeric parameter of that sign")
                plan.append((path, (tuple(wp), tuple(idx)), prm, value))
        if len({key for _, key, _, _ in plan}) != len(plan):
            raise ValueError("the same parameter twice in one write")
        with self._write_lock:
            with self._echo:
                self._awaiting = {key: None for _, key, _, _ in plan}
            echoed: dict[Any, Any] = {}
            try:
                self._authorized = {key: (prm.type, value) for _, key, prm, value in plan}
                try:
                    for _, (wp, idx), prm, value in plan:
                        self._send(sock, "MMIX", set_param_message(list(wp), list(idx), prm, value))
                finally:
                    self._authorized = {}
                log.info("TF5 editor protocol: wrote %d EQ parameter(s) on user request: %s", len(plan),
                         ", ".join(f"{p}={v}" for p, _, _, v in plan))
                deadline = time.monotonic() + timeout
                with self._echo:
                    while any(v is None for v in self._awaiting.values()):
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        self._echo.wait(left)
                    echoed = dict(self._awaiting)
            finally:
                with self._echo:
                    self._awaiting = {}
        return [{"path": path, "value": value, "confirmed": echoed.get(key) is not None, "actual": echoed.get(key)}
                for path, key, _, value in plan]

    def _allow_set(self, msg: Message) -> bool:
        """check_outbound's test while write() runs: exactly one of the requested changes."""
        e = msg.elements
        if len(e) != 8 or e[0] != Element(T_U8, b"\0") or e[1] != Element(T_STR, "Mixing") \
                or e[2] != Element(T_STR, "Mixing") or e[6] != Element(T_U32, [ORIGIN_SELF]) \
                or e[4].type != T_U32 or e[5].type != T_U32 or e[3] != Element(T_U16, [len(e[4].value)]):
            return False
        want = self._authorized.get((tuple(e[4].value), tuple(e[5].value)))
        if want is None:
            return False
        typ, value = want
        return e[7] == Element(T_S32 if typ == 1 else T_U32, [value])

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
        """THE ONLY WAY ANYTHING IS SENT. Refuses anything that isn't read-only, except
        the set-params of a write() in progress."""
        check_outbound(tag, payload, self._allow_set if self._authorized else None)
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
            log.info("TF5 editor protocol: connected to %s:%d (%s)", self.host, self.port,
                     "EQ writes on request" if self.writes else "read-only")
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
        key = f"{tag} {bytes(msg.cmd).hex(' ')}"
        self.received[key] = self.received.get(key, 0) + 1
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
                self.rejected += 1
                self.last_rejected = f"path={e[4].value} idx={e[5].value}: {err}"
                if self.rejected <= 5 or self.rejected % 100 == 0:   # loud, but not one line per knob tick
                    log.warning("TF5 editor protocol: push not applied (%d so far): %s", self.rejected,
                                self.last_rejected)
                return
        key = (tuple(e[4].value), tuple(e[5].value))
        with self._echo:
            if key in self._awaiting:   # the console confirming a write() of ours
                self._awaiting[key] = value
                self._echo.notify_all()
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
