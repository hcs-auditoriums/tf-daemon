"""The typed config: loading, overrides, validation, keys from earlier versions."""

import dataclasses
import logging

import pytest
from helpers import make_config

from tf_daemon.__main__ import source_overrides
from tf_daemon.app import build_modules
from tf_daemon.config import Config, ConfigError, load_config


def test_defaults_are_a_valid_config():
    cfg = load_config(None)
    assert isinstance(cfg, Config)
    assert (cfg.audio.source, cfg.rf.source, cfg.qlxd.source, cfg.tf5.source) == ("device",) * 4
    assert cfg.tf5.active_source == "off"   # no host: nothing to connect to
    assert [c.name for c in cfg.channels] == ["Main L", "Main R"]


def test_config_is_immutable():
    cfg = load_config(None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.tf5.host = "10.0.0.5"  # type: ignore[misc]
    changed = dataclasses.replace(cfg.tf5, host="10.0.0.5")   # the way to vary one: validated again
    assert changed.active_source == "device"
    with pytest.raises(ConfigError, match="tf5.port"):
        dataclasses.replace(cfg.tf5, port=0)


def test_overrides_beat_the_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[tf5]\nhost = "10.0.0.5"\nport = 49281\n')
    cfg = load_config(p, {"tf5": {"port": 49282}})
    assert (cfg.tf5.host, cfg.tf5.port) == ("10.0.0.5", 49282)


def test_unknown_keys_are_reported_not_fatal(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text('[tf5]\nhots = "10.0.0.5"\n[mixer]\nx = 1\n[[channels]]\nindex = 0\nlable = "Vox"\n')
    with caplog.at_level(logging.WARNING):
        cfg = load_config(p)
    assert cfg.tf5.host == ""
    assert "tf5.hots" in caplog.text and "[mixer]" in caplog.text and "channels.lable" in caplog.text


def test_types_are_checked(tmp_path):
    p = tmp_path / "c.toml"
    for bad, key in (('[server]\nport = "8080"\n', "server.port"), ("[dsp]\nhop = 1.5\n", "dsp.hop"),
                     ("[audio]\nbuffer_seconds = true\n", "audio.buffer_seconds"),
                     ('[qlxd]\nhosts = "10.0.0.5"\n', "qlxd.hosts"), ("[[channels]]\nlabel = 'x'\n", "index")):
        p.write_text(bad)
        with pytest.raises(ConfigError, match=key):
            load_config(p)
    p.write_text("[rf]\nrbw = 100\n[dsp]\nhop = 2400.0\n")   # a whole-number float is fine; rbw is in kHz
    cfg = load_config(p)
    assert cfg.rf.rbw == "100" and cfg.dsp.hop == 2400


def test_enabled_false_from_earlier_versions_means_off(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[rf]\nenabled = false\nsource = \"device\"\n[qlxd]\nenabled = true\nsource = \"demo\"\n")
    cfg = load_config(p)
    assert (cfg.rf.source, cfg.qlxd.source) == ("off", "demo")
    # ...but a source chosen on the command line still wins.
    assert load_config(p, source_overrides(rf="demo")).rf.source == "demo"


def test_source_overrides():
    assert source_overrides() == {}
    assert source_overrides("wav", "a.wav", tf5="off") == {"audio": {"source": "wav", "wav": "a.wav"},
                                                         "tf5": {"source": "off"}}
    demo = source_overrides("device", rf="off", demo=True)   # --demo simulates everything
    assert demo == {"audio": {"source": "synth"}, "rf": {"source": "demo"}, "qlxd": {"source": "demo"},
                    "tf5": {"source": "demo"}}


def test_wav_needs_a_file():
    with pytest.raises(ConfigError, match="audio.wav"):
        make_config(audio={"source": "wav"})


def test_every_source_off_but_audio():
    cfg = make_config(rf={"source": "off"}, qlxd={"source": "off"}, tf5={"source": "off", "host": "10.0.0.5"})
    assert [m.name for m in build_modules(cfg)] == ["audio"]
