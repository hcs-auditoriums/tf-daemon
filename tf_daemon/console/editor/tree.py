"""The TF5's parameter tree, firmware V4.55: the single source for its shape, tags, MD5s and ranges.

``tf5_v455.json`` (next to this file) holds, for the 10 functions TF Editor subscribes on a TF5
(Property, Mixing, Processing, FX, Setup, Volatile, Status, SceneList, SceneStatus,
ConsoleSetup): the frame ``tag``, the descriptor ``md5`` and the ``tree``, collections
``{name, size, params, children}`` in the console's own order, parameters ``{name, type
(str | int | uint), width (bytes), size (array), min, max, default, scaling, unit}`` from TF
Editor's schema (``mms_<Function>.xml``). It holds no desk values.

    TAGS["Processing"]                                        -> "MPRC"
    DESC_MD5["Mixing"]                                        -> "8d0d0bdd..."
    spec("Mixing", "InputChannel:3/PEQ/Bank/Band:1/Gain")     -> Spec(type="int", width=2, min=-1800, ...)

A raw value is ``value_in_unit * scaling`` (guides/tf5_api.md; docs/desk-coverage-howto-complete.md
has the feature-by-feature view). The wire address of a parameter comes from the console's
dump (dump.Dump.address), never from here: this file describes V4.55, a console with other
firmware sends its own tree.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).with_name("tf5_v455.json")
_DATA: dict[str, Any] = json.loads(SOURCE.read_text(encoding="utf-8"))
_FUNCS: dict[str, Any] = _DATA["functions"]

FIRMWARE: str = _DATA["firmware"]
FUNCTIONS: tuple[str, ...] = tuple(_FUNCS)  # in the order TF Editor subscribes them
TAGS: dict[str, str] = {f: d["tag"] for f, d in _FUNCS.items()}  # function -> 4-character frame tag
DESC_MD5: dict[str, str] = {f: d["md5"] for f, d in _FUNCS.items()}  # descriptor MD5 per function (V4.55)
TYPE_CODES = {"str": 0, "int": 1, "uint": 2}  # as in the MMSXLIT descriptor (dump.Prm.type)


@dataclass(frozen=True)
class Spec:
    """One parameter's schema. For a string, ``max`` is its length limit and min / default are None."""

    name: str
    type: str  # "str" | "int" | "uint"
    width: int  # bytes per element (a string's buffer, NUL included)
    size: int  # array size (``Value:5`` indexes it)
    min: int | None
    max: int | None
    default: int | None
    scaling: int | None  # raw = value * scaling
    unit: str | None

    @property
    def code(self) -> int:
        """The descriptor's type code: 0 string, 1 signed, 2 unsigned."""
        return TYPE_CODES[self.type]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def function_of_tag(tag: str) -> str | None:
    return next((f for f, t in TAGS.items() if t == tag), None)


def tree(func: str) -> dict[str, Any]:
    """The root collection of ``func`` (KeyError if the TF5 has no such function). Don't modify it."""
    return _FUNCS[func]["tree"]


def _split(part: str) -> tuple[str, int]:
    name, _, i = part.partition(":")
    return name, int(i) if i else 0


@cache
def spec(func: str, path: str) -> Spec:
    """The schema of the parameter at name path ``path`` in ``func``, e.g.
    ``spec("Mixing", "InputChannel:3/PEQ/Bank/Band:1/Gain")``. Each level may carry an array
    index (``Band:1``; omitted = 0), checked against the collection's / parameter's size.
    KeyError if there is no such parameter or an index is out of range."""
    node = tree(func)
    parts = path.split("/")
    for part in parts[:-1]:
        name, i = _split(part)
        child: dict[str, Any] | None = next((c for c in node["children"] if c["name"] == name), None)
        if child is None:
            raise KeyError(f"{func}/{path}: no collection {name!r}")
        node = child
        if not 0 <= i < node["size"]:
            raise KeyError(f"{func}/{path}: {part} out of range (size {node['size']})")
    name, i = _split(parts[-1])
    p = next((p for p in node["params"] if p["name"] == name), None)
    if p is None:
        raise KeyError(f"{func}/{path}: no parameter {name!r}")
    if not 0 <= i < p["size"]:
        raise KeyError(f"{func}/{path}: {parts[-1]} out of range (size {p['size']})")
    is_str = p["type"] == "str"
    return Spec(
        name=p["name"],
        type=p["type"],
        width=p["width"],
        size=p["size"],
        min=None if is_str else p.get("min"),
        max=p["width"] - 1 if is_str else p.get("max"),
        default=None if is_str else p.get("default"),
        scaling=p.get("scaling"),
        unit=p.get("unit"),
    )
