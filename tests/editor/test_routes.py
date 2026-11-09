"""The EQ in the app: tf5.eq config, TF5Module, GET /api/tf5/eq and the page's EQ toggle."""

import re
import socket
from pathlib import Path

import pytest
from editor_fakes import PASSWORD, PKG, assert_read_only, wait_until
from helpers import make_config

from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config


def test_eq_config(tmp_path):
    t = load_config(None).tf5
    assert t.eq is False and t.eq_port == 50368
    p = tmp_path / "c.toml"
    for bad in ('[tf5]\neq = "yes"\n', '[tf5]\neq = 1\n', '[tf5]\neq_port = 0\n', '[tf5]\neq_port = 70000\n'):
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


def _app(eq, eq_port=None, usb_patch=None):
    tf5 = {"host": "127.0.0.1", "port": _closed_port(), "eq": eq}   # no RCP console: EQ is independent
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
        assert d["eq"]["bands"][1] == {"label": "LM", "type": "bell", "on": True, "freq": 800.0, "gain": -14.1,
                                         "q": 0.25}
        assert len(d["freqs"]) == len(d["curve"]) == 241
        assert d["freqs"][0] == 20.0 and d["freqs"][-1] == 20000.0
        assert min(d["curve"]) < -20 and all(isinstance(v, float) for v in d["curve"])
        off = c.get("/api/tf5/eq/1").get_json()
        assert off["eq"]["on"] is False and set(off["curve"]) == {0.0}
        assert c.get("/api/tf5/eq/2").get_json() == {"index": 2, "source": None, "connected": True, "synced": True,
                                                     "writable": False, "eq": None, "freqs": [], "curve": []}
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
    # Handles: bells / shelves at their own gain (what you grab), the HPF and filters on the curve.
    handles = re.search(r"export function eqHandles\(\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "yEq(b.gain)" in handles and "yEq(eqAt(b.freq) ?? 0)" in handles and "yEq(eqAt(eq.hpf.freq) ?? 0)" in handles
    # The console sidebar has its own EQ graph, redrawn on every pushed change.
    side = re.search(r"function drawPanelEq\(\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "eqAt(f)" in side and "eqHalf()" in side and "peqRange" not in js   # the spectrum's scale
    set_range = re.search(r"function setRange\(min, max\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "drawPanelEq()" in set_range
    on_eq = re.search(r"function onConsoleEq\(msg\) \{(.*?)\n\}\n", js, re.S).group(1)
    assert "drawPanelEq()" in on_eq
    css = (static / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.cp-eq \{[^}]*position: static", css)   # not the full-page canvas rule
