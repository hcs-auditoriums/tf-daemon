"""Lock-protected multichannel ring buffer.

The capture side (audio callback or synthetic generator thread) calls
``write_device_block`` with a full device block; only the analyzed columns are
copied in. The analysis thread waits on ``wait_for`` and pulls the most recent
frame with ``read_latest`` into a preallocated array. No allocation happens on
either path.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

import numpy as np


class RingBuffer:
    def __init__(self, columns: Sequence[int], capacity: int) -> None:
        self.columns = tuple(int(c) for c in columns)
        self.capacity = int(capacity)
        # Channel-major so each analyzed channel is one contiguous row.
        self._data = np.zeros((len(self.columns), self.capacity), dtype=np.float32)
        self._cond = threading.Condition(threading.Lock())
        self._pos = 0  # next write index
        self._written = 0  # total frames ever written
        self._closed = False

    @property
    def written(self) -> int:
        return self._written

    def write_device_block(self, block: np.ndarray) -> None:
        """Copy the analyzed columns of a (frames, device_channels) block in.

        Safe to call from a PortAudio callback: slice copies only, no
        allocation, no logging.
        """
        frames = block.shape[0]
        if frames > self.capacity:
            block = block[-self.capacity :]
            frames = self.capacity
        with self._cond:
            pos = self._pos
            first = min(frames, self.capacity - pos)
            rest = frames - first
            data = self._data
            for row, col in enumerate(self.columns):
                data[row, pos : pos + first] = block[:first, col]
                if rest:
                    data[row, :rest] = block[first:, col]
            self._pos = (pos + frames) % self.capacity
            self._written += frames
            self._cond.notify_all()

    def wait_for(self, target_written: int, timeout: float) -> int:
        """Block until at least ``target_written`` total frames exist.

        Returns the current written count (may be less on timeout/close).
        """
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._written >= target_written, timeout)
            return self._written

    def read_latest(self, out: np.ndarray, rows: Sequence[int] | None = None) -> int:
        """Fill ``out`` with the newest n frames. Returns written count.

        ``out`` is (channels, n), or (len(rows), n) to read only those rows
        (positions in ``columns``).
        """
        n = out.shape[1]
        if n > self.capacity:
            raise ValueError("read larger than ring capacity")
        with self._cond:
            start = (self._pos - n) % self.capacity
            first = min(n, self.capacity - start)
            if rows is None:
                out[:, :first] = self._data[:, start : start + first]
                if first < n:
                    out[:, first:] = self._data[:, : n - first]
            else:
                for i, r in enumerate(rows):
                    out[i, :first] = self._data[r, start : start + first]
                    if first < n:
                        out[i, first:] = self._data[r, : n - first]
            return self._written

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
