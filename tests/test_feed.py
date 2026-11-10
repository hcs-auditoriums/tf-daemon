"""WS /ws/tf5 (console/feed.py): an area is sent once when the page asks for it, again only when
its version changes, and the status at most once a second when it differs; nothing is polled."""

import json

import pytest
from flask_sock import ConnectionClosed

from tf_daemon.console import feed as feed_mod
from tf_daemon.console.feed import Feed


class Socket:
    """Feeds ``script`` to Feed.stream one tick at a time (a message or None, or a callable run
    between ticks), records what it sends, and ends the stream when the script runs out."""

    def __init__(self, script):
        self.script, self.sent = list(script), []

    def receive(self, timeout=None):
        while self.script:
            step = self.script.pop(0)
            if callable(step):
                step()
                continue
            return None if step is None else json.dumps(step)
        raise ConnectionClosed(None, None)

    def send(self, text):
        self.sent.append(json.loads(text))


class Buses:
    def __init__(self):
        self.v, self.builds = 0, 0

    def version(self):
        return self.v

    def snapshot(self):
        self.builds += 1
        return {"available": True, "masters": [], "version": self.v}


def run(feed, script):
    ws = Socket(script)
    with pytest.raises(ConnectionClosed):
        feed.stream(ws)
    return ws.sent


def test_an_area_is_pushed_once_and_again_only_when_it_changes():
    buses = Buses()
    feed = Feed(lambda: {"ok": True}, lambda: None, buses)

    def change():
        buses.v += 1

    sent = run(feed, [{"areas": ["mix"]}, None, None, change, None, None])
    assert [(m["type"], m["version"]) for m in sent] == [("mix", 0), ("mix", 1)]
    assert buses.builds == 2  # unchanged ticks build nothing


def test_asking_again_resends_and_dropped_areas_stop():
    buses = Buses()
    feed = Feed(lambda: {}, lambda: None, buses)
    sent = run(feed, [{"areas": ["mix"]}, {"areas": []}, None, {"areas": ["mix"]}, None])
    assert [m["type"] for m in sent] == ["mix", "mix"]  # once, then again after re-asking


def test_desk_areas_say_once_when_the_editor_protocol_is_off():
    feed = Feed(lambda: {}, lambda: None, Buses())
    sent = run(feed, [{"areas": ["scenes", "fx", "nonsense"]}, None, None])
    assert [(m["type"], "error" in m) for m in sent] == [("scenes", True), ("fx", True), ("nonsense", True)]


def test_status_is_sent_when_it_differs_at_most_once_a_second(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(feed_mod.time, "monotonic", lambda: clock[0])
    state = {"eq": None}
    feed = Feed(lambda: dict(state), lambda: None, Buses())

    def later(dt, **changes):
        def step():
            clock[0] += dt
            state.update(changes)

        return step

    sent = run(
        feed,
        [
            {"areas": ["status"]},
            later(0.3, eq={"synced": True}),  # changed, but within the second: not yet
            None,
            later(0.8),  # now past the second: the change goes out
            None,
            later(1.5),  # past again, but nothing changed: nothing sent
            None,
        ],
    )
    assert [m["data"] for m in sent] == [{"eq": None}, {"eq": {"synced": True}}]


def test_areas_that_are_not_a_list_are_ignored():
    feed = Feed(lambda: {}, lambda: None, Buses())
    sent = run(feed, [{"areas": "mix"}, None, {"areas": ["mix"]}, None])
    assert [m["type"] for m in sent] == ["mix"]  # only after a proper list


def test_a_broken_frame_does_not_end_the_stream():
    buses = Buses()
    feed = Feed(lambda: {}, lambda: None, buses)

    class Raw(Socket):
        def receive(self, timeout=None):
            if self.script and self.script[0] == "raw":
                self.script.pop(0)
                return "{not json"
            return super().receive(timeout)

    ws = Raw(["raw", {"areas": ["mix"]}, None])
    with pytest.raises(ConnectionClosed):
        feed.stream(ws)
    assert [m["type"] for m in ws.sent] == ["mix"]
