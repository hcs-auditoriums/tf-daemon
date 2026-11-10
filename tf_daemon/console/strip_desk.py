"""An input channel's input section, 1-knob EQ / EQ type, gate and compressor on the desk itself,
over the TF Editor protocol (console/editor): what console/strip.py shows on /api/tf5/strip/<usb>.

Where each lives (docs/desk-coverage-howto-complete.md 3.3-3.5; strip.SPECS): the channel's
settings in Mixing (``InputChannel:C/...``); head-amp gain and +48V on the channel's input port
in Processing (``input_port``); the phantom master in Setup. Reading is the editor client's
``get``; writing its ``write`` with ``func=``, only in ``DeskStrip.put`` for a person's confirmed
edit (PUT /api/tf5/strip/<usb> with "confirm": true; tests/editor/test_guard.py pins it), and
only what an enabled write family allows (editor/guard.py: ``processing`` for phase / digital
gain / gate / comp / 1-knob / EQ type, ``headamp`` for the gain, ``phantom`` for +48V and the
master, each behind its tf5.*_write switch). Each value's lock reason (switch off, can't be
written from here) is shown with it; a write returns whether the desk echoed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .editor import tree
from .editor.guard import WriteNotAllowed, families_for, writable
from .strip import SOURCES, SPECS, Spec, limit, to_raw, to_value

if TYPE_CHECKING:
    from .editor.client import TFEditorClient

WATCHED = ("Mixing", "Processing", "Setup")  # the functions the strip reads
SHOWN_ONLY = "shown only: other values aren't verified on the desk yet"
NOT_HERE = "can't be changed from here"


def input_port(select: Any, x: int) -> str | None:
    """The Processing path of the input port whose head amp feeds input channel ``x`` (0-based),
    from its ``Patch/Select``: INPUT -> ``BuiltIn/InputPort:x``, SLOT -> ``Slot/InputPort:x``
    (CH n -> port n, [INFERRED] from TF Editor, howto 3.3), USB or unknown -> None (no head
    amp); None too when the desk has no such port (INPUT on CH 33-40)."""
    if select not in (0, 2):
        return None
    port = f"{'BuiltIn' if select == 0 else 'Slot'}/InputPort:{x}"
    try:
        tree.spec("Processing", f"{port}/HeadAmp/Gain")
    except KeyError:
        return None
    return port


def port_label(port: str | None) -> str | None:
    """``"BuiltIn/InputPort:4"`` -> ``"INPUT 5"``, ``"Slot/InputPort:0"`` -> ``"SLOT 1"``."""
    if port is None:
        return None
    where, _, n = port.partition("/InputPort:")
    return f"{'INPUT' if where == 'BuiltIn' else 'SLOT'} {int(n) + 1}"


class DeskStrip:
    """The desk-side sections of console/strip.py's Strip, read from / written to ``editor``."""

    def __init__(self, editor: TFEditorClient) -> None:
        self.ec = editor

    def version(self) -> tuple[int, ...]:
        """Changes whenever a watched function's data does (a dump or a push)."""
        return tuple(self.ec.versions.get(f, 0) for f in WATCHED)

    def _where(self, s: Spec, x: int, port: str | None) -> str | None:
        if s.at == "channel":
            return f"InputChannel:{x}/{s.path}"
        if s.at == "port":
            return f"{port}/{s.path}" if port else None
        return s.path

    def _port(self, x: int) -> tuple[Any, str | None]:
        select = self.ec.get(f"InputChannel:{x}/Patch/Select")
        return select, input_port(select, x)

    def _lock(self, s: Spec, path: str) -> str | None:
        """Why the parameter can't be changed from here, or None if it can."""
        if s.kind == "text":
            return SHOWN_ONLY
        found = families_for(s.func, path)
        if not found:
            return NOT_HERE
        if writable(s.func, path, self.ec.enabled_families) is None:
            return f"writing it is off ({' / '.join(sorted({f.switch for f, _ in found}))})"
        return None

    def section(self, name: str, x: int) -> dict[str, Any]:
        """One section of input channel ``x``: ``{"available", "values", "locked": {key: reason},
        "absent": {key: reason}, "writable"}``, input also ``"port"`` (e.g. "INPUT 5")."""
        if not self.ec.synced:
            return {"available": False, "reason": "not read from the desk yet (connecting)"}
        select, port = self._port(x)
        values: dict[str, Any] = {}
        locked: dict[str, str] = {}
        absent: dict[str, str] = {}
        for s in SPECS[name]:
            path = self._where(s, x, port)
            if path is None:
                src = SOURCES[select] if isinstance(select, int) and 0 <= select < len(SOURCES) else "this source"
                absent[s.key] = f"no head amp ({src})"
                continue
            raw = self.ec.get(path, s.func)
            if raw is None:
                absent[s.key] = f"not read from the desk ({s.func} isn't subscribed or hasn't arrived)"
                continue
            values[s.key] = to_value(s, raw)
            reason = self._lock(s, path)
            if reason:
                locked[s.key] = reason
        out: dict[str, Any] = {
            "available": bool(values),
            "values": values,
            "locked": locked,
            "absent": absent,
            "writable": any(k not in locked for k in values),
        }
        if not values:
            out["reason"] = "not read from the desk yet"
        if name == "input":
            out["port"] = port_label(port)
        return out

    def put(self, x: int, section: str, changes: dict[str, Any]) -> dict[str, Any]:
        """SET parameters of input channel ``x``'s ``section`` on the desk: ``{key: value}`` as
        the page shows them. Only for a person's confirmed edit. Returns ``{"applied": {key: the
        value the desk reports}, "confirmed": {key: echoed}}``. ValueError for an unknown key or a
        bad value, WriteNotAllowed when the guard refuses (a switch is off)."""
        specs = {s.key: s for s in SPECS[section]}
        bad = [k for k in changes if k not in specs]
        if bad:
            raise ValueError(f"unknown {section} parameter(s): {bad}")
        if not self.ec.enabled_families:
            raise WriteNotAllowed("writing to the desk is off (the tf5.*_write switches are false)")
        _, port = self._port(x)
        by_func: dict[str, list[tuple[str, int | str, str]]] = {}
        for k, v in changes.items():
            s = specs[k]
            path = self._where(s, x, port)
            if path is None:
                raise ValueError(f"{s.label}: this channel has no head amp (its source isn't INPUT or SLOT)")
            by_func.setdefault(s.func, []).append((path, to_raw(s, limit(s, v)), k))
        applied: dict[str, Any] = {}
        confirmed: dict[str, bool] = {}
        for func, items in by_func.items():
            results = self.ec.write([(p, r) for p, r, _ in items], func=func)
            for (_, raw, k), res in zip(items, results, strict=True):
                actual = res["actual"] if res["confirmed"] and res["actual"] is not None else raw
                applied[k] = to_value(specs[k], actual)
                confirmed[k] = bool(res["confirmed"])
        return {"applied": applied, "confirmed": confirmed}
