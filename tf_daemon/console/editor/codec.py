"""TF Editor protocol wire format: elements, messages, frames, and the messages we build.

``tag[4] | u32 BE length | payload``; a payload is ``0x11 | u32 n | cmd[3] |
element count | elements``. See guides/tf5_api.md.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .dump import Prm

MAX_FRAME = 4 * 1024 * 1024      # the Mixing dump is 67 kB; anything near this is garbage

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
ORIGIN_SELF = 0xA0   # "the sender's own change": the console applies and echoes it (guide 4.4)


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
