"""Latest-value broadcast hub shared by the feature modules."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class FrameHub:
    """Holds only the latest published frame; readers wait for a newer one.

    Payloads are opaque (bytes for binary WebSocket frames, str for JSON text).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seq = 0
        self._payload: Any = None
        self._closed = False

    def publish(self, payload: Any) -> None:
        with self._cond:
            self._seq += 1
            self._payload = payload
            self._cond.notify_all()

    def wait(self, last_seq: int, timeout: float) -> tuple[int, Any | None]:
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._seq != last_seq, timeout)
            if self._seq == last_seq or self._closed:
                return last_seq, None
            return self._seq, self._payload

    def stream(self, send: Callable[[Any], None], timeout: float = 1.0) -> None:
        """Send each new payload, starting with the current one, until the hub closes or
        ``send`` raises (the client went away: the caller catches that)."""
        seq = 0
        while not self._closed:
            seq, payload = self.wait(seq, timeout)
            if payload is not None:
                send(payload)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
