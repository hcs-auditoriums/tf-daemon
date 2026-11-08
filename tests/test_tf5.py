import socket
import threading
import time

import pytest

from tf_daemon.app import build_modules, create_app
from tf_daemon.audio.engine import AudioEngine
from tf_daemon.audio.sources import SynthSource
from tf_daemon.config import ConfigError, load_config
from tf_daemon.tf5 import TF5Client, default_usb_patch, parse_line, parse_patch_entry

INCH = "MIXER:Current/InCh/"
ST = "MIXER:Current/St/"


class FakeTF5:
    """Answers RCP get/prminfo like the console; can push NOTIFY lines."""

    PRMINFO = [  # the shape of the TF's published list, plus one extra the client should pick up
        f'"{INCH}Fader/Level" 40 0 -32768 1000 -32768 "dB" integer any rw 100',
        f'"{INCH}Label/Name" 40 0 0 64 "ch 1" "" string any rw 1',
        f'"{INCH}Port/HA/Gain" 40 1 -600 6600 -600 "dB" integer any rw 100',
        f'"{INCH}ToMix/Level" 40 20 -32768 1000 -32768 "dB" integer any rw 100',
        f'"{INCH}PEQ/Band/Gain" 40 4 -1800 1800 0 "dB" integer any rw 100',  # has a y dimension: not an extra
    ]

    def __init__(self, values):
        self.values = values  # (addr, x) or (addr, x, y) -> raw reply token
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.requests: list[str] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        try:
            while chunk := conn.recv(65536):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                out = []
                for raw in lines:
                    line = raw.decode()
                    self.requests.append(line)
                    parts = line.split()
                    if parts[0] == "prminfo":
                        i = int(parts[1])
                        out.append(f"OK prminfo {i} {self.PRMINFO[i]}" if i < len(self.PRMINFO)
                                   else f"ERROR prminfo {i} UnknownIndex")
                    elif parts[0] == "get":
                        addr, x, y = parts[1], int(parts[2]), int(parts[3])
                        value = self.values.get((addr, x, y), self.values.get((addr, x)) if y == 0 else None)
                        if value is not None:
                            out.append(f"OK get {addr} {x} {y} {value}")
                        else:
                            out.append(f"ERROR get {addr} InvalidArgument")
                conn.sendall("".join(o + "\n" for o in out).encode())
        except OSError:
            pass

    def push(self, line):
        for c in self.conns:
            c.sendall((line + "\n").encode())

    def drop_connections(self):
        for c in self.conns:
            c.close()
        self.conns.clear()

    def close(self):
        self.drop_connections()
        self.srv.close()


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached in time")


def test_patch_entries():
    assert parse_patch_entry("InCh 5") == ("InCh", 4)
    assert parse_patch_entry("ch40") == ("InCh", 39)
    assert parse_patch_entry("st r") == ("St", 1)
    assert parse_patch_entry("") is None
    for bad in ("InCh 0", "InCh 41", "Mix 1"):
        with pytest.raises(ValueError):
            parse_patch_entry(bad)
    assert len(default_usb_patch()) == 34


def test_parse_line_handles_quoted_names():
    assert parse_line('OK get MIXER:Current/InCh/Label/Name 4 0 "Lead Vox"') == \
        ("OK", "get", ["MIXER:Current/InCh/Label/Name", "4", "0", "Lead Vox"])
    assert parse_line('OK get X 0 0 "unbalanced') is None


def test_config_validation(tmp_path):
    cfg = load_config(None)
    assert cfg["tf5"]["host"] == "" and cfg["tf5"]["port"] == 49280
    assert [m.name for m in build_modules(cfg, "synth", rf_source="off", qlxd_source="off")] == ["audio"]
    p = tmp_path / "c.toml"
    for bad in ('[tf5]\nport = 0\n', '[tf5]\nusb_patch = ["Aux 3"]\n', '[tf5]\npassword = 1234\n'):
        p.write_text(bad)
        with pytest.raises(ConfigError, match="tf5"):
            load_config(p)
    p.write_text('[tf5]\nhost = "192.168.1.50"\nusername = "admin"\npassword = "secret"\nusb_patch = ["St L", ""]\n')
    t = load_config(p)["tf5"]
    assert (t["host"], t["username"], t["password"], t["usb_patch"]) == ("192.168.1.50", "admin", "secret", ["St L", ""])


@pytest.fixture
def console():
    tf = FakeTF5({
        (INCH + "Label/Name", 0): '"Kick"',
        (INCH + "Label/Name", 1): '"Lead Vox"',
        (INCH + "Fader/Level", 1): "-1250",
        (INCH + "Fader/On", 1): "1",
        (INCH + "ToStereo/Pan", 1): "-20",
        (INCH + "Label/Color", 1): '"Blue"',
        (INCH + "Port/HA/Gain", 1): "3150",
        (ST + "Label/Name", 0): '"ST L"',
        (ST + "Fader/Level", 0): "-32768",
        (INCH + "ToMix/Level", 1, 0): "-32768",
        (INCH + "ToMix/Level", 1, 7): "-600",
        (INCH + "ToMix/On", 1, 7): "1",
        (INCH + "ToFx/Level", 1, 1): "-1000",
        ("MIXER:Current/Mix/Label/Name", 0): '"STAGE"',
        ("MIXER:Current/Mix/Label/Name", 7): '"DRUM"',
    })
    yield tf
    tf.close()


def test_names_and_settings_flow_into_the_engine(console):
    cfg = load_config(None)
    cfg["channels"] = [{"index": 0, "label": "Main"}]  # an explicit label beats the console's name
    src = SynthSource(channels=4, active=[0, 1, 2, 3], blocksize=1200, speech_db=-20, noise_db=-50,
                      tone_hz=2500, tone_db=-15)
    engine = AudioEngine(cfg, src)
    patch = [("InCh", 0), ("InCh", 1), ("St", 0), None]
    client = TF5Client("127.0.0.1", console.port, patch, poll_s=30, on_names=engine.set_console_names,
                       on_gains=engine.set_console_gains)
    client.start()
    try:
        wait_until(lambda: engine.inputs[2].label == "ST L")
        wait_until(lambda: engine._console_gains.get(1) == -12.5)  # fader + on reach the engine
        assert [c.label for c in engine.inputs] == ["Main", "Lead Vox", "ST L", "Ch 4"]
        version = engine.meta_version

        # The extra parameter from prminfo is read too; sends with a mix dimension are not.
        wait_until(lambda: "Port/HA/Gain" in client.channels()[1]["params"])
        ch = client.channels()[1]
        assert ch["source"] == "InCh 2"
        assert ch["params"]["Port/HA/Gain"] == {"value": 31.5, "unit": "dB"}
        assert ch["params"]["Fader/Level"] == {"value": -12.5, "unit": "dB"}
        assert ch["params"]["ToStereo/Pan"]["value"] == -20
        assert client.channels()[2]["params"]["Fader/Level"]["value"] is None  # -inf
        assert client.channels()[3] == {"index": 3, "source": None, "params": {}}
        assert client.status()["extra_params"] == ["Port/HA/Gain"]
        assert not any("PEQ" in r for r in console.requests if r.startswith("get"))

        # Sends come back as one value per mix / FX; mix names are read too.
        sends = ch["params"]["ToMix/Level"]["value"]
        assert len(sends) == 20 and sends[0] is None and sends[7] == -6.0
        assert ch["params"]["ToMix/On"]["value"][7] == 1
        assert ch["params"]["ToFx/Level"]["value"] == [None, -10.0]
        wait_until(lambda: client.mixes()[7] == "DRUM")
        assert client.mixes()[0] == "STAGE" and client.mixes()[1] is None
        console.push(f"NOTIFY set {INCH}ToMix/Level 1 7 -300")
        wait_until(lambda: client.channels()[1]["params"]["ToMix/Level"]["value"][7] == -3.0)

        # Changes made on the desk arrive as NOTIFY lines.
        console.push(f'NOTIFY set {INCH}Label/Name 1 0 "Pastor"')
        console.push(f"NOTIFY set {INCH}Fader/On 1 0 0")
        wait_until(lambda: engine.inputs[1].label == "Pastor")
        assert engine.meta_version > version
        wait_until(lambda: client.channels()[1]["params"]["Fader/On"]["value"] == 0)
        wait_until(lambda: 1 in engine._console_gains and engine._console_gains[1] is None)  # off: out of the loop

        # A scene recall makes the client read everything again.
        n_gets = sum(r.startswith("get") for r in console.requests)
        console.push("NOTIFY sscurrent_ex scene_a 3")
        wait_until(lambda: sum(r.startswith("get") for r in console.requests) > n_gets)
    finally:
        client.stop()


def test_client_reconnects(console):
    client = TF5Client("127.0.0.1", console.port, [("InCh", 0)], poll_s=30, on_names=lambda names: None)
    client.start()
    try:
        wait_until(lambda: client.names() == {0: "Kick"})
        console.drop_connections()
        wait_until(lambda: client.connects >= 2 and client.connected, timeout=8)
    finally:
        client.stop()
    assert not client.connected


def test_unreachable_console_is_reported(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]  # closed again when the block ends: nothing listens there
    client = TF5Client("127.0.0.1", port, [("InCh", 0)], poll_s=30, on_names=lambda names: None)
    client.start()
    try:
        wait_until(lambda: client.last_error is not None)
        assert not client.connected
    finally:
        client.stop()


def test_tf5_routes(console):
    cfg = load_config(None)
    cfg["tf5"]["host"] = "127.0.0.1"
    cfg["tf5"]["port"] = console.port
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="off")
    assert [m.name for m in modules] == ["audio", "tf5"]
    app = create_app(cfg, modules)
    try:
        client = app.test_client()
        wait_until(lambda: client.get("/api/tf5/status").get_json()["connected"])
        # Input 1 takes the console's name; Main L/R (33/34) keep their config labels.
        wait_until(lambda: client.get("/api/audio/meta").get_json()["channels"][0]["label"] == "Kick")
        assert client.get("/api/audio/meta").get_json()["channels"][32]["label"] == "Main L"
        wait_until(lambda: "Label/Name" in client.get("/api/tf5/channels").get_json()["channels"][0]["params"])
        chans = client.get("/api/tf5/channels").get_json()
        assert chans["connected"] and chans["channels"][0]["params"]["Label/Name"]["value"] == "Kick"
        assert len(chans["mixes"]) == 20
        assert client.get("/api/status").get_json()["modules"]["tf5"]["connected"]
    finally:
        app.extensions["tf_stop"]()
