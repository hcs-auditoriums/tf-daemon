"""Feature-module interface.

Each feature area (audio analysis today; RF / wireless mic monitoring later)
is a self-contained module that owns its own threads and hardware and
registers its HTTP/WebSocket routes on the shared Flask app. The app knows
nothing about module internals.
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

from flask import Blueprint, Flask, jsonify
from flask_sock import ConnectionClosed, Sock

from .hub import FrameHub


class Module(Protocol):
    name: str

    def register(self, app: Flask, sock: Sock) -> None:
        """Add blueprints / websocket routes. Called once before start()."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


class Engine(Protocol):
    hub: FrameHub

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, Any]: ...

    def snapshot(self) -> dict[str, Any]: ...


class EngineModule:
    """A module that wraps one engine publishing JSON state on a hub.

    Routes: ``GET /<name>`` (the page ``<name>.html``), ``GET /api/<name>/status``,
    ``GET /api/<name>/state`` and ``WS /ws/<name>``; subclasses add their own routes
    to the blueprint in ``routes``."""

    name: str

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def start(self) -> None:
        self.engine.start()

    def stop(self) -> None:
        self.engine.stop()

    def status(self) -> dict[str, Any]:
        return self.engine.status()

    def routes(self, bp: Blueprint) -> None:
        """Add this module's own routes under ``/api/<name>``."""

    def register(self, app: Flask, sock: Sock) -> None:
        engine, name = self.engine, self.name
        bp = Blueprint(name, __name__, url_prefix=f"/api/{name}")
        app.add_url_rule(f"/{name}", f"{name}_page", lambda: app.send_static_file(f"{name}.html"))
        bp.add_url_rule("/status", "status", lambda: jsonify(engine.status()))
        bp.add_url_rule("/state", "state", lambda: jsonify(engine.snapshot()))
        self.routes(bp)
        app.register_blueprint(bp)

        def ws(ws: Any) -> None:
            # The hub always holds a full state snapshot, so a new client gets it at once.
            with contextlib.suppress(ConnectionClosed):
                engine.hub.stream(ws.send)

        sock.route(f"/ws/{name}", endpoint=f"ws_{name}")(ws)
