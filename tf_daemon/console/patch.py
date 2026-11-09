"""Which console source each USB capture channel carries.

USB capture channels carry whatever the TF's USB output patch routes to them;
RCP doesn't expose that patch, so ``tf5.usb_patch`` describes it (default: the
TF's recording patch, USB 1-32 = input channels 1-32, USB 33-34 = ST L/R).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

Source = tuple[str, int]   # ("InCh", 0-based channel) or ("St", 0 = L / 1 = R)

TF_INPUT_CHANNELS = 40
_PATCH_RE = re.compile(r"^\s*(?:(?:InCh|Ch)\s*(\d+)|St\s*([LR]))\s*$", re.IGNORECASE)


def default_usb_patch() -> list[str]:
    return [f"InCh {n}" for n in range(1, 33)] + ["St L", "St R"]


def parse_patch_entry(entry: str) -> Source | None:
    """``"InCh 5"`` -> ("InCh", 4); ``"St R"`` -> ("St", 1); ``""`` -> None."""
    if not str(entry).strip():
        return None
    m = _PATCH_RE.match(str(entry))
    if not m:
        raise ValueError(f"unknown usb_patch entry {entry!r} (expected \"InCh N\", \"St L\", \"St R\" or \"\")")
    if m.group(1):
        n = int(m.group(1))
        if not 1 <= n <= TF_INPUT_CHANNELS:
            raise ValueError(f"usb_patch entry {entry!r}: input channel must be 1..{TF_INPUT_CHANNELS}")
        return "InCh", n - 1
    return "St", "LR".index(m.group(2).upper())


def patch_from(entries: Sequence[str]) -> list[Source | None]:
    """USB capture channel i -> the console source it carries (``tf5.usb_patch``;
    empty = the TF default)."""
    return [parse_patch_entry(e) for e in (entries or default_usb_patch())]


def source_label(src: Source) -> str:
    kind, x = src
    return f"InCh {x + 1}" if kind == "InCh" else f"St {'LR'[x]}"
