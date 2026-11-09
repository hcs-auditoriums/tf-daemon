"""A simulated stage: talkers, the desk, the PA and the room, in a closed loop.

The ``stage`` audio source (``--demo`` uses it) produces what the TF5's USB
capture would carry in a small venue, so the vocal EQ assist can be developed
and tested without one:

  talker -> mic -> desk channel (its current EQ and fader) -> mains -> PA
              ^                                                        |
              +---------- PA -> mic leak (delay, resonances) <---------+
                                                                       |
                                     house mic <- room (delay, reverb) +

* Talkers: the named vocal inputs take turns (one talks at a time). Each voice
  has its own colour (boomy, nasal, ...) so the analysis has something to find.
* Each mic hears its talker, stage noise, and the PA after the PA-to-mic delay,
  through a leak path with a couple of resonances. Every 5 ms step the channels
  go through their *current* desk EQ (read from the desk, e.g. the simulated
  TF5) and faders into the mains, and the mains feed the leak. The loop delay
  (~17 ms) is longer than a step, so this is a real loop: push a fader or boost
  the EQ at a leak resonance and it rings, then runs away (a limiter keeps the
  mains bounded).
* The mains go out on the stereo-bus USB channels; the house mic (``synth.house_mic``)
  hears the mains through the room plus a little of the talker directly.

Levels follow SynthSource (``synth.speech_db`` at the mic, ``synth.noise_db``).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from ..config import REQUIRED_SAMPLERATE
from ..dsp.eq import eq_curve, section_db
from .sources import VoiceGen, _db_to_amp, _PacedSource, make_pink_noise

SPEED_OF_SOUND = 343.0
STEP = 240          # samples per loop step (5 ms): the leak delay must be at least this
EQ_TAPS = 512       # linear-phase FIR per channel EQ (adds 5.3 ms, like a real desk's latency)
LEAK_TAPS = 256
LIMIT = 0.9         # mains soft limit (sine-peak full scale = 1)

# Colour of each talker's voice (bells: freq, gain dB, Q), cycled over the talkers.
TALKER_COLOURS = [
    [(240.0, 6.0, 1.2), (3200.0, -3.0, 1.0)],     # boomy, a little dull
    [(1100.0, 5.0, 2.0), (4200.0, 4.0, 3.0)],     # nasal, edgy
    [(600.0, 4.0, 1.5)],                          # woody
]
# The PA -> mic path: where this stage wants to ring first (freq, gain dB, Q).
LEAK_RESONANCES = [(820.0, 6.0, 8.0), (2480.0, 8.0, 10.0)]
# What the PA and the room do to sound reaching the house mic (kind, freq, gain dB, Q):
# a boomy low-mid build-up and a dull top end.
ROOM_COLOUR = [("bell", 300.0, 6.0, 1.2), ("high_shelf", 5000.0, -5.0, 0.7)]


class DeskMix(Protocol):
    """What the stage needs from the desk (console.sim.SimConsole, or FlatDesk)."""

    def channel_eq(self, x: int) -> dict[str, Any] | None: ...   # console.editor.dump.channel_eq shape

    def fader_db(self, x: int) -> float | None: ...              # None: channel off

    def master_db(self) -> float: ...


class FlatDesk:
    """No console: flat EQ, faders at 0 dB."""

    def channel_eq(self, x: int) -> dict[str, Any] | None:
        return None

    def fader_db(self, x: int) -> float | None:
        return 0.0

    def master_db(self) -> float:
        return 0.0


@dataclass
class StageSettings:
    talkers: list[int]                 # inputs with a talker on them
    mains: tuple[int, int]             # inputs carrying the stereo bus (L, R)
    house_mic: int | None              # input carrying the house mic
    desk_channel: Callable[[int], int | None] = lambda i: i   # input -> console input channel
    speech_db: float = -20.0
    noise_db: float = -50.0
    tone_hz: float = 2500.0
    tone_db: float = -15.0
    pa_leak_db: float = -18.0          # PA level reaching an open mic, relative to the mains
    pa_distance_m: float = 6.0
    house_distance_m: float = 12.0
    room_rt60: float = 0.8
    turn_s: tuple[float, float] = (6.0, 12.0)
    seed: int = 1
    colours: list[list[tuple[float, float, float]]] = field(default_factory=lambda: TALKER_COLOURS)
    room_colour: list[tuple[str, float, float, float]] = field(default_factory=lambda: ROOM_COLOUR)


def fir_from_db(db: np.ndarray, taps: int) -> np.ndarray:
    """Linear-phase FIR (``taps`` long, delay taps/2) with the magnitude ``db`` given on
    ``np.fft.rfftfreq(taps)``."""
    h = np.fft.irfft(10 ** (np.asarray(db) / 20), taps)
    return np.roll(h, taps // 2) * np.hanning(taps)


def _bells_db(freqs: np.ndarray, bells: list[tuple[float, float, float]], fs: float) -> np.ndarray:
    out = np.zeros(len(freqs))
    for f, g, q in bells:
        out += section_db("bell", f, g, q, freqs, fs)
    return out


class _Conv:
    """Streaming direct convolution with an FIR (for short filters)."""

    def __init__(self, h: np.ndarray) -> None:
        self.h = h
        self.tail = np.zeros(len(h) - 1)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = np.convolve(x, self.h)
        y[:len(self.tail)] += self.tail
        self.tail = y[len(x):].copy()
        return y[:len(x)]


class _FFTConv:
    """Streaming FFT (overlap-add) convolution, for long filters such as a room."""

    def __init__(self, h: np.ndarray, block: int) -> None:
        self.n = 1 << math.ceil(math.log2(block + len(h) - 1))
        self.H = np.fft.rfft(h, self.n)
        self.tail = np.zeros(len(h) - 1)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = np.fft.irfft(np.fft.rfft(x, self.n) * self.H, self.n)[:len(x) + len(self.tail)]
        y[:len(self.tail)] += self.tail
        self.tail = y[len(x):].copy()
        return y[:len(x)]


class _Talker:
    def __init__(self, index: int, colour: list[tuple[float, float, float]], rng: np.random.Generator,
                 fs: int, blocksize: int, f0_range: tuple[float, float]) -> None:
        self.index = index
        self.voice = VoiceGen(rng, fs, blocksize, f0_range)
        freqs = np.fft.rfftfreq(EQ_TAPS, 1 / fs)
        freqs[0] = freqs[1] / 2
        # A gentle speech-like tilt above 1 kHz, plus this talker's colour.
        tilt = section_db("high_shelf", 2000.0, -6.0, 0.7, freqs, fs)
        self.colour = _Conv(fir_from_db(tilt + _bells_db(freqs, colour, fs), EQ_TAPS))
        self.eq = _Conv(fir_from_db(np.zeros(len(freqs)), EQ_TAPS))
        self.eq_key: Any = object()
        self.gain = 1.0
        self.noise_pos = 0


class StageSource(_PacedSource):
    name = "stage"

    def __init__(self, channels: int, blocksize: int, settings: StageSettings, desk: DeskMix | None = None,
                 samplerate: int = REQUIRED_SAMPLERATE) -> None:
        super().__init__(channels, blocksize, samplerate)
        s = self.settings = settings
        self.desk: DeskMix = desk or FlatDesk()
        fs = samplerate
        for i in [*s.talkers, *s.mains, *([s.house_mic] if s.house_mic is not None else [])]:
            if not 0 <= i < channels:
                raise ValueError(f"stage input {i} is outside the {channels} capture channels")
        self._rng = np.random.default_rng(s.seed)
        self._speech_amp = _db_to_amp(s.speech_db) / np.sqrt(2.0)
        self._noise_amp = _db_to_amp(s.noise_db) / np.sqrt(2.0)
        self._tone_peak = _db_to_amp(s.speech_db + s.tone_db)
        self._tone_w = 2 * np.pi * s.tone_hz / fs
        self._tone_phase = 0.0
        self._t = np.arange(1, blocksize + 1, dtype=np.float64)
        self._noise = make_pink_noise(1 << 19, self._rng)
        ranges = [(85.0, 160.0), (160.0, 280.0), (110.0, 200.0)]
        self.talkers = [_Talker(i, s.colours[k % len(s.colours)], self._rng, fs, blocksize, ranges[k % len(ranges)])
                        for k, i in enumerate(s.talkers)]
        for t in self.talkers:
            t.noise_pos = int(self._rng.integers(0, len(self._noise)))

        # Turn-taking: who talks now, and for how much longer.
        self.speaking: int | None = 0 if self.talkers else None
        self._turn_left = int(fs * self._rng.uniform(*s.turn_s))
        self._turn_env = np.zeros(len(self.talkers))

        # The PA -> mic leak: delay, gain and resonances.
        self.leak_delay = max(STEP, round(s.pa_distance_m / SPEED_OF_SOUND * fs))
        freqs = np.fft.rfftfreq(LEAK_TAPS, 1 / fs)
        freqs[0] = freqs[1] / 2
        self._leak_h = fir_from_db(s.pa_leak_db + _bells_db(freqs, LEAK_RESONANCES, fs), LEAK_TAPS)
        self._pa = np.zeros(self.leak_delay + LEAK_TAPS + STEP)   # mains history, newest last

        # The room, as heard by the house mic: the PA arriving after the distance,
        # then an exponentially decaying diffuse tail.
        self._room = None
        self._direct_delay = round(s.house_distance_m / SPEED_OF_SOUND * fs)
        self._direct = np.zeros(self._direct_delay + blocksize)
        if s.house_mic is not None:
            n = int(fs * min(1.0, 0.8 * s.room_rt60))
            h = np.zeros(self._direct_delay + n)
            h[self._direct_delay] = 0.5
            secs = np.arange(n) / fs
            tail = self._rng.standard_normal(n) * np.exp(-6.91 * secs / s.room_rt60)
            tail[: int(0.005 * fs)] = 0.0                       # first reflections after ~5 ms
            tail *= 0.5 * math.sqrt(0.5 / max(float(np.sum(tail ** 2)), 1e-12))   # reverb ~ -3 dB re the direct
            h[self._direct_delay:] += tail
            if s.room_colour:   # the PA and the room's colour, as heard at the house mic
                freqs = np.fft.rfftfreq(EQ_TAPS, 1 / fs)
                freqs[0] = freqs[1] / 2
                colour = sum((section_db(k, f, g, q, freqs, fs) for k, f, g, q in s.room_colour), np.zeros(len(freqs)))
                h = np.convolve(h, fir_from_db(colour, EQ_TAPS))
            self._room = _FFTConv(h, blocksize)
            self._house_noise_pos = int(self._rng.integers(0, len(self._noise)))
        self.mains_peak = 0.0

    # ------------------------------------------------------------------ desk

    def _update_desk(self) -> None:
        for t in self.talkers:
            x = self.settings.desk_channel(t.index)
            eq = self.desk.channel_eq(x) if x is not None else None
            fader = self.desk.fader_db(x) if x is not None else None
            t.gain = 0.0 if fader is None else 10 ** (fader / 20)
            key = repr(eq)
            if key != t.eq_key:
                t.eq_key = key
                freqs = np.fft.rfftfreq(EQ_TAPS, 1 / self.samplerate)
                freqs[0] = freqs[1] / 2
                t.eq.h = fir_from_db(eq_curve(eq, freqs), EQ_TAPS)

    # ------------------------------------------------------------------ signal

    def _noise_block(self, pos: int, n: int) -> tuple[np.ndarray, int]:
        L = len(self._noise)
        idx = (pos + np.arange(n)) % L if pos + n > L else slice(pos, pos + n)
        return self._noise[idx] * self._noise_amp, (pos + n) % L

    def _turns(self, n: int) -> np.ndarray:
        """Per-talker gate for this block (one talks at a time, short gaps between)."""
        self._turn_left -= n
        if self._turn_left <= 0 and self.talkers:
            if self.speaking is None:      # gap over: someone (maybe the same) talks
                self.speaking = int(self._rng.integers(0, len(self.talkers)))
                self._turn_left = int(self.samplerate * self._rng.uniform(*self.settings.turn_s))
            else:                          # a short pause between talkers
                self.speaking = None
                self._turn_left = int(self.samplerate * self._rng.uniform(0.5, 2.0))
        target = np.zeros(len(self.talkers))
        if self.speaking is not None:
            target[self.speaking] = 1.0
        start = self._turn_env.copy()
        self._turn_env = start + (target - start) * (1 - math.exp(-n / (0.05 * self.samplerate)))
        ramp = self._t[:n] / n
        return start[:, None] + (self._turn_env - start)[:, None] * ramp[None, :]

    def _fill(self, block: np.ndarray) -> None:
        n = block.shape[0]
        s = self.settings
        self._update_desk()
        gates = self._turns(n)
        ph = self._tone_phase + self._tone_w * self._t[:n]
        tone = self._tone_peak * np.sin(ph)
        self._tone_phase = float(ph[-1] % (2 * np.pi))

        dry = []
        for k, t in enumerate(self.talkers):
            v = t.colour(t.voice.block(n) * self._speech_amp) * gates[k]
            noise, t.noise_pos = self._noise_block(t.noise_pos, n)
            dry.append(v + noise + tone)
        mics = np.zeros((len(self.talkers), n))
        mains = np.zeros(n)
        master = 10 ** (self.desk.master_db() / 20)
        d, L = self.leak_delay, LEAK_TAPS
        for a in range(0, n, STEP):
            b = min(a + STEP, n)
            m = b - a
            end = len(self._pa) - (d - m)
            leak = np.convolve(self._pa[end - (m + L - 1):end], self._leak_h, "valid")
            mix = np.zeros(m)
            for k, t in enumerate(self.talkers):
                mics[k, a:b] = dry[k][a:b] + leak
                mix += t.eq(mics[k, a:b]) * t.gain
            out = LIMIT * np.tanh(mix * master / LIMIT)
            mains[a:b] = out
            self._pa = np.concatenate((self._pa[m:], out))
        self.mains_peak = max(self.mains_peak * 0.9, float(np.max(np.abs(mains))) if n else 0.0)

        for k, t in enumerate(self.talkers):
            block[:, t.index] = mics[k]
        block[:, s.mains[0]] = mains
        block[:, s.mains[1]] = mains
        if self._room is not None and s.house_mic is not None:
            direct_in = sum((dry[k] for k in range(len(self.talkers))), np.zeros(n))
            self._direct = np.concatenate((self._direct[n:], direct_in))
            direct = self._direct[: n] * 0.1                      # the talker heard directly (-20 dB)
            noise, self._house_noise_pos = self._noise_block(self._house_noise_pos, n)
            block[:, s.house_mic] = self._room(mains) + direct + noise * 0.5
