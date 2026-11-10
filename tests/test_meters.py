"""Level meters over RCP (tf_daemon/console/meters.py): the tolerant reply parsing, the client
against the simulated desk (console/sim_rcp.py), its read-only command set, and the probe CLI.
The route and the page are checked in tests/test_live.py and tests/test_browser_strip.py."""

from __future__ import annotations

import socket
import threading

import pytest
from helpers import make_config, wait_until

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError
from tf_daemon.console.meters import (
    ALLOWED,
    Meter,
    MeterClient,
    decode_values,
    meter_address,
    meter_kind,
    parse_mtrinfo,
    probe,
)
from tf_daemon.console.patch import default_usb_patch, parse_patch_entry
from tf_daemon.console.sim_rcp import METER_FLOOR, RCP_PREFIX, SimRCP

PATCH = [parse_patch_entry(e) for e in default_usb_patch()]  # USB 1-32 = InCh 1-32, 33-34 = St L / R
INCH = meter_address("InCh", "PreFader")


# ---------------------------------------------------------------------- parsing


def test_meter_addresses():
    assert meter_kind("MIXER:Current/InCh/PreFader") == ("InCh", "PreFader")
    assert meter_kind("MIXER:Current/Meter/StInCh/PostOn") == ("StInCh", "PostOn")  # an extra segment is fine
    assert meter_kind("MIXER:Current/InCh/Fader/Level") is None


def test_mtrinfo_like_prminfo():
    info = parse_mtrinfo(["3", "MIXER:Current/InCh/PreFader", "40", "0", "-13800", "0", "dB", "100"])
    assert info is not None
    assert (info.index, info.address, info.channels, info.lo, info.hi) == (3, INCH, 40, -13800, 0)
    short = parse_mtrinfo(["0", "MIXER:Current/St/PostOn", "2"])  # fewer fields: what is there
    assert short is not None and short.channels == 2 and short.lo is None
    assert parse_mtrinfo(["x", "addr"]) is None


@pytest.mark.parametrize(
    ("tokens", "values"),
    [
        (["-2000", "-13800", "0"], [-2000, -13800, 0]),  # dB x 100
        (["mtr", "12", "127", "0"], [12, 127, 0]),  # a type word first
        (["0a", "7f", "00"], [10, 127, 0]),  # hex bytes
        (["05", "10", "20"], [5, 16, 32]),  # zero-padded: hex
        (["0a7f000102030405"], [10, 127, 0, 1, 2, 3, 4, 5]),  # one packed hex string
        ([], []),
        (["1", "zz"], None),
    ],
)
def test_meter_values_decode_tolerantly(tokens, values):
    assert decode_values(tokens) == values


def test_meter_scale_from_listing_or_values():
    m = Meter(INCH, parse_mtrinfo(["0", INCH, "4", "0", "-13800", "0"]))
    m.take([0, -1250, -13800, -32768], [])
    assert m.is_db and [m.level(x) for x in range(5)] == [0.0, -12.5, None, None, None]
    raw = Meter(INCH)
    raw.take([0, 40, 127], [])
    assert not raw.is_db and raw.level(1) == 40 and raw.raw_max() == 127
    extra = Meter(INCH, parse_mtrinfo(["0", INCH, "2"]))
    extra.take([5, 0, 30, 40], [])  # more values than channels: the leading ones are indices
    assert extra.values == [30, 40]


# ---------------------------------------------------------------------- the client and the simulated desk


@pytest.fixture
def desk():
    sim = SimRCP()
    yield sim
    sim.close()


def test_client_discovers_starts_and_reads_levels(desk):
    client = MeterClient("127.0.0.1", desk.port, PATCH, interval_ms=50, point="PreFader")
    client.start()
    try:
        wait_until(lambda: client.levels()["scale"] == "dB" and client.levels()["levels"][33] is not None)
        st = client.status()
        assert st["mtrnum"] == len(st["listed"]) > 20
        assert st["meters"][INCH]["state"] == "started" and st["meters"][INCH]["scale"] == "dB"
        levels = client.levels()["levels"]
        assert len(levels) == len(PATCH)
        assert -40 < levels[1] <= 0  # "Lead Vox" talks
        assert levels[20] is None  # an unused channel: no signal
        sent = list(desk.received)
        assert all(ALLOWED.fullmatch(line) for line in sent), sent  # meter commands only: changes nothing
        assert f"mtrstart {INCH} 50" in sent and f"mtrstart {meter_address('St', 'PostOn')} 50" in sent
    finally:
        client.stop()
    assert f"mtrstop {INCH}" in desk.received


def test_post_on_meter_follows_the_channel_on(desk):
    client = MeterClient("127.0.0.1", desk.port, PATCH, interval_ms=20, point="PostOn")
    client.start()
    try:
        wait_until(lambda: client.levels()["levels"][1] is not None)
        desk.set(RCP_PREFIX["InCh"] + "Fader/On", 1, 0, "0")  # channel 2 switched off on the desk
        wait_until(lambda: client.levels()["levels"][1] is None)
        assert client.levels()["levels"][0] is not None
    finally:
        client.stop()


def test_since_reports_news_only(desk):
    client = MeterClient("127.0.0.1", desk.port, PATCH, interval_ms=20)
    v, msg = client.since(0)
    assert v == 0 and msg is None
    client.start()
    try:
        wait_until(lambda: client.since(0)[1] is not None)
        v, msg = client.since(0)
        assert msg is not None and msg["type"] == "tf5-meters"
        assert client.since(client.version) == (client.version, None)
    finally:
        client.stop()


@pytest.mark.slow  # the reconnect waits its 1 s backoff
def test_reconnects_after_the_desk_drops_the_connection(desk):
    client = MeterClient("127.0.0.1", desk.port, PATCH, interval_ms=20)
    client.start()
    try:
        wait_until(lambda: client.levels()["scale"] == "dB")
        desk.drop_connections()
        wait_until(lambda: client.connects >= 2 and client.levels()["levels"][1] is not None, timeout=10)
    finally:
        client.stop()
    assert not client.connected


def test_a_desk_without_meters_is_only_reported(desk):
    desk.reply = lambda line, conn=None: f"ERROR {line.split()[0]} UnknownCommand"  # type: ignore[method-assign]
    client = MeterClient("127.0.0.1", desk.port, PATCH)
    client.start()
    try:
        wait_until(lambda: any(m["state"].startswith("refused") for m in client.status()["meters"].values()))
        st = client.status()
        assert st["connected"] and st["mtrnum"] is None and st["listed"] == []
        assert client.levels()["scale"] is None and set(client.levels()["levels"]) == {None}
    finally:
        client.stop()


def test_raw_meter_steps_are_reported_as_raw():
    # A desk that answers in raw 0..127 steps, hex, with a type word (one guess at the real grammar).
    srv = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while chunk := conn.recv(4096):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    cmd, *rest = line.decode().split()
                    if cmd == "mtrnum":
                        conn.sendall(b"ERROR mtrnum UnknownCommand\n")
                    elif cmd == "mtrstart":
                        conn.sendall(f"OK mtrstart {rest[0]} {rest[1]}\n".encode())
                        if rest[0] == INCH:
                            conn.sendall(f"NOTIFY mtr {rest[0]} mtr 40 7f 00\n".encode())

    threading.Thread(target=serve, daemon=True).start()
    client = MeterClient("127.0.0.1", srv.getsockname()[1], PATCH[:3])
    client.start()
    try:
        wait_until(lambda: client.levels()["scale"] == "raw")
        assert client.levels() | {"connected": True} == {
            "type": "tf5-meters",
            "connected": True,
            "point": "PreFader",
            "scale": "raw",
            "max": 127,
            "levels": [64, 127, 0],
        }
    finally:
        client.stop()
        srv.close()


def test_only_meter_commands_can_be_sent():
    client = MeterClient("127.0.0.1", 1, PATCH)
    for line in ("set MIXER:Current/InCh/Fader/Level 0 0 0", "mtrstart a b", "ssrecall_ex scene_a 1"):
        with pytest.raises(ValueError, match="not a meter command"):
            client._send(None, [line])  # type: ignore[arg-type]


def test_simulated_meter_replies(desk):
    assert desk.reply("mtrnum").startswith("OK mtrnum ")
    assert (
        desk.reply("mtrinfo 0") == f'OK mtrinfo 0 "{INCH.replace("PreFader", "PreHPF")}" 40 0 {METER_FLOOR} 0 "dB" 100'
    )
    assert desk.reply("mtrinfo 999") == "ERROR mtrinfo 999 UnknownIndex"
    assert desk.reply(f"mtrstart {INCH} 100") == f"OK mtrstart {INCH} 100"
    assert desk.reply("mtrstart MIXER:Current/InCh/Nope 100") == "ERROR mtrstart InvalidArgument"
    levels = desk.meter_levels(INCH, 0.0)
    assert len(levels) == 40 and levels[30] == METER_FLOOR and -4000 < levels[0] <= 0


# ---------------------------------------------------------------------- probe CLI, config, app


def test_probe_prints_the_listing_and_data(desk):
    out: list[str] = []
    probe("127.0.0.1", desk.port, 0.2, [], 50, out=out.append)
    assert out[0] == "-> mtrnum" and out[1].startswith("<- OK mtrnum ")
    assert any(line.startswith(f"<- NOTIFY mtr {INCH} ") for line in out)
    assert out[-1].startswith("-- ") and "data line(s)" in out[-1]
    assert all(ALLOWED.fullmatch(line) for line in desk.received)


def test_meter_config():
    assert make_config().tf5.meters  # on by default: it only reads
    with pytest.raises(ConfigError, match="meter_point"):
        make_config(tf5={"meter_point": "Post"})
    with pytest.raises(ConfigError, match="meter_interval_ms"):
        make_config(tf5={"meter_interval_ms": 5})


def test_demo_serves_meters():
    cfg = make_config(tf5={"source": "demo", "meter_interval_ms": 20})
    modules = build_modules(cfg)
    app = create_app(cfg, modules)
    try:
        http = app.test_client()
        wait_until(lambda: http.get("/api/tf5/meters").get_json()["levels"][1] is not None)
        body = http.get("/api/tf5/meters").get_json()
        assert body["scale"] == "dB" and body["status"]["meters"][INCH]["state"] == "started"
        assert http.get("/api/tf5/status").get_json()["meters"]["connected"]
    finally:
        app.extensions["tf_stop"]()
    off = make_config(tf5={"source": "demo", "meters": False})
    app = create_app(off, build_modules(off))
    try:
        assert app.test_client().get("/api/tf5/meters").status_code == 404
    finally:
        app.extensions["tf_stop"]()
