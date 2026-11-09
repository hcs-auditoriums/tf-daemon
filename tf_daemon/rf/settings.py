"""RF sweep settings: the validated start / stop / points / RBW / interval of a sweep.

Kept apart from the engine so the config loader can validate [rf] without
importing it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .devices import MAX_POINTS

RBW_CHOICES = ("auto", "3", "10", "30", "100", "300", "600", "850")  # kHz
MAX_HZ = 15_000_000_000


@dataclass(frozen=True)
class SweepSettings:
    start_hz: int
    stop_hz: int
    points: int
    rbw: str
    interval_s: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SweepSettings:
        try:
            s = cls(int(d["start_hz"]), int(d["stop_hz"]), int(d["points"]), str(d["rbw"]), float(d["interval_s"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"invalid sweep settings: {e}") from None
        if not 0 <= s.start_hz < s.stop_hz <= MAX_HZ:
            raise ValueError(f"need 0 <= start_hz < stop_hz <= {MAX_HZ}")
        if not 2 <= s.points <= MAX_POINTS:
            raise ValueError(f"points must be between 2 and {MAX_POINTS}")
        if s.rbw not in RBW_CHOICES:
            raise ValueError(f"rbw must be one of {', '.join(RBW_CHOICES)}")
        if not 0.1 <= s.interval_s <= 60:
            raise ValueError("interval_s must be between 0.1 and 60")
        return s

    def updated(self, d: dict[str, Any]) -> SweepSettings:
        """These settings with any keys present in ``d`` replaced, validated."""
        known = {k: d[k] for k in asdict(self) if d.get(k) is not None}
        return SweepSettings.from_dict({**asdict(self), **known})
