"""FrameHub: the latest-value broadcast behind every WebSocket."""

import threading
import time

from helpers import wait_until

from tf_daemon.hub import FrameHub


def test_wait_returns_only_newer_payloads():
    hub = FrameHub()
    assert hub.wait(0, timeout=0.01) == (0, None)
    hub.publish("a")
    hub.publish("b")  # only the latest is kept
    assert hub.wait(0, timeout=0.01) == (2, "b")
    assert hub.wait(2, timeout=0.01) == (2, None)


def test_stream_sends_the_current_payload_then_each_new_one():
    hub = FrameHub()
    hub.publish("snapshot")
    sent: list[str] = []
    t = threading.Thread(target=hub.stream, args=(sent.append,), kwargs={"timeout": 0.05}, daemon=True)
    t.start()
    wait_until(lambda: sent == ["snapshot"])
    hub.publish("next")
    wait_until(lambda: sent == ["snapshot", "next"])
    hub.close()
    t.join(timeout=1)
    assert not t.is_alive()  # a closed hub ends the stream instead of spinning


def test_stream_stops_when_the_client_goes_away():
    hub = FrameHub()
    hub.publish("x")

    def send(payload):
        raise ConnectionError("closed")

    start = time.monotonic()
    try:
        hub.stream(send)
    except ConnectionError:
        pass
    assert time.monotonic() - start < 1
