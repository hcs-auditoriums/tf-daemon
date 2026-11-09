"""The desk changes that turn a channel's EQ into the vocal assist's suggestion (pure: no I/O)."""

from __future__ import annotations

from typing import Any


def _band_path(label: str) -> str:
    """Suggestion / console band label -> PEQ path relative to the channel."""
    if label in ("L", "H"):
        return f"PEQ/Bank/Band{label}"
    return f"PEQ/Bank/Band:{int(label) - 1}"


def eq_plan(result: dict[str, Any], current: dict[str, Any]) -> tuple[list[tuple[str, int]], list[str]]:
    """The desk changes that make the channel's EQ the suggestion, as (path, raw value), and a
    line per change for the person confirming. Suggested bands become bells with the
    suggested settings; the other bands are set flat (0 dB) so the desk matches the
    suggested curve; a low / high band used as a filter (HPF / LPF) is left alone."""
    changes: list[tuple[str, int]] = []
    lines: list[str] = []
    if not current["on"]:
        changes.append(("PEQ/On", 1))
        lines.append("EQ: on")
    hpf = result.get("hpf")
    if hpf and current.get("hpf") is not None:
        changes += [("PEQ/Bank/HPF/On", 1), ("PEQ/Bank/HPF/Frequency", round(hpf["freq"] * 10))]
        lines.append(f"HPF: on, {hpf['freq']:g} Hz")
    suggested = {b["band"]: b for b in result.get("bands", [])}
    labels = {b["label"] for b in current["bands"]}
    if set(suggested) - labels:
        raise ValueError(f"the desk channel has no band {', '.join(sorted(set(suggested) - labels))}")
    for b in current["bands"]:
        label, base, m = b["label"], _band_path(b["label"]), suggested.get(b["label"])
        if m is not None:
            if label in ("L", "H"):
                changes.append((f"{base}/Type", 0))   # bell
            q = min(max(float(m["q"]), 0.1), 10.0)
            changes += [(f"{base}/Frequency", round(m["freq"] * 10)), (f"{base}/Gain", round(m["gain"] * 10) * 10),
                        (f"{base}/Q", round(q * 1000))]
            lines.append(f"{label}: bell {m['gain']:+.1f} dB @ {m['freq']:g} Hz, Q {q:g}")
        elif b["type"] in ("hpf", "lpf"):
            lines.append(f"{label}: {b['type'].upper()} left as it is")
        elif b["gain"] != 0:
            changes.append((f"{base}/Gain", 0))
            lines.append(f"{label}: flat (was {b['gain']:+.1f} dB)")
    return changes, lines
