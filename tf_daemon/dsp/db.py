"""Decibel conversions. Each caller keeps its own floor ``eps`` (what silence reads as)."""

from __future__ import annotations

from typing import overload

import numpy as np


def db_to_amp(db: float) -> float:
    return float(10.0 ** (db / 20.0))


@overload
def power_db(p: float, eps: float = ...) -> float: ...
@overload
def power_db(p: np.ndarray, eps: float = ...) -> np.ndarray: ...
def power_db(p: float | np.ndarray, eps: float = 1e-30) -> float | np.ndarray:
    """Power -> dB: ``10 log10(p + eps)``."""
    return 10.0 * np.log10(p + eps)


def amp_db(a: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    """Amplitude -> dB: ``20 log10(a + eps)``."""
    return 20.0 * np.log10(a + eps)
