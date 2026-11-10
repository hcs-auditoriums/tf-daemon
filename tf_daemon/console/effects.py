"""The desk's effects and delays over the TF Editor protocol (served by console/desk.py).

FX function (docs/desk-coverage-howto-complete.md 3.7):

    FX:0-1/{Bypass,MixBalance}, FX:n/Param/{PluginName,LibraryName}, FX:n/Param/Bank/Value:0-63
    InsFX:0-5/... the same shape (insert FX; which channel each is inserted on isn't in the tree)

The 64 parameter values are raw s32; what each means depends on the plugin and is unknown
[PENDING], so they are shown and edited as index / raw value. FX 1 returns on FxRtnChannel 0/1
(FX RTN 1L/1R), FX 2 on 2/3: their names are shown with the FX (their faders are a bus strip's).

Delay (Mixing): only ``StInChannel:0-3/Delay`` and ``Matrix:0-3/Delay`` have one in the V4.55
tree: {On, Time u32 ms x 1000 (1-1000 ms), TimeOffset s32 ms x 1000, LRLink}.

Writes go through POST /api/tf5/param (TFEditorClient.write): FX under the ``fx`` family
(tf5.fx_write), delay under ``processing`` (tf5.processing_write); ``writable`` here only says
whether that family is enabled, guard.py checks every value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .editor import tree
from .editor.guard import writable

if TYPE_CHECKING:
    from .editor.client import TFEditorClient

FX_UNITS = (("FX", 2), ("InsFX", 6))
VALUES = 64  # Param/Bank/Value[64]
DELAY_STRIPS = (("StInChannel", 4), ("Matrix", 4))
TIME_SCALE = 1000  # Delay Time / TimeOffset: raw = ms x 1000


def _limits(func: str, path: str) -> list[int | None]:
    s = tree.spec(func, path)
    return [s.min, s.max]


def _name(ec: TFEditorClient, strip: str) -> str:
    v = ec.get(f"{strip}/Label/Name")
    return v if isinstance(v, str) else ""


def fx_unit(ec: TFEditorClient, kind: str, n: int) -> dict[str, Any]:
    base = f"{kind}:{n}"
    values = [ec.get(f"{base}/Param/Bank/Value:{i}", "FX") for i in range(VALUES)]
    unit: dict[str, Any] = {
        "kind": kind,
        "index": n,
        "label": f"FX {n + 1}" if kind == "FX" else f"Insert FX {n + 1}",
        "path": base,
        "bypass": ec.get(f"{base}/Bypass", "FX"),
        "mix_balance": ec.get(f"{base}/MixBalance", "FX"),
        "plugin": ec.get(f"{base}/Param/PluginName", "FX"),
        "library": ec.get(f"{base}/Param/LibraryName", "FX"),
        "values": values,
        "writable": writable("FX", f"{base}/Bypass", ec.enabled_families) is not None,
    }
    if kind == "FX":
        unit["returns"] = [
            {"strip": f"FxRtnChannel:{c}", "name": _name(ec, f"FxRtnChannel:{c}")} for c in (2 * n, 2 * n + 1)
        ]
    return unit


def fx(ec: TFEditorClient) -> dict[str, Any]:
    """FX 1/2 and the six insert FX: bypass, mix balance, plugin, raw parameter values."""
    funcs = ec.status()["functions"]
    return {
        "synced": bool(funcs.get("FX", {}).get("synced")),
        "subscribed": "FX" in funcs,
        "units": [fx_unit(ec, kind, n) for kind, count in FX_UNITS for n in range(count)],
        "limits": {
            "mix_balance": _limits("FX", "FX:0/MixBalance"),
            "value": _limits("FX", "FX:0/Param/Bank/Value:0"),
        },
        "switch": "tf5.fx_write",
        "note": "parameter meanings depend on the plugin and are unknown: raw index / value",
    }


def _ms(v: int | str | None) -> float | None:
    return v / TIME_SCALE if isinstance(v, int) else None


def delay(ec: TFEditorClient) -> dict[str, Any]:
    """The ST IN and matrix delays: on, time and offset (raw and ms), L/R link."""
    strips = []
    for kind, count in DELAY_STRIPS:
        for n in range(count):
            base = f"{kind}:{n}"
            time, offset = ec.get(f"{base}/Delay/Time"), ec.get(f"{base}/Delay/TimeOffset")
            strips.append(
                {
                    "strip": base,
                    "label": f"ST IN {n // 2 + 1}{'LR'[n % 2]}" if kind == "StInChannel" else f"MATRIX {n + 1}",
                    "name": _name(ec, base),
                    "on": ec.get(f"{base}/Delay/On"),
                    "time": time,
                    "time_ms": _ms(time),
                    "offset": offset,
                    "offset_ms": _ms(offset),
                    "lr_link": ec.get(f"{base}/Delay/LRLink"),
                    "writable": writable("Mixing", f"{base}/Delay/Time", ec.enabled_families) is not None,
                }
            )
    return {
        "synced": ec.synced,
        "strips": strips,
        "limits": {
            "time": _limits("Mixing", "Matrix:0/Delay/Time"),
            "offset": _limits("Mixing", "Matrix:0/Delay/TimeOffset"),
        },
        "scale": TIME_SCALE,
        "switch": "tf5.processing_write",
    }
