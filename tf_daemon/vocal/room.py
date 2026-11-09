"""What the PA and the room do to the voice, from a house mic (``vocal.house_mic``).

The mic -> house transfer (transfer.py) is the voice's whole path to the
listener: the channel EQ, the fader, the PA and the room. Taking out the
channel EQ that's on the desk now (its exact curve is known) leaves the PA and
the room. ``room_correction`` returns that, in dB at the analysis points, with
the level removed (only the shape matters): added to the mic's spectrum, the
analysis (tone.py) sees the voice as the room hears it with the channel EQ
flat, and its suggestion becomes the channel EQ that makes the voice right in
the room.

Only bands where the house mic clearly hears this voice count (coherence at
least MIN_COHERENCE: the room's late reverb and everything else in the house
lower it); the others get no correction. With too few good bands the house mic
isn't used and the suggestion falls back to the mic alone.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .transfer import Transfer

MIN_COHERENCE = 0.4
MIN_GOOD_SHARE = 0.6     # share of the analysis points that must be trustworthy
MIN_FRAMES = 40          # ~2 s of speech before the room is trusted at all


def room_correction(t: Transfer, points: np.ndarray, eq_db: np.ndarray | None,
                    norm: tuple[float, float] = (200.0, 6300.0)) -> tuple[np.ndarray | None, dict[str, Any]]:
    """(correction dB at ``points`` or None, info). ``eq_db``: the channel EQ curve at
    ``points`` now (None: flat / unknown)."""
    summary = t.summary()
    info: dict[str, Any] = {**summary, "used": False}
    if t.delay is None or t.frames < MIN_FRAMES:
        info["note"] = "listening for the voice at the house mic…"
        return None, info
    gain, coh = t.bands(points, 1 / 3)
    good = (coh >= MIN_COHERENCE) & ~np.isnan(gain)
    info["good_share"] = round(float(good.mean()), 2)
    if good.mean() < MIN_GOOD_SHARE:
        info["note"] = (f"the house mic hears this voice clearly in only {good.mean():.0%} of the range "
                        "(too far, too much else going on?): suggesting from the mic alone")
        return None, info
    room = np.where(good, gain - (eq_db if eq_db is not None else 0.0), np.nan)
    band = (points >= norm[0]) & (points <= norm[1]) & good
    room = room - float(np.nanmedian(room[band] if band.any() else room))
    room = np.where(good, room, 0.0)
    info.update(used=True, curve=[round(float(v), 2) for v in room],
                note=f"aimed at the voice in the room (house mic, coherence {summary['coherence']:.2f})")
    return room, info
