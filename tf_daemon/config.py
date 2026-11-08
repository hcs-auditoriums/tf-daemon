"""Configuration loading.

A single ``config.toml`` is read at startup and deep-merged over DEFAULTS, so
any missing key gets a sensible value.
"""

from __future__ import annotations

import copy
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rf.engine import SweepSettings

log = logging.getLogger(__name__)

REQUIRED_SAMPLERATE = 48000

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "audio": {
        "device": "TF",
        "samplerate": REQUIRED_SAMPLERATE,
        "blocksize": 1200,
        "buffer_seconds": 2.0,
    },
    "dsp": {
        "fft_size": 32768,
        "hop": 2400,
        "average_tau": 3.0,
        "display_fmin": 50.0,
        "display_fmax": 16000.0,
        "display_bands_per_octave": 48,
        "db_floor": -140.0,
    },
    "synth": {
        "speech_db": -20.0,
        "noise_db": -50.0,
        "tone_hz": 2500.0,
        "tone_db": -15.0,
        "seed": 1,
    },
    "rf": {
        "enabled": True,
        "source": "device",  # device (tinySA) | demo
        "auto_connect": True,  # keep the tinySA connected and sweeping (range: the QLX-D receivers)
        "start_hz": 88_000_000,
        "stop_hz": 108_000_000,
        "points": 290,
        "rbw": "auto",
        "interval_s": 0.5,
    },
    "qlxd": {
        "enabled": True,
        "source": "device",  # device (real receivers on the network) | demo
        "interface": "",     # NIC for discovery; "" = first with a 169.254.x.x address
        "hosts": [],         # receivers to always monitor, "ip" or "ip:port"
        "state_file": "qlxd_state.json",  # UI-edited interface/hosts, overrides the two above
        "meter_poll_s": 1.0,
        "full_poll_s": 10.0,
    },
    "tf5": {
        # Read channel names (and settings) from the mixer over the network.
        "host": "",          # console IP; "" = don't connect
        "port": 49280,       # Yamaha remote control protocol
        "username": "",      # the TF5's protocol has no login; kept for completeness, not sent
        "password": "",
        "poll_s": 30.0,      # full re-read period (changes on the desk are pushed in between)
        "usb_patch": [],     # per USB capture channel: "InCh N", "St L", "St R" or ""; [] = TF default
    },
    "update": {
        # Where the /config page's Update button pulls from.
        "remote": "https://github.com/hcs-auditoriums/tf-daemon.git",
        "branch": "main",
    },
    "channels": [
        {"index": 0, "label": "Main L"},
        {"index": 1, "label": "Main R"},
    ],
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Channel:
    index: int
    label: str


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Load ``path`` (if it exists) over DEFAULTS and validate the result."""
    user: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as f:
                user = tomllib.load(f)
            log.info("Loaded config from %s", p)
        else:
            log.warning("Config file %s not found; using defaults", p)
    cfg = _merge(DEFAULTS, user)
    _validate(cfg)
    return cfg


def channels_from(cfg: dict[str, Any]) -> list[Channel]:
    return [Channel(int(c["index"]), str(c.get("label", f"Ch {int(c['index']) + 1}"))) for c in cfg["channels"]]


def _validate(cfg: dict[str, Any]) -> None:
    sr = int(cfg["audio"]["samplerate"])
    if sr != REQUIRED_SAMPLERATE:
        raise ConfigError(f"audio.samplerate must be {REQUIRED_SAMPLERATE} (TF5 is fixed at 48 kHz), got {sr}")

    dsp = cfg["dsp"]
    n = int(dsp["fft_size"])
    if n < 1024 or n & (n - 1):
        raise ConfigError(f"dsp.fft_size must be a power of two >= 1024, got {n}")
    hop = int(dsp["hop"])
    if not 1 <= hop <= n:
        raise ConfigError(f"dsp.hop must be between 1 and fft_size, got {hop}")
    if float(dsp["average_tau"]) <= 0:
        raise ConfigError("dsp.average_tau must be > 0")
    fmin, fmax = float(dsp["display_fmin"]), float(dsp["display_fmax"])
    if not 0 < fmin < fmax <= sr / 2:
        raise ConfigError(f"need 0 < display_fmin < display_fmax <= {sr / 2}")

    buf_frames = float(cfg["audio"]["buffer_seconds"]) * sr
    if buf_frames < n + hop:
        raise ConfigError("audio.buffer_seconds is too short to hold one FFT frame plus a hop")

    chans = cfg["channels"]
    if not isinstance(chans, list) or not chans:
        raise ConfigError("at least one [[channels]] entry is required")
    seen = set()
    for c in chans:
        if "index" not in c:
            raise ConfigError(f"[[channels]] entry missing 'index': {c}")
        idx = int(c["index"])
        if idx < 0:
            raise ConfigError(f"channel index must be >= 0, got {idx}")
        if idx in seen:
            raise ConfigError(f"channel index {idx} listed twice")
        seen.add(idx)

    rf = cfg["rf"]
    if str(rf["source"]).lower() not in ("device", "demo"):
        raise ConfigError(f"rf.source must be 'device' or 'demo', got {rf['source']!r}")
    try:
        SweepSettings.from_dict(rf)
    except ValueError as e:
        raise ConfigError(f"[rf] {e}") from None

    t = cfg["tf5"]
    if not isinstance(t["host"], str):
        raise ConfigError("tf5.host must be a string (IP address or hostname)")
    if not 1 <= int(t["port"]) <= 65535:
        raise ConfigError(f"tf5.port must be 1..65535, got {t['port']}")
    if not isinstance(t["username"], str) or not isinstance(t["password"], str):
        raise ConfigError("tf5.username and tf5.password must be strings")
    if not 1 <= float(t["poll_s"]) <= 3600:
        raise ConfigError("tf5.poll_s must be between 1 and 3600")
    if not isinstance(t["usb_patch"], list):
        raise ConfigError("tf5.usb_patch must be a list of strings")
    from .tf5 import parse_patch_entry  # local: tf5 imports flask, config shouldn't at import time
    for entry in t["usb_patch"]:
        try:
            parse_patch_entry(entry)
        except ValueError as e:
            raise ConfigError(f"[tf5] {e}") from None

    q = cfg["qlxd"]
    if str(q["source"]).lower() not in ("device", "demo"):
        raise ConfigError(f"qlxd.source must be 'device' or 'demo', got {q['source']!r}")
    if not isinstance(q["hosts"], list):
        raise ConfigError("qlxd.hosts must be a list of \"ip\" or \"ip:port\" strings")
    for key in ("meter_poll_s", "full_poll_s"):
        if not 0.2 <= float(q[key]) <= 300:
            raise ConfigError(f"qlxd.{key} must be between 0.2 and 300")
