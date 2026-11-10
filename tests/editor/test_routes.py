"""The editor protocol in the app: tf5.eq config, TF5Module, GET /api/tf5/eq and the page's EQ toggle,
and any parameter of any function: tf5.functions / write switches, /api/tf5/param, scene recall."""

import re
import socket
from pathlib import Path

import pytest
from editor_fakes import MOBILE_PASSWORD, PASSWORD, PKG, assert_read_only, wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import WRITE_SWITCHES, ConfigError, load_config
from tf_daemon.console.editor.tree import FUNCTIONS
from tf_daemon.console.module import EQ_LIMITS


def test_eq_config(tmp_path):
    t = load_config(None).tf5
    assert t.eq is False and t.eq_port == 50368
    p = tmp_path / "c.toml"
    for bad in ('[tf5]\neq = "yes"\n', "[tf5]\neq = 1\n", "[tf5]\neq_port = 0\n", "[tf5]\neq_port = 70000\n"):
        p.write_text(bad)
        with pytest.raises(ConfigError, match="tf5.eq"):
            load_config(p)
    p.write_text('[tf5]\nhost = "10.0.0.5"\neq = true\neq_port = 50369\n')
    t = load_config(p).tf5
    assert t.eq is True and t.eq_port == 50369


def test_shipped_config_keeps_eq_off():
    cfg = load_config(Path(__file__).parent.parent / "config.toml")
    assert cfg.tf5.eq is False


def _closed_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def _app(eq, eq_port=None, usb_patch=None, **extra):
    tf5 = {"host": "127.0.0.1", "port": _closed_port(), "eq": eq, **extra}  # no RCP console: EQ is independent
    if eq_port:
        tf5["eq_port"] = eq_port
    if usb_patch is not None:
        tf5["usb_patch"] = usb_patch
    cfg = make_config(tf5=tf5)
    modules = build_modules(cfg)
    return create_app(cfg, modules), modules


def test_eq_route_is_404_when_off():
    app, modules = _app(eq=False)
    try:
        c = app.test_client()
        assert c.get("/api/tf5/eq/0").status_code == 404
        assert c.get("/api/tf5/status").get_json()["eq"] is None
        assert modules[1].eq_client is None
    finally:
        app.extensions["tf_stop"]()


def test_eq_route(console):
    app, _ = _app(eq=True, eq_port=console.port, usb_patch=["InCh 1", "InCh 2", "", "St R", "InCh 40"])
    try:
        c = app.test_client()
        wait_until(lambda: c.get("/api/tf5/eq/0").get_json()["synced"])
        d = c.get("/api/tf5/eq/0").get_json()
        assert d["index"] == 0 and d["source"] == "InCh 1" and d["connected"]
        assert d["eq"]["bands"][1] == {
            "label": "LM",
            "type": "bell",
            "on": True,
            "freq": 800.0,
            "gain": -14.1,
            "q": 0.25,
        }
        assert len(d["freqs"]) == len(d["curve"]) == 241
        assert d["freqs"][0] == 20.0 and d["freqs"][-1] == 20000.0
        assert min(d["curve"]) < -20 and all(isinstance(v, float) for v in d["curve"])
        off = c.get("/api/tf5/eq/1").get_json()
        assert off["eq"]["on"] is False and set(off["curve"]) == {0.0}
        assert c.get("/api/tf5/eq/2").get_json() == {
            "index": 2,
            "source": None,
            "connected": True,
            "synced": True,
            "writable": False,
            "fader_writable": False,
            "limits": EQ_LIMITS,  # values pinned in tests/test_eq_edit.py
            "eq": None,
            "freqs": [],
            "curve": [],
        }
        st = c.get("/api/tf5/eq/3").get_json()
        assert st["source"] == "St R" and st["eq"]["bands"][0]["type"] == "hpf"
        assert c.get("/api/tf5/eq/4").get_json()["eq"]["bands"][1]["freq"] == 1000.0
        assert c.get("/api/tf5/eq/99").get_json()["source"] is None
        status = c.get("/api/tf5/status").get_json()
        assert status["eq"]["synced"] and status["eq"]["read_only"]
        assert c.get("/api/status").get_json()["modules"]["tf5"]["eq"]["connected"]
        console.push_change("InputChannel:0/PEQ/Bank/Band:0/Gain", 0)
        wait_until(lambda: c.get("/api/tf5/eq/0").get_json()["eq"]["bands"][1]["gain"] == 0.0)
        assert PASSWORD not in c.get("/api/tf5/status").get_data(as_text=True)
    finally:
        app.extensions["tf_stop"]()
    assert_read_only(console)


def test_page_has_the_eq_toggle():
    static = PKG / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    # The audio page: app.js and its modules.
    js = "\n".join(f.read_text(encoding="utf-8") for f in [static / "app.js", *sorted((static / "audio").glob("*.js"))])
    assert 'id="eq-toggle" hidden' in html and 'id="show-eq"' in html
    # The EQ is pushed over /ws/audio ("tf5-eq" messages) and drawn.
    assert '"tf5-eq"' in js and "drawEq()" in js
    # The page writes the EQ in one place only (eqedit.js: a person's drag / entry), with PUT
    # and "confirm"; everything else only reads it.
    writes = re.findall(r"fetch\(`/api/tf5/eq[^`]*`\s*,", js)
    assert len(writes) == 1
    edit_js = (static / "audio" / "eqedit.js").read_text(encoding="utf-8")
    assert re.search(r'fetch\(`/api/tf5/eq/\$\{usb\}`, \{\s*method: "PUT".*?confirm: true', edit_js, re.S)
    assert "--eq:" in (static / "style.css").read_text(encoding="utf-8")
    draw = re.search(r"function drawEq\(\) \{(.*?)\n\}\n", js, re.S).group(1)
    # Same dB scale as the spectrum (the View range; shared with the vocal assist
    # through eqHalf / eqY), and the handles sit on the curve.
    assert re.search(r"const eqHalf = \(\) => \(state\.dbMax - state\.dbMin\) / 2", js)
    assert "eqHalf()" in draw and "eqY" in draw
    assert "eqHandles()" in draw
    # Handles: bells / shelves at their own gain (what you grab), the HPF and filters (no gain)
    # on the 0 dB line, so a sideways drag doesn't move them up and down.
    handles = re.search(r"export function eqHandles\(\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "yEq(b.gain)" in handles and "filter ? eqY(0)" in handles and "y: eqY(0)" in handles
    # The console sidebar has its own EQ graph, redrawn on every pushed change.
    side = re.search(r"function drawPanelEq\(\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "eqAt(f)" in side and "eqHalf()" in side and "peqRange" not in js  # the spectrum's scale
    set_range = re.search(r"function setRange\(min, max\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "drawPanelEq()" in set_range
    on_eq = re.search(r"function onConsoleEq\(msg\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "drawPanelEq()" in on_eq
    css = (static / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.cp-eq \{[^}]*position: static", css)  # not the full-page canvas rule


# ====================================================================== any parameter, any function


def test_functions_and_write_switches_config(tmp_path):
    t = load_config(None).tf5
    assert t.functions == FUNCTIONS and not any(getattr(t, k) for k in WRITE_SWITCHES)
    p = tmp_path / "c.toml"
    for bad, key in (
        ('[tf5]\nfunctions = ["Property"]\n', "tf5.functions"),  # Mixing is needed
        ('[tf5]\nfunctions = ["Mixing", "Meter"]\n', "tf5.functions"),
        ("[tf5]\nmix_write = true\n", "tf5.mix_write"),  # needs eq
        ('[tf5]\neq = true\nphantom_write = "yes"\n', "tf5.phantom_write"),
        ("[tf5]\nscene_recall = true\n", "tf5.scene_recall"),
    ):
        p.write_text(bad)
        with pytest.raises(ConfigError, match=key):
            load_config(p)
    p.write_text('[tf5]\neq = true\nheadamp_write = true\nfunctions = ["Mixing", "Processing"]\n')
    t = load_config(p).tf5
    assert t.headamp_write and t.functions == ("Mixing", "Processing")


def _param_app(console, **tf5):
    return _app(eq=True, eq_port=console.port, **tf5)


def test_param_read(console):
    app, _ = _param_app(console, mix_write=True)
    try:
        c = app.test_client()
        url = "/api/tf5/param?func=Mixing&path=DCA:0/Fader/Level"
        wait_until(lambda: c.get("/api/tf5/status").get_json()["eq"]["functions"]["ConsoleSetup"]["synced"])
        d = c.get(url).get_json()
        assert d["func"] == "Mixing" and d["path"] == "DCA:0/Fader/Level" and d["value"] == 0
        assert d["spec"] == {
            "name": "Level",
            "type": "int",
            "width": 2,
            "size": 1,
            "min": -32768,
            "max": 1000,
            "default": 0,
            "scaling": 100,
            "unit": "dB",
        }
        assert d["family"] == "mix" and d["writable"] is True
        ha = c.get("/api/tf5/param?func=Processing&path=BuiltIn/InputPort:0/HeadAmp/Gain").get_json()
        assert ha["value"] == 4500 and ha["family"] is None and not ha["writable"]  # headamp_write is off
        name = c.get("/api/tf5/param?path=InputChannel:1/Label/Name").get_json()  # func defaults to Mixing
        assert name["value"] == "Pastor" and name["spec"]["max"] == 63
        secret = c.get("/api/tf5/param?func=Setup&path=CommSession/MobileMix/Password")
        assert secret.status_code == 200 and secret.get_json()["value"] is None
        assert MOBILE_PASSWORD not in secret.get_data(as_text=True)
        for bad in (
            "func=Mixing&path=DCA:8/Fader/Level",
            "func=Nope&path=X",
            "func=Mixing&path=",
            "path=Mono:1/Fader/On",
        ):
            assert c.get(f"/api/tf5/param?{bad}").status_code == 404
    finally:
        app.extensions["tf_stop"]()
    assert_read_only(console)


def test_param_routes_are_404_without_the_editor_protocol():
    app, _ = _app(eq=False)
    try:
        c = app.test_client()
        assert c.get("/api/tf5/param?path=DCA:0/Fader/Level").status_code == 404
        body = {"changes": [["DCA:0/Fader/Level", 0]], "confirm": True}
        assert c.post("/api/tf5/param", json=body).status_code == 404
        assert c.post("/api/tf5/event/recall_scene", json={"number": "A01", "confirm": True}).status_code == 404
    finally:
        app.extensions["tf_stop"]()


def test_param_write(console):
    app, _ = _param_app(console, mix_write=True, label_write=True)
    try:
        c = app.test_client()
        wait_until(lambda: c.get("/api/tf5/status").get_json()["eq"]["functions"]["ConsoleSetup"]["synced"])
        ok = {"func": "Mixing", "changes": [["DCA:0/Fader/Level", -1000], ["DCA:0/Label/Name", "Choir"]]}
        for body, code in (
            (ok, 400),  # no confirm
            ({**ok, "confirm": "yes"}, 400),
            ({"func": "Mixing", "changes": [], "confirm": True}, 400),
            ({"func": "Mixing", "changes": [["DCA:0/Fader/Level", True]], "confirm": True}, 400),
            ({"func": 3, "changes": ok["changes"], "confirm": True}, 400),
            ({"func": "Mixing", "changes": [["DCA:0/Fader/Level", 2000]], "confirm": True}, 400),  # out of range
            ({"func": "Mixing", "changes": [["DCA:0/Label/Color", "Teal"]], "confirm": True}, 400),
            ({"func": "Processing", "changes": [["BuiltIn/InputPort:0/HeadAmp/Gain", 4000]], "confirm": True}, 403),
            ({"func": "Mixing", "changes": [["InputChannel:0/Fader/Level", 0]], "confirm": True}, 403),  # fader_write
            ({"func": "Mixing", "changes": [["InputChannel:0/Patch/Select", 1]], "confirm": True}, 403),
        ):
            r = c.post("/api/tf5/param", json=body)
            assert r.status_code == code and r.get_json()["ok"] is False, (body, r.get_json())
        assert console.all_writes == []
        r = c.post("/api/tf5/param", json={**ok, "confirm": True})
        assert r.status_code == 200, r.get_json()
        assert r.get_json() == {
            "ok": True,
            "results": [
                {"path": "DCA:0/Fader/Level", "value": -1000, "confirmed": True, "actual": -1000},
                {"path": "DCA:0/Label/Name", "value": "Choir", "confirmed": True, "actual": "Choir"},
            ],
        }
        assert console.all_writes == [("Mixing", "DCA:0/Fader/Level", -1000), ("Mixing", "DCA:0/Label/Name", "Choir")]
        cue = {"func": "Volatile", "changes": [["Cue/StInChannel:1/On", 1]], "confirm": True}
        assert c.post("/api/tf5/param", json=cue).get_json()["results"][0]["confirmed"]
        assert c.get("/api/tf5/param?func=Volatile&path=Cue/StInChannel:1/On").get_json()["value"] == 1
        st = c.get("/api/tf5/status").get_json()["eq"]
        assert st["write_families"] == ["label", "mix"] and not st["read_only"]
    finally:
        app.extensions["tf_stop"]()


def test_recall_scene_route(console):
    app, _ = _param_app(console, scene_recall=True)
    off, _ = _param_app(console)
    try:
        c, c_off = app.test_client(), off.test_client()
        for client in (c, c_off):
            wait_until(lambda cl=client: cl.get("/api/tf5/status").get_json()["eq"]["functions"]["Setup"]["synced"])
        url = "/api/tf5/event/recall_scene"
        assert c.post(url, json={"number": "A05"}).status_code == 400
        assert c.post(url, json={"number": 5, "confirm": True}).status_code == 400
        assert c.post(url, json={"number": "Z05", "confirm": True}).status_code == 400
        assert c_off.post(url, json={"number": "A05", "confirm": True}).status_code == 403
        assert console.recalls == []
        r = c.post(url, json={"number": "B12", "confirm": True})
        assert r.status_code == 200 and r.get_json() == {"ok": True, "number": "B12", "confirmed": True, "status": "OK"}
        wait_until(lambda: c.get("/api/tf5/param?func=Setup&path=Scene/LastScene").get_json()["value"] == "B12")
        assert console.recalls == ["B12"]
    finally:
        app.extensions["tf_stop"]()
        off.extensions["tf_stop"]()
