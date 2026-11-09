"""Putting a suggestion on the desk, and taking it back: ONLY on a person's confirmed request.

Nothing here writes on its own. ``apply_suggestion``, ``write_values`` (profile
recall, copy between channels) and ``undo`` are called from routes that require
``"confirm": true`` (the page sends it after a confirmation dialog).
tests/editor/test_guard.py pins the ways the desk's EQ gets written.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .plan import eq_plan
from .ports import DeskEQ

log = logging.getLogger(__name__)


class DeskWriter:
    """Applies the suggestion for an input to its desk channel's EQ, and undoes that.

    ``suggestion(index)`` returns the finished suggestion for an input (or None).
    ``desk`` None: no console, or its EQ isn't readable: suggestions only.
    """

    def __init__(self, desk: DeskEQ | None, suggestion: Callable[[int], dict[str, Any] | None]) -> None:
        self.desk = desk
        self._suggestion = suggestion
        self._apply_lock = threading.Lock()  # one desk write at a time
        self._undo: dict[int, list[tuple[str, int]]] = {}  # input -> desk values before the first apply
        self._applied: dict[int, dict[str, Any]] = {}  # input -> outcome of the last apply / undo

    def writable(self) -> DeskEQ | None:
        return self.desk if self.desk is not None and self.desk.eq_writable else None

    def current_eq(self, index: int) -> dict[str, Any] | None:
        console = self.desk
        if console is None or not console.eq_available:
            return None
        try:
            return console.eq(index).get("eq")
        except Exception:
            return None

    def apply_blocker(self, index: int) -> str | None:
        """Why the suggestion can't be applied to the desk now; None if it can."""
        console = self.writable()
        if console is None:
            return "writing to the desk is off (tf5.eq_write)"
        if self._suggestion(index) is None:
            return "no suggestion yet"
        if console.eq_target(index) is None:
            return "this input isn't an input channel on the desk"
        cur = self.current_eq(index)
        if cur is None:
            return "the desk's EQ for this channel hasn't been read yet"
        if cur.get("one_knob"):
            return "1-knob EQ is on for this channel; turn it off on the desk first"
        return None

    def plan(self, index: int) -> tuple[list[tuple[str, int]], list[str]]:
        reason = self.apply_blocker(index)
        if reason:
            raise ValueError(reason)
        result, cur = self._suggestion(index), self.current_eq(index)
        assert result is not None and cur is not None
        return eq_plan(result, cur)

    def apply_state(self, index: int) -> dict[str, Any]:
        reason = self.apply_blocker(index)
        state: dict[str, Any] = {"possible": reason is None, "reason": reason, "changes": []}
        if reason is None:
            try:
                state["changes"] = self.plan(index)[1]
            except ValueError as e:
                state.update(possible=False, reason=str(e))
        return state

    def apply_suggestion(self, index: int) -> dict[str, Any]:
        """SET THE DESK EQ to the suggestion. Only from a person's confirmed request."""
        with self._apply_lock:
            changes, lines = self.plan(index)  # raises unless the desk is writable
            out = self._write_locked(index, changes, "apply", lines)
        log.info(
            "Vocal assist: applied the suggested EQ to input %d on request (%d/%d confirmed by the desk)",
            index + 1,
            out["confirmed"],
            out["sent"],
        )
        return out

    def write_values(self, index: int, changes: list[tuple[str, int]], action: str, lines: list[str]) -> dict[str, Any]:
        """SET channel EQ values (raw, paths relative to the channel), keeping what they replace
        for Undo. Only from a person's confirmed request (a profile recall, a copy)."""
        with self._apply_lock:
            out = self._write_locked(index, changes, action, lines)
        log.info(
            "Vocal assist: %s on input %d on request (%d/%d confirmed by the desk)",
            action,
            index + 1,
            out["confirmed"],
            out["sent"],
        )
        return out

    def _write_locked(
        self, index: int, changes: list[tuple[str, int]], action: str, lines: list[str]
    ) -> dict[str, Any]:
        console = self.writable()
        if console is None:
            raise PermissionError("writing to the desk is off (tf5.eq_write)")
        if console.eq_target(index) is None:
            raise ValueError("this input isn't an input channel on the desk")
        if not changes:
            return self._record(index, action, [], lines)
        raw = console.eq_raw(index, [p for p, _ in changes])
        before = {p: v for p, v in raw.items() if isinstance(v, int)}
        if len(before) != len(raw):
            raise ValueError("couldn't read the current desk values to keep for Undo")
        results = console.write_eq(index, changes)
        # Undo goes back to before the first change since the last undo, for every value touched.
        originals = dict(self._undo.get(index, []))
        for p, _ in changes:
            originals.setdefault(p, before[p])
        self._undo[index] = list(originals.items())
        return self._record(index, action, results, lines)

    def undo(self, index: int) -> dict[str, Any]:
        """Put back the desk values the first apply replaced. Only on a person's request."""
        with self._apply_lock:
            changes = self._undo.get(index)
            if not changes:
                raise ValueError("nothing to undo")
            console = self.writable()
            if console is None:
                raise PermissionError("writing to the desk is off (tf5.eq_write)")
            results = console.write_eq(index, changes)
            del self._undo[index]
            out = self._record(index, "undo", results, [f"{p.removeprefix('PEQ/')} = {v}" for p, v in changes])
        log.info(
            "Vocal assist: undid the applied EQ on input %d on request (%d/%d confirmed by the desk)",
            index + 1,
            out["confirmed"],
            out["sent"],
        )
        return out

    def history(self) -> dict[str, Any]:
        return {"undo": {str(i): True for i in self._undo}, "applied": {str(i): a for i, a in self._applied.items()}}

    def _record(self, index: int, action: str, results: list[dict[str, Any]], lines: list[str]) -> dict[str, Any]:
        out = {
            "action": action,
            "at": time.time(),
            "sent": len(results),
            "confirmed": sum(1 for r in results if r["confirmed"]),
            "results": results,
            "changes": lines,
        }
        self._applied[index] = out
        return out
