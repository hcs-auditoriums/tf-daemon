"""Feature-module interface.

Each feature area (audio analysis today; RF / wireless mic monitoring later)
is a self-contained module that owns its own threads and hardware and
registers its HTTP/WebSocket routes on the shared Flask app. The app knows
nothing about module internals.
"""

from __future__ import annotations

from typing import Any, Protocol

from flask import Flask
from flask_sock import Sock


class Module(Protocol):
    name: str

    def register(self, app: Flask, sock: Sock) -> None:
        """Add blueprints / websocket routes. Called once before start()."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...
