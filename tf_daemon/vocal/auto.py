"""Auto EQ: moves a vocal channel's EQ towards the live suggestion on its own.

OFF unless both: ``vocal.auto_allowed = true`` in the config, and a person turns
it on for a channel (POST /api/vocal/auto with ``confirm: true``). The desk must
be writable (``tf5.eq_write``). Then, every TICK_S:

Slow loop (tone), only while the person is talking and the live suggestion is
new and has settled (session.py's model):
  * one step towards the suggestion (the desk changes plan.eq_plan asks for): a
    band's gain moves at most STEP_DB; its frequency, Q and type are changed
    only while its gain is (near) 0, so a band that has to move is faded out,
    moved, and faded back in. Nothing jumps audibly.
  * boosts only where the feedback headroom is known (feedback.py; the
    suggestion already keeps 6 dB of it); unknown headroom: boosts aim at 0 dB.

Fast loop (feedback), whenever a ring this mic causes is live (session.rings_for):
  * any boost within 1/3 octave of it is taken out at once;
  * a narrow notch (Q 8, -3 dB) goes on the band whose home range holds the
    ring, deepened by 3 dB each tick while the ring lasts (down to NOTCH_MAX_DB),
    and that band is left alone by the slow loop for NOTCH_HOLD_S.

It stops itself when the channel's EQ changes on the desk (any value it didn't
write: an operator takes over), when the desk stops being writable, or when the
channel's EQ is off or in 1-knob mode. Undo puts back the EQ from before it was
turned on. Every change it makes is logged.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any

from .plan import eq_plan
from .ports import DeskEQ
from .session import MicChecks

log = logging.getLogger(__name__)

TICK_S = 1.0
STEP_EVERY_S = 4.0  # slow loop: at most one step this often
STEP_DB = 1.0  # slow loop: largest gain change per band per step
NEAR_ZERO = 50  # raw gain (0.5 dB): a band this close to flat can be moved / retyped
TALKING_WITHIN_S = 1.5  # slow loop: only while the person has talked this recently
NOTCH_Q = 8.0
NOTCH_STEP_DB = 3.0
NOTCH_MAX_DB = 9.0
NOTCH_HOLD_S = 60.0
HPF_STEP_HZ = 10.0
BANDS = ("PEQ/Bank/BandL", "PEQ/Bank/Band:0", "PEQ/Bank/Band:1", "PEQ/Bank/BandH")
SLOT_RANGES = {
    "PEQ/Bank/BandL": (0, 400),
    "PEQ/Bank/Band:0": (250, 1600),
    "PEQ/Bank/Band:1": (1000, 5000),
    "PEQ/Bank/BandH": (3500, 20000),
}
PATHS = ["PEQ/On", "PEQ/Bank/HPF/On", "PEQ/Bank/HPF/Frequency"] + [
    f"{b}/{p}" for b in BANDS for p in ("Type", "Frequency", "Gain", "Q")
]


class _Channel:
    def __init__(self, baseline: dict[str, int]) -> None:
        self.baseline = baseline  # the desk before auto was turned on (for Undo)
        self.known = dict(baseline)  # what the desk holds, as far as we know
        self.acted_revision = 0
        self.last_step = 0.0
        self.held: dict[str, float] = {}  # band -> hold until (a notch)
        self.log: deque[dict[str, Any]] = deque(maxlen=50)
        self.stopped: str | None = None
        self.pending = False  # the last step didn't reach the suggestion yet


class AutoEQ:
    def __init__(self, checks: MicChecks, desk: DeskEQ | None, *, max_cut_db: float, max_boost_db: float) -> None:
        self.checks = checks
        self.desk = desk
        self.max_cut = round(max_cut_db * 100)
        self.max_boost = round(max_boost_db * 100)
        self._lock = threading.Lock()
        self._channels: dict[int, _Channel] = {}
        self._stopped: dict[int, _Channel] = {}  # kept for Undo / the log after a stop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ control (a person's requests)

    def arm(self, index: int) -> dict[str, Any]:
        """Turn auto EQ on for ``index``. Raises ValueError / PermissionError why not."""
        desk = self._writable()
        if desk.eq_target(index) is None:
            raise ValueError("this input isn't an input channel on the desk")
        eq = desk.eq(index).get("eq")
        if not eq:
            raise ValueError("the desk's EQ for this channel hasn't been read yet")
        if not eq["on"] or eq.get("one_knob"):
            raise ValueError("turn the channel's EQ on (and 1-knob off) on the desk first")
        raw = desk.eq_raw(index, PATHS)
        if any(not isinstance(raw.get(p), int) for p in PATHS):
            raise ValueError("couldn't read the channel's EQ from the desk")
        if not any(s.live and s.running for i, s in self.checks.sessions() if i == index):
            self.checks.start(index, live=True)
        ch = _Channel({p: int(v) for p, v in raw.items() if isinstance(v, int)})
        with self._lock:
            self._channels[index] = ch
            self._stopped.pop(index, None)
        self._log(ch, "on", [], "turned on by a person")
        log.info("Auto EQ on for input %d (turned on by a person)", index + 1)
        return self.state(index)

    def disarm(self, index: int, reason: str = "turned off by a person") -> None:
        with self._lock:
            ch = self._channels.pop(index, None)
            if ch is not None:
                ch.stopped = reason
                self._stopped[index] = ch
        if ch is not None:
            self._log(ch, "off", [], reason)
            log.info("Auto EQ off for input %d: %s", index + 1, reason)

    def undo(self, index: int) -> dict[str, Any]:
        """Turn auto off and put back the EQ from before it was turned on."""
        self.disarm(index, "undone by a person")
        with self._lock:
            ch = self._stopped.get(index)
        if ch is None:
            raise ValueError("auto EQ hasn't changed this channel")
        desk = self._writable()
        now = desk.eq_raw(index, PATHS)
        changes = [(p, v) for p, v in ch.baseline.items() if now.get(p) != v]
        results = desk.write_eq(index, changes) if changes else []
        self._log(ch, "undo", changes, "put back the EQ from before auto")
        return {"sent": len(results), "confirmed": sum(1 for r in results if r["confirmed"])}

    def state(self, index: int) -> dict[str, Any]:
        with self._lock:
            ch = self._channels.get(index) or self._stopped.get(index)
            on = index in self._channels
        if ch is None:
            return {"on": False, "log": []}
        return {"on": on, "stopped": ch.stopped, "log": list(ch.log)}

    def states(self) -> dict[str, Any]:
        with self._lock:
            indices = set(self._channels) | set(self._stopped)
        return {str(i): self.state(i) for i in sorted(indices)}

    # ------------------------------------------------------------------ the loop

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vocal-auto", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            self.tick()

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            items = list(self._channels.items())
        for index, ch in items:
            try:
                self._tick_channel(index, ch, now)
            except Exception as e:  # never leave it running in a state we don't understand
                log.exception("Auto EQ failed on input %d", index + 1)
                self.disarm(index, f"stopped: {e}")

    def _tick_channel(self, index: int, ch: _Channel, now: float) -> None:
        desk = self.desk
        if desk is None or not desk.eq_writable:
            self.disarm(index, "stopped: the desk isn't writable")
            return
        raw = desk.eq_raw(index, PATHS)
        changed = [p for p in PATHS if raw.get(p) != ch.known.get(p)]
        if changed:  # someone changed the EQ on the desk: they're in charge now
            self.disarm(index, f"stopped: the EQ was changed on the desk ({changed[0].removeprefix('PEQ/')})")
            return
        if raw.get("PEQ/On") != 1:
            self.disarm(index, "stopped: the channel's EQ was turned off")
            return
        cur = {p: int(v) for p, v in raw.items() if isinstance(v, int)}
        changes, why = self._feedback_step(index, ch, cur, now)
        if not changes:
            changes, why = self._tone_step(index, ch, cur, now)
        if changes:
            results = desk.write_eq(index, changes)
            ok = {r["path"]: r.get("actual", r["value"]) for r in results if r["confirmed"]}
            for p, v in changes:
                ch.known[p] = int(ok.get(p, v))
            if len(ok) < len(changes):  # the desk didn't take every change: re-read before going on
                fresh = desk.eq_raw(index, PATHS)
                ch.known.update({p: int(v) for p, v in fresh.items() if isinstance(v, int)})
            self._log(ch, "eq", changes, why)

    # ------------------------------------------------------------------ fast loop: feedback

    def _feedback_step(
        self, index: int, ch: _Channel, cur: dict[str, int], now: float
    ) -> tuple[list[tuple[str, int]], str]:
        live = self.checks.rings_for(index, live_only=True)
        if not live:
            return [], ""
        changes: list[tuple[str, int]] = []
        for ring in live:
            f = float(ring["freq"])
            for b in BANDS:  # no boost anywhere near a ring
                if cur[f"{b}/Gain"] > 0 and abs(math.log2(cur[f"{b}/Frequency"] / 10 / f)) <= 1 / 6:
                    changes.append((f"{b}/Gain", 0))
            band = self._notch_band(cur, f)
            if band is None:
                continue
            at_ring = abs(math.log2(cur[f"{band}/Frequency"] / 10 / f)) < 1 / 24 and cur[f"{band}/Q"] >= 5000
            depth = cur[f"{band}/Gain"] - round(NOTCH_STEP_DB * 100) if at_ring else -round(NOTCH_STEP_DB * 100)
            depth = max(depth, -round(NOTCH_MAX_DB * 100))
            if not at_ring:
                changes += [(f"{band}/Frequency", round(f * 10)), (f"{band}/Q", round(NOTCH_Q * 1000))]
                if band in ("PEQ/Bank/BandL", "PEQ/Bank/BandH") and cur[f"{band}/Type"] != 0:
                    changes.append((f"{band}/Type", 0))
            if depth != cur[f"{band}/Gain"]:
                changes.append((f"{band}/Gain", depth))
            ch.held[band] = now + NOTCH_HOLD_S
        return list(dict(changes).items()), f"ring at {live[0]['freq']:.0f} Hz"

    @staticmethod
    def _notch_band(cur: dict[str, int], f: float) -> str | None:
        """The band to notch ``f`` with: one already on it, else the least-used band whose
        home range holds it (never a band used as a filter)."""
        for b in BANDS:
            if abs(math.log2(cur[f"{b}/Frequency"] / 10 / f)) < 1 / 24 and cur[f"{b}/Q"] >= 5000:
                return b
        fits = [
            b
            for b in BANDS
            if SLOT_RANGES[b][0] <= f < SLOT_RANGES[b][1]
            and not (b in ("PEQ/Bank/BandL", "PEQ/Bank/BandH") and cur[f"{b}/Type"] == 2)
        ]
        return min(fits, key=lambda b: abs(cur[f"{b}/Gain"])) if fits else None

    # ------------------------------------------------------------------ slow loop: tone

    def _tone_step(
        self, index: int, ch: _Channel, cur: dict[str, int], now: float
    ) -> tuple[list[tuple[str, int]], str]:
        if now - ch.last_step < STEP_EVERY_S:
            return [], ""
        s = next((s for i, s in self.checks.sessions() if i == index), None)
        result = self.checks.result(index)
        if s is None or not s.live or result is None:
            return [], ""
        model = result.get("model") or {}
        if not model.get("settled") or (model.get("revision", 0) <= ch.acted_revision and not ch.pending):
            return [], ""
        if not s.talking(TALKING_WITHIN_S):
            return [], ""  # only while the person is talking
        eq = self.desk.eq(index).get("eq") if self.desk is not None else None
        if not eq:
            return [], ""
        target, _ = eq_plan(result, eq)
        target_d = dict(target)
        fb = result.get("feedback") or {}
        changes: list[tuple[str, int]] = []
        for b in BANDS:
            if ch.held.get(b, 0) > now:
                continue
            changes += self._band_step(b, cur, target_d, boosts_ok=bool(fb.get("known")))
        changes += self._hpf_step(cur, target_d)
        ch.last_step = now
        ch.acted_revision = model.get("revision", 0)
        ch.pending = bool(changes)  # keep stepping on the same revision until it's reached
        return changes, f"towards the suggestion (revision {ch.acted_revision})"

    def _band_step(
        self, b: str, cur: dict[str, int], target: dict[str, int], *, boosts_ok: bool
    ) -> list[tuple[str, int]]:
        tg = target.get(f"{b}/Gain")
        if tg is None:
            return []
        tg = max(-self.max_cut, min(tg, self.max_boost if boosts_ok else 0))
        cg = cur[f"{b}/Gain"]
        moves = [
            (p, target[f"{b}/{p}"])
            for p in ("Type", "Frequency", "Q")
            if f"{b}/{p}" in target
            and target[f"{b}/{p}"] != cur[f"{b}/{p}"]
            and not (p == "Frequency" and abs(math.log2(target[f"{b}/{p}"] / cur[f"{b}/{p}"])) < 1 / 48)
            and not (p == "Q" and abs(math.log(target[f"{b}/{p}"] / cur[f"{b}/{p}"])) < 0.1)
        ]
        if moves:
            if abs(cg) <= NEAR_ZERO:  # flat: move / retype it now, inaudibly
                return [(f"{b}/{p}", v) for p, v in moves] + ([(f"{b}/Gain", 0)] if cg else [])
            tg = 0  # fade it out first
        step = round(STEP_DB * 100)
        new = cg + max(-step, min(step, tg - cg))
        return [(f"{b}/Gain", new)] if new != cg else []

    @staticmethod
    def _hpf_step(cur: dict[str, int], target: dict[str, int]) -> list[tuple[str, int]]:
        tf = target.get("PEQ/Bank/HPF/Frequency")
        if tf is None:
            return []
        out: list[tuple[str, int]] = []
        if cur["PEQ/Bank/HPF/On"] != 1:  # switch it on low, then walk it up
            return [("PEQ/Bank/HPF/Frequency", 200), ("PEQ/Bank/HPF/On", 1)]
        cf = cur["PEQ/Bank/HPF/Frequency"]
        step = round(HPF_STEP_HZ * 10)
        new = cf + max(-step, min(step, tf - cf))
        if new != cf:
            out.append(("PEQ/Bank/HPF/Frequency", new))
        return out

    # ------------------------------------------------------------------ helpers

    def _writable(self) -> DeskEQ:
        if self.desk is None or not self.desk.eq_writable:
            raise PermissionError("writing to the desk is off (tf5.eq_write)")
        return self.desk

    @staticmethod
    def _log(ch: _Channel, action: str, changes: list[tuple[str, int]], why: str) -> None:
        ch.log.append(
            {
                "at": time.time(),
                "action": action,
                "why": why,
                "changes": [f"{p.removeprefix('PEQ/Bank/').removeprefix('PEQ/')} = {v}" for p, v in changes],
            }
        )
