"""Offline rigs for the vocal tests: the simulated stage run hop by hop, as fast as the
CPU allows (no real-time pacing), and a desk whose EQ the stage hears."""

import numpy as np

from tf_daemon.audio.stage import FlatDesk
from tf_daemon.config import Channel
from tf_daemon.dsp.ringlog import RingLog

FS, BLOCK, HOP, FRAME = 48000, 1200, 2400, 32768


class OfflineEngine:
    """Just enough of the audio engine for MicChecks: frames from the stage, hop by hop."""

    def __init__(self, source, analyzed):
        self.source, self.analyzed = source, analyzed
        self.inputs = [Channel(i, f"In {i + 1}") for i in range(source.channels)]
        self.rings = RingLog()
        self.frame_taps = []
        self.buf = np.zeros((FRAME, source.channels), np.float32)
        self.t = 0.0

    def add_tap(self, tap):
        pass

    def add_frame_tap(self, tap):
        self.frame_taps.append(tap)

    def acquire(self, index):
        pass

    def release(self, index):
        pass

    def run(self, seconds):
        hop = np.zeros((HOP, self.source.channels), np.float32)
        for _ in range(int(seconds * FS / HOP)):
            for j in range(0, HOP, BLOCK):
                self.source._fill(hop[j : j + BLOCK])
            self.buf = np.concatenate((self.buf[HOP:], hop))  # one copy per hop, not per block
            self.t += HOP / FS
            frames = {i: self.buf[:, i] for i in self.analyzed}
            for tap in self.frame_taps:
                tap(frames, self.t)


class RawDesk(FlatDesk):
    """Channel 0's EQ as the TF5 holds it (raw values); readable and writable like
    TF5Module (DeskEQ), and heard by the stage (DeskMix)."""

    eq_available = True
    eq_writable = True

    def __init__(self):
        self.raw = {"PEQ/On": 1, "PEQ/Bank/HPF/On": 0, "PEQ/Bank/HPF/Frequency": 800}
        for b, t, f in (("BandL", 1, 1250), ("Band:0", 0, 8000), ("Band:1", 0, 25000), ("BandH", 1, 80000)):
            self.raw.update(
                {f"PEQ/Bank/{b}/Type": t, f"PEQ/Bank/{b}/Frequency": f, f"PEQ/Bank/{b}/Gain": 0, f"PEQ/Bank/{b}/Q": 700}
            )
        self.writes = []

    def _eq(self):
        r, bands = self.raw, []
        for b, label, low, high in (
            ("BandL", "L", True, False),
            ("Band:0", "LM", False, False),
            ("Band:1", "HM", False, False),
            ("BandH", "H", False, True),
        ):
            t = r[f"PEQ/Bank/{b}/Type"]
            kind = (
                "bell"
                if t == 0 or not (low or high)
                else ("low_shelf" if low else "high_shelf")
                if t == 1
                else ("hpf" if low else "lpf")
            )
            bands.append(
                {
                    "label": label,
                    "type": kind,
                    "on": True,
                    "freq": r[f"PEQ/Bank/{b}/Frequency"] / 10,
                    "gain": r[f"PEQ/Bank/{b}/Gain"] / 100,
                    "q": r[f"PEQ/Bank/{b}/Q"] / 1000,
                }
            )
        return {
            "on": bool(r["PEQ/On"]),
            "bank": "PRECISE",
            "one_knob": False,
            "bands": bands,
            "hpf": {"on": bool(r["PEQ/Bank/HPF/On"]), "freq": r["PEQ/Bank/HPF/Frequency"] / 10, "slope": 12},
        }

    # DeskMix (the stage)
    def channel_eq(self, x):
        return self._eq() if x == 0 else None

    # DeskEQ (the vocal assist)
    def eq(self, usb):
        return {"eq": self._eq() if usb == 0 else None}

    def eq_target(self, usb):
        return "InputChannel:0" if usb == 0 else None

    def eq_raw(self, usb, paths):
        return {p: self.raw.get(p) for p in paths}

    def write_eq(self, usb, changes):
        self.writes.append(list(changes))
        for p, v in changes:
            self.raw[p] = v
        return [{"path": p, "value": v, "confirmed": True, "actual": v} for p, v in changes]
