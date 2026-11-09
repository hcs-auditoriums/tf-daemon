"""The console's parameter data: an MMSXLIT dump (parameter tree + data image), and
one channel's EQ settings read from it."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .codec import T_U8, Message, ProtocolError


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
        for depth, (p, i) in enumerate(zip(path, idx, strict=True)):
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

# Console source (console/patch.py kinds) -> Mixing collection.
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
            eq["hpf"] = {"on": bool(g(bank + "HPF/On")), "freq": int(g(bank + "HPF/Frequency")) / 10,
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
