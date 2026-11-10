"""The simulated TF5's parameter data (console/sim_editor.py): MMSXLIT dumps built from the real
V4.55 tree (console/editor/tree.py), seeded with the schema's defaults and then demo values, and
the console -> client messages that carry them. So real wire addresses resolve in the simulator
exactly as on the desk.

Test-only shapes can still be built from ``C`` collections (``build_dump``).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .editor import tree
from .editor.codec import (
    DESC_MD5,
    DUMP,
    HASH_COMPARE,
    SET_PARAM,
    T_S32,
    T_STR,
    T_U8,
    T_U16,
    T_U32,
    element,
    message,
)
from .editor.dump import Dump

STR, INT, UINT = 0, 1, 2
PASSWORD = "s3cret-Pa55word"  # in the fake Property dump; must never leak anywhere (not the desk's)
MOBILE_PASSWORD = "m0bile-Pa55"  # the fake StageMix password in Setup; likewise

Param = tuple[Any, ...]  # (name, type, elemsize, arraysize[, default])


# ---------------------------------------------------------------------- MMSXLIT builder


@dataclass
class C:
    """A collection: name, array size, params [(name, type, elemsize, arraysize[, default])], children."""

    name: str
    arraysize: int = 1
    params: list[Param] = field(default_factory=list)
    children: list[C] = field(default_factory=list)


def _datasize(c: C) -> int:
    return sum(p[2] * p[3] for p in c.params) + sum(_datasize(ch) * ch.arraysize for ch in c.children)


def _desc(c: C, offset: int) -> bytes:
    prs = b"".join(b"PR " + struct.pack("<BHH", p[1], p[2], p[3]) + p[0].encode().ljust(24, b"\0") for p in c.params)
    kids, off = b"", sum(p[2] * p[3] for p in c.params)
    for ch in c.children:
        kids += _desc(ch, off)
        off += _datasize(ch) * ch.arraysize
    head = (
        b"COL0"
        + c.name.encode().ljust(24, b"\0")
        + struct.pack("<5I", len(prs), len(kids), offset, _datasize(c), c.arraysize)
    )
    return head + prs + kids


def _fill_defaults(c: C, data: bytearray, base: int) -> None:
    """Write each parameter's default (5th tuple entry, if any) into every array element."""
    size = _datasize(c)
    for i in range(c.arraysize):
        off = base + i * size
        for p in c.params:
            if len(p) > 4 and p[4]:
                raw = int(p[4]).to_bytes(p[2], "little", signed=p[1] == INT)
                for j in range(p[3]):
                    data[off + j * p[2] : off + (j + 1) * p[2]] = raw
            off += p[2] * p[3]
        for ch in c.children:
            _fill_defaults(ch, data, off)
            off += _datasize(ch) * ch.arraysize


def _image(root: C) -> tuple[bytes, bytes]:
    desc = _desc(root, 0)
    data = bytearray(_datasize(root) * root.arraysize)
    _fill_defaults(root, data, 0)
    return desc, bytes(data)


def _assemble(name: str, desc: bytes, data: bytes, values: dict[str, int | str] | None, md5: str | None) -> bytes:
    md5 = md5 or hashlib.md5(desc).hexdigest()
    head = (
        b"MMSXLIT\0"
        + name.encode().ljust(36, b"\0")
        + md5.encode().ljust(36, b"\0")
        + struct.pack("<II", len(desc), len(data))
    )
    if not values:
        return head + desc + data
    d = Dump(head + desc + data)
    buf = bytearray(data)
    for path, v in values.items():
        off, prm = d.locate(path)
        if prm.type == STR:
            raw = str(v).encode()[: prm.elemsize - 1].ljust(prm.elemsize, b"\0")
        else:
            raw = int(v).to_bytes(prm.elemsize, "little", signed=prm.type == INT)
        buf[off : off + prm.elemsize] = raw
    return head + desc + bytes(buf)


def build_dump(root: C, values: dict[str, int | str] | None = None, md5: str | None = None) -> bytes:
    """An MMSXLIT blob for ``root``; ``values`` by name path ("InputChannel:3/PEQ/On") over the defaults."""
    desc, data = _image(root)
    return _assemble(root.name, desc, data, values, md5)


def _from_json(node: dict[str, Any]) -> C:
    params = [
        (p["name"], tree.TYPE_CODES[p["type"]], p["width"], p["size"], p.get("default") if p["type"] != "str" else None)
        for p in node["params"]
    ]
    return C(node["name"], node["size"], params, [_from_json(ch) for ch in node["children"]])


def function_tree(func: str) -> C:
    """The TF5's tree of ``func`` (V4.55, every collection and parameter, the console's order)."""
    return _from_json(tree.tree(func))


@cache
def _function_image(func: str) -> tuple[bytes, bytes]:
    return _image(function_tree(func))


def function_dump(func: str, values: dict[str, int | str] | None = None, md5: str | None = None) -> bytes:
    """The MMSXLIT dump of ``func`` as a V4.55 TF5 sends it: schema defaults, then ``values``."""
    desc, data = _function_image(func)
    return _assemble(func, desc, data, values, md5 or DESC_MD5[func])


def mixing_tree() -> C:
    return function_tree("Mixing")


def property_tree() -> C:
    return function_tree("Property")


# ---------------------------------------------------------------------- demo values


def default_mixing_values() -> dict[str, int | str]:
    """Ch1 as captured from the real console (guide section 5); a few others for variety."""
    v: dict[str, int | str] = {}
    for x in range(40):
        b = f"InputChannel:{x}/PEQ"
        v.update(
            {
                f"InputChannel:{x}/Label/Name": f"ch{x + 1}",
                f"InputChannel:{x}/Patch/Select": 0 if x < 32 else 1,  # CH 33-40: USB (no local jack)
                f"{b}/On": 1,
                f"{b}/Bank/Type": "BOLD",
                f"{b}/Bank/HPF/Frequency": 800,
                f"{b}/Bank/HPF/Slope": 12,
                f"{b}/Bank/BandL/Type": 1,
                f"{b}/Bank/BandL/Frequency": 1250,
                f"{b}/Bank/BandL/Q": 700,
                f"{b}/Bank/Band:0/Frequency": 10000,
                f"{b}/Bank/Band:0/Q": 700,
                f"{b}/Bank/Band:1/Frequency": 40000,
                f"{b}/Bank/Band:1/Q": 700,
                f"{b}/Bank/BandH/Type": 1,
                f"{b}/Bank/BandH/Frequency": 100000,
                f"{b}/Bank/BandH/Q": 700,
            }
        )
    b = "InputChannel:0/PEQ/Bank"
    v.update(
        {
            "InputChannel:0/Label/Name": "GRA",
            f"{b}/HPF/On": 1,
            f"{b}/HPF/Frequency": 2000,
            f"{b}/BandL/Type": 0,
            f"{b}/BandL/Frequency": 1600,
            f"{b}/BandL/Gain": -520,
            f"{b}/BandL/Q": 140,
            f"{b}/Band:0/Frequency": 8000,
            f"{b}/Band:0/Gain": -1410,
            f"{b}/Band:0/Q": 250,
            f"{b}/Band:1/Frequency": 33500,
            f"{b}/Band:1/Gain": -570,
            f"{b}/Band:1/Q": 350,
            f"{b}/BandH/Type": 1,
            f"{b}/BandH/Frequency": 100000,
            f"{b}/BandH/Gain": 460,
            f"{b}/BandH/Q": 700,
        }
    )
    b = "InputChannel:1/PEQ"
    v.update(
        {"InputChannel:1/Label/Name": "Pastor", f"{b}/On": 0, f"{b}/Bank/Band:0/Gain": 600, f"{b}/Bank/OneKnob/On": 1}
    )
    for x in range(2):
        b = f"Stereo:{x}/PEQ"
        v.update(
            {
                f"Stereo:{x}/Label/Name": "Stereo",
                f"{b}/On": 1,
                f"{b}/Bank/Type": "BOLD",
                f"{b}/Bank/BandL/Frequency": 1250,
                f"{b}/Bank/BandL/Q": 700,
                f"{b}/Bank/Band:0/Frequency": 10000,
                f"{b}/Bank/Band:0/Q": 700,
                f"{b}/Bank/Band:1/Frequency": 40000,
                f"{b}/Bank/Band:1/Q": 700,
                f"{b}/Bank/BandH/Frequency": 100000,
                f"{b}/Bank/BandH/Q": 700,
            }
        )
    v["Stereo:1/PEQ/Bank/BandL/Type"] = 2  # the low band as a high-pass filter...
    v["Stereo:1/PEQ/Bank/BandL/HPFOn"] = 1  # ...switched on
    v["Stereo:1/PEQ/Bank/BandL/Frequency"] = 800
    v["Stereo:1/PEQ/Bank/Band:1/Gain"] = -300
    for x, name in enumerate(["VOX", "DRUMS", "BAND"] + [f"DCA {x + 1}" for x in range(3, 8)]):
        v[f"DCA:{x}/Label/Name"] = name
    for x, dca in ((0, 0), (1, 0), (2, 0), (3, 0), (5, 1), (6, 1), (7, 1), (8, 1), (9, 2), (10, 2), (13, 2)):
        v[f"InputChannel:{x}/DCAGroup:{dca}/Assign"] = 1  # the demo band (sim_rcp.DEMO_INPUTS) on DCAs
        v[f"InputChannel:{x}/MuteGroup:0/Assign"] = 1  # every used input in mute group 1 (INPUT)
    for band, gain in ((9, -300), (15, -450), (20, -250)):  # wedge 1's GEQ notches
        v[f"Mix:0/GEQ/Band:{band}/Gain"] = gain
    return v


def default_values(func: str) -> dict[str, int | str]:
    """Demo values of a function, over its schema defaults (as on the auditorium desk where known)."""
    if func == "Mixing":
        return default_mixing_values()
    if func == "Property":
        return {
            "Specification/Unit/ProductName": "TF5",
            "Specification/Unit/ManufacturerName": "Yamaha",
            "Specification/Unit/FirmwareVersion": tree.FIRMWARE,
            "Status/CommSession:0/ClientType": "Editor",
            "UserAccount/Unit/Password": PASSWORD,
        }
    if func == "Setup":
        names = ["INPUT", "FX", "MUTE 3", "MUTE 4", "MUTE 5", "MUTE 6"]
        v: dict[str, int | str] = {f"MuteMaster:{i}/Name": n for i, n in enumerate(names)}
        v.update({"Scene/LastScene": "A00", "CommSession/MobileMix/Password": MOBILE_PASSWORD})
        return v
    if func == "Processing":
        return {f"BuiltIn/InputPort:{p}/HeadAmp/Gain": g for p, g in enumerate((4500, 4000, 4200))}
    if func == "FX":
        return {"FX:0/Param/PluginName": "Echo", "FX:1/Param/PluginName": "MonoDelay"}
    if func == "SceneStatus":
        return {"Bank:0/Info:0/Title": "Default", "Bank:0/Info:1/Title": "Sunday"}
    return {}


# ---------------------------------------------------------------------- console -> client messages


def wire_address(dump: Dump, path: str) -> tuple[list[int], list[int]]:
    """Name path -> (wire path, index per level): the inverse of Dump.resolve."""
    wp, idx, _ = dump.address(path)
    return wp, idx


def set_param(
    func: str, path: list[int], idx: list[int], value: int | str, origin: int = 0xA1, vtype: int | None = None
) -> bytes:
    """A console -> client set-param notification (built only by the FAKE console)."""
    if isinstance(value, str):
        v = element(T_STR, value)
    else:
        v = element(vtype or (T_S32 if value < 0 else T_U32), [value])
    return message(
        SET_PARAM,
        [
            element(T_U8, b"\0"),
            element(T_STR, func),
            element(T_STR, func),
            element(T_U16, [len(path)]),
            element(T_U32, path),
            element(T_U32, idx),
            element(T_U32, [origin]),
            v,
        ],
    )


def dump_message(func: str, blob: bytes) -> bytes:
    return message(
        DUMP,
        [
            element(T_U8, b"\0"),
            element(T_STR, func),
            element(T_STR, func),
            element(T_U16, [0]),
            element(T_U32, []),
            element(T_U32, []),
            element(T_U32, [240]),
            element(T_U8, blob),
            element(T_U8, b""),
        ],
    )


def console_hash(func: str, md5: str | None = None) -> bytes:
    return message(
        HASH_COMPARE,
        [
            element(T_U8, b"\0"),
            element(T_STR, func),
            element(T_U8, bytes.fromhex(md5 or DESC_MD5[func])),
            element(T_U8, hashlib.md5(func.encode()).digest()),
        ],
    )
