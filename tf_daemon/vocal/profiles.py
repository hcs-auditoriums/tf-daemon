"""EQ profiles: a channel's whole EQ saved under a name (e.g. per actor), to recall onto
any channel later, plus copying EQ straight from one channel to another.

A profile is the channel EQ as the TF5 holds it: the raw values of EQ on, the
high-pass and every band's type, frequency, gain and Q (auto.PATHS), so a recall
puts back exactly what was saved. Profiles live in ``vocal.profiles_file`` (JSON,
written atomically) and survive restarts. Saving only reads the desk; recall and
copy change it, so they go through DeskWriter.write_values (a person's confirmed
request, with Undo).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..files import write_atomic
from .auto import PATHS

log = logging.getLogger(__name__)

MAX_NAME = 40
MAX_PROFILES = 200


class ProfileStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._profiles: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable EQ profiles file %s: %s", self.path, e)
            return {}
        out = {}
        for name, p in (data.get("profiles") or {}).items():
            values = p.get("values") if isinstance(p, dict) else None
            if isinstance(values, dict) and all(isinstance(values.get(k), int) for k in PATHS):
                out[name] = {**p, "values": {k: values[k] for k in PATHS}}
        return out

    def _save(self) -> None:
        write_atomic(self.path, json.dumps({"profiles": self._profiles}, indent=1), parents=True)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._profiles.items(), key=lambda kv: kv[0].lower())
        return [
            {"name": n, "saved_at": p.get("saved_at"), "from": p.get("from"), "summary": summary(p["values"])}
            for n, p in items
        ]

    def get(self, name: Any) -> dict[str, int]:
        with self._lock:
            p = self._profiles.get(name) if isinstance(name, str) else None
        if p is None:
            raise ValueError(f"no profile called {name!r}")
        return dict(p["values"])

    def save(self, name: str, values: dict[str, Any], source: str | None) -> dict[str, Any]:
        name = clean_name(name)
        if any(not isinstance(values.get(k), int) for k in PATHS):
            raise ValueError("couldn't read the channel's EQ from the desk")
        with self._lock:
            if name not in self._profiles and len(self._profiles) >= MAX_PROFILES:
                raise ValueError(f"at most {MAX_PROFILES} profiles; delete some first")
            self._profiles[name] = {
                "values": {k: int(values[k]) for k in PATHS},
                "saved_at": time.time(),
                "from": source,
            }
            self._save()
        return {"name": name}

    def delete(self, name: Any) -> None:
        with self._lock:
            if not isinstance(name, str) or self._profiles.pop(name, None) is None:
                raise ValueError(f"no profile called {name!r}")
            self._save()


def clean_name(name: Any) -> str:
    if not isinstance(name, str) or not (n := re.sub(r"\s+", " ", name).strip()):
        raise ValueError("a profile needs a name")
    if len(n) > MAX_NAME:
        raise ValueError(f"profile names are at most {MAX_NAME} characters")
    return n


def changes_to(current: dict[str, Any], values: dict[str, int]) -> list[tuple[str, int]]:
    """The writes that turn ``current`` (raw) into ``values``: only what differs, the band
    types first (a type change resets nothing else, but reads more naturally first)."""
    order = sorted(PATHS, key=lambda p: (not p.endswith("/Type"), PATHS.index(p)))
    return [(p, values[p]) for p in order if current.get(p) != values[p]]


def summary(values: dict[str, int]) -> str:
    """'HPF 90 Hz · L -4.0 @ 230 · HM -3.0 @ 2.5k' for a list."""

    def f(raw: int) -> str:
        hz = raw / 10
        return f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:.0f}"

    parts = [] if values.get("PEQ/On") else ["EQ off"]
    if values.get("PEQ/Bank/HPF/On"):
        parts.append(f"HPF {f(values['PEQ/Bank/HPF/Frequency'])} Hz")
    for band, label in (("BandL", "L"), ("Band:0", "LM"), ("Band:1", "HM"), ("BandH", "H")):
        g = values.get(f"PEQ/Bank/{band}/Gain", 0) / 100
        if g:
            parts.append(f"{label} {g:+.1f} @ {f(values[f'PEQ/Bank/{band}/Frequency'])}")
    return " · ".join(parts) or "flat"
