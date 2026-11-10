"""WS /ws/tf5: the Desk and Mix pages' data, pushed when it changes instead of polled.

The page says what it shows with ``{"areas": [...]}`` (on open, and again whenever that
changes); the server sends each of those areas at once as ``{"type": <area>, "version", ...}``
and again whenever its version changes. Versions are the editor client's change counters, so
checking them every CHECK_S is in-process work only: no desk traffic, no HTTP requests.

Areas (each the same data as its GET route):
  status                              GET /api/tf5/status (at most once a second, when it differs)
  scenes                              GET /api/tf5/scenes?all=1 (every slot; the page filters)
  recorder, fx, delay, selected       GET /api/tf5/<area> (console/desk.py)
  mix                                 GET /api/tf5/mix (console/buses.py)
An area that can't be read (tf5.eq off, an unknown name) comes once as ``{"type", "error"}``.

Routes:
  WS /ws/tf5    {"areas": [...]} from the page; {"type": area, ...} pushes from the server
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flask_sock import ConnectionClosed, Sock

from . import desk, scenes

if TYPE_CHECKING:
    from .buses import Buses
    from .editor.client import TFEditorClient

CHECK_S = 0.25  # how often a connection compares versions (in-process)
STATUS_S = 1.0  # the status area: its content has no version, so it is compared at most this often

Area = tuple[Callable[[], Any], Callable[[], dict[str, Any]]]  # (version, build)


class Feed:
    """What each area is and how to tell it changed; ``register`` adds the websocket."""

    def __init__(
        self,
        status: Callable[[], dict[str, Any]],
        client: Callable[[], TFEditorClient | None],
        buses: Buses,
    ) -> None:
        self.status, self.client, self.buses = status, client, buses

    def area(self, name: str) -> Area | None:
        """``(version, build)`` for an area, or None if it can't be read now."""
        if name == "mix":
            return self.buses.version, self.buses.snapshot
        ec = self.client()
        if ec is None:
            return None
        if name == "scenes":
            return (lambda: desk.version(ec, scenes.FUNCS)), (lambda: scenes.scenes(ec, include_empty=True))
        if name in desk.AREAS:
            funcs, build = desk.AREAS[name]
            return (lambda: desk.version(ec, funcs)), (lambda: build(ec))
        return None

    def stream(self, ws: Any) -> None:
        """Serve one page until it goes away."""
        wanted: list[str] = []
        sent: dict[str, Any] = {}  # area -> the version (or for status, the JSON) last sent
        status_at = 0.0
        while True:
            msg = ws.receive(timeout=CHECK_S)
            if msg:
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    areas = json.loads(msg).get("areas")
                    if isinstance(areas, list):
                        wanted = [a for a in areas if isinstance(a, str)]
                        sent = {a: v for a, v in sent.items() if a in wanted}  # a re-added area comes at once
            now = time.monotonic()
            for name in wanted:
                if name == "status":
                    if now - status_at < STATUS_S and "status" in sent:
                        continue
                    status_at = now
                    text = json.dumps(self.status(), sort_keys=True, default=str)
                    if sent.get(name) != text:
                        sent[name] = text
                        ws.send(f'{{"type": "status", "data": {text}}}')
                    continue
                area = self.area(name)
                if area is None:
                    if sent.get(name) != "error":
                        sent[name] = "error"
                        ws.send(json.dumps({"type": name, "error": "not available (tf5.eq is off, or no such area)"}))
                    continue
                version, build = area
                v = version()
                if sent.get(name) != v:
                    sent[name] = v
                    ws.send(json.dumps({**build(), "type": name, "version": v}, default=str))

    def register(self, sock: Sock) -> None:
        def ws_tf5(ws: Any) -> None:
            with contextlib.suppress(ConnectionClosed):
                self.stream(ws)

        sock.route("/ws/tf5", endpoint="ws_tf5")(ws_tf5)
