"""The ring log: the detector's rings (ring.py) as events the pages show, with which
inputs feed a ring heard on the mains (attribution)."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any

from .ring import KIND_RANK, geq_band


@dataclass
class RingEvent:
    id: int
    channel: int
    label: str
    freq: float
    kind: str
    level: float  # loudest seen
    prominence: float  # largest seen
    first_seen: float  # wall clock
    last_seen: float
    count: int = 1  # separate occurrences
    active: bool = True
    # Inputs feeding this ring (rings on a bus): input -> smoothed contribution (dB), and labels.
    contrib: dict[int, float] = field(default_factory=dict)
    contrib_labels: dict[int, str] = field(default_factory=dict)
    contrib_basis: str = ""  # "fader": level at the ring frequency + fader; "level": level only

    SOURCES_WITHIN_DB = 12.0
    MAX_SOURCES = 3

    def sources(self) -> list[dict[str, Any]]:
        if not self.contrib:
            return []
        ranked = sorted(self.contrib.items(), key=lambda kv: -kv[1])
        top = ranked[0][1]
        return [
            {"channel": i, "label": self.contrib_labels.get(i, f"Ch {i + 1}"), "rel_db": round(c - top, 1)}
            for i, c in ranked[: self.MAX_SOURCES]
            if c >= top - self.SOURCES_WITHIN_DB
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "label": self.label,
            "freq": self.freq,
            "kind": self.kind,
            "level": self.level,
            "prominence": self.prominence,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "count": self.count,
            "active": self.active,
            "geq": geq_band(self.freq),
            "sources": self.sources(),
            "sources_basis": self.contrib_basis,
        }


class RingLog:
    """Ring events across inputs, for the UI. Thread-safe.

    A ring that comes back on the same input at the same frequency (within 1%)
    within ``merge_s`` is the same event with ``count`` incremented: a ring that
    keeps coming back is the one to EQ out. An event ends only after ``GRACE_S``
    unseen, so a detection that flickers for a few frames isn't a "comeback".
    """

    GRACE_S = 1.5
    MATCH = 0.01
    IGNORE_MATCH = 0.015
    KEEP = 40

    def __init__(self, merge_s: float = 300.0) -> None:
        self.merge_s = merge_s
        self._lock = threading.Lock()
        self._events: list[RingEvent] = []
        self._ignored: list[float] = []
        self._next_id = 1
        self.version = 0

    def update(self, channel: int, label: str, rings: list[dict[str, Any]], now: float) -> None:
        with self._lock:
            changed = False
            seen: set[int] = set()
            for r in rings:
                if any(abs(r["freq"] - f) <= self.IGNORE_MATCH * f for f in self._ignored):
                    continue
                ev = next(
                    (
                        e
                        for e in self._events
                        if e.channel == channel
                        and e.id not in seen
                        and abs(e.freq - r["freq"]) <= self.MATCH * e.freq
                        and (e.active or now - e.last_seen <= self.merge_s)
                    ),
                    None,
                )
                if ev is None:
                    ev = RingEvent(
                        self._next_id, channel, label, r["freq"], r["kind"], r["level"], r["prominence"], now, now
                    )
                    self._next_id += 1
                    self._events.append(ev)
                    changed = True
                else:
                    if not ev.active:
                        ev.count += 1
                        ev.active = True
                        changed = True
                    elif now - ev.last_seen > self.GRACE_S:
                        ev.count += 1  # gone long enough to be a new occurrence
                        changed = True
                    if KIND_RANK[r["kind"]] > KIND_RANK[ev.kind]:
                        ev.kind = r["kind"]
                        changed = True
                    ev.freq = r["freq"]
                    ev.level = max(ev.level, r["level"])
                    ev.prominence = max(ev.prominence, r["prominence"])
                    ev.last_seen = now
                    ev.label = label
                seen.add(ev.id)
            for e in self._events:
                if e.channel == channel and e.active and e.id not in seen and now - e.last_seen >= self.GRACE_S:
                    e.active = False
                    changed = True
            if len(self._events) > self.KEEP:
                self._events = sorted(self._events, key=lambda e: (e.active, e.last_seen))[-self.KEEP :]
            if changed:
                self.version += 1

    def active_on(self, channels: frozenset[int] | set[int]) -> list[tuple[int, float]]:
        """(channel, freq) of the live rings on these inputs."""
        with self._lock:
            return [(e.channel, e.freq) for e in self._events if e.active and e.channel in channels]

    def set_sources(self, channel: int, freq: float, contributions: list[tuple[int, str, float]], basis: str) -> None:
        """Contribution (dB) of each input to the live ring at ``freq`` on ``channel``.

        Smoothed in the power domain over the ring's life, so one noisy
        measurement doesn't reorder the list.
        """
        with self._lock:
            ev = next(
                (
                    e
                    for e in self._events
                    if e.channel == channel and e.active and abs(e.freq - freq) <= self.MATCH * e.freq
                ),
                None,
            )
            if ev is None:
                return
            before = [s["channel"] for s in ev.sources()]
            seen = set()
            for i, label, db in contributions:
                seen.add(i)
                ev.contrib_labels[i] = label
                p_new = 10 ** (db / 10)
                p_old = 10 ** (ev.contrib[i] / 10) if i in ev.contrib else p_new
                ev.contrib[i] = 10 * math.log10(0.6 * p_old + 0.4 * p_new)
            for i in list(ev.contrib):
                if i not in seen:  # muted or gone: no longer feeding the ring
                    del ev.contrib[i]
            ev.contrib_basis = basis
            if [s["channel"] for s in ev.sources()] != before:
                self.version += 1

    def end_channel(self, channel: int) -> None:
        """The input stopped being analyzed: its rings are no longer live."""
        with self._lock:
            for e in self._events:
                if e.channel == channel and e.active:
                    e.active = False
                    self.version += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = sorted(self._events, key=lambda e: (not e.active, -e.last_seen))
            return {
                "type": "rings",
                "version": self.version,
                "events": [e.to_dict() for e in events],
                "ignored": list(self._ignored),
            }

    def clear(self) -> None:
        """Forget past events (live ones stay)."""
        with self._lock:
            self._events = [e for e in self._events if e.active]
            self.version += 1

    def ignore(self, freq: float) -> None:
        with self._lock:
            if not any(abs(freq - f) <= self.IGNORE_MATCH * f for f in self._ignored):
                self._ignored.append(round(freq, 1))
            self._events = [e for e in self._events if abs(e.freq - freq) > self.IGNORE_MATCH * freq]
            self.version += 1

    def unignore(self, freq: float | None = None) -> None:
        with self._lock:
            self._ignored = [] if freq is None else [f for f in self._ignored if abs(f - freq) > self.IGNORE_MATCH * f]
            self.version += 1
