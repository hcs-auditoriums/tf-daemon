"""Configuration: ``config.toml`` read into a typed, validated, immutable ``Config``.

Every key is optional; anything missing takes the default declared on its
section below. Unknown keys are logged and ignored. Each device has a
``source``: ``device`` (the real hardware), ``demo`` / ``synth`` / ``wav``
(simulated) or ``off``.

Code that needs different settings (the command line, tests) passes
``overrides`` in the same nested shape as the TOML file, so everything goes
through the same validation:

    load_config("config.toml", {"tf5": {"source": "demo"}})
"""

from __future__ import annotations

import copy
import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

from .console.patch import parse_patch_entry
from .rf.settings import SweepSettings

log = logging.getLogger(__name__)

REQUIRED_SAMPLERATE = 48000

DEVICE_SOURCES = ("device", "demo", "off")
AUDIO_SOURCES = ("device", "synth", "stage", "wav")


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------------- sections


@dataclass(frozen=True)
class Section:
    """A ``[table]`` of config.toml. Subclasses declare fields with defaults and
    check their values in ``validate``."""

    NAME: ClassVar[str] = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Section:
        if not isinstance(d, dict):
            raise ConfigError(f"[{cls.NAME}] must be a table")
        d = cls._prepare(dict(d))
        known = {f.name: f for f in fields(cls)}
        for key in sorted(set(d) - set(known)):
            log.warning("Ignoring unknown config key %s.%s", cls.NAME, key)
        return cls(**{k: _coerce(cls.NAME, k, known[k].default, v) for k, v in d.items() if k in known})

    @classmethod
    def _prepare(cls, d: dict[str, Any]) -> dict[str, Any]:
        """Hook for keys renamed or reshaped since earlier versions."""
        return d

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        pass


def _coerce(section: str, key: str, default: Any, value: Any) -> Any:
    """``value`` as the type of the field's default, or ConfigError naming the key."""
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false, got {value!r}")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
            raise ConfigError(f"{where} must be a whole number, got {value!r}")
        return int(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} must be a number, got {value!r}")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string, got {value!r}")
        return value
    if isinstance(default, tuple):
        if not isinstance(value, list | tuple):
            raise ConfigError(f"{where} must be a list, got {value!r}")
        return tuple(value)
    raise AssertionError(f"no coercion for {where}")


def _upgrade(data: dict[str, Any]) -> dict[str, Any]:
    """Keys from earlier versions, in their current form. Applied to the file only,
    before overrides, so a source given on the command line still wins."""
    data = copy.deepcopy(data)
    for section in ("rf", "qlxd"):  # ``enabled = false`` turned these off; now ``source = "off"``
        d = data.get(section)
        if isinstance(d, dict) and "enabled" in d:
            enabled = d.pop("enabled")
            if not isinstance(enabled, bool):
                raise ConfigError(f"{section}.enabled must be true or false, got {enabled!r}")
            if not enabled:
                d["source"] = "off"
    return data


def _check_source(section: str, source: str, choices: tuple[str, ...]) -> None:
    if source not in choices:
        raise ConfigError(f"{section}.source must be {', '.join(choices[:-1])} or {choices[-1]}, got {source!r}")


@dataclass(frozen=True)
class ServerConfig(Section):
    NAME: ClassVar[str] = "server"
    host: str = "0.0.0.0"
    port: int = 8080

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"server.port must be 1..65535, got {self.port}")


@dataclass(frozen=True)
class AudioConfig(Section):
    NAME: ClassVar[str] = "audio"
    source: str = "device"  # device (capture) | synth (test signal) | stage (simulated stage) | wav
    wav: str = ""  # file for source = "wav"
    device: str = "TF"  # case-insensitive substring of the capture device name
    channels: int = 34  # capture channels (TF5: 32 + St L/R); a device with fewer is zero-padded
    samplerate: int = REQUIRED_SAMPLERATE
    blocksize: int = 1200
    buffer_seconds: float = 2.0

    def validate(self) -> None:
        _check_source("audio", self.source, AUDIO_SOURCES)
        if self.source == "wav" and not self.wav:
            raise ConfigError('audio.source = "wav" needs a file (audio.wav, or --source wav PATH)')
        if self.samplerate != REQUIRED_SAMPLERATE:
            raise ConfigError(
                f"audio.samplerate must be {REQUIRED_SAMPLERATE} (TF5 is fixed at 48 kHz), got {self.samplerate}"
            )
        if not 1 <= self.channels <= 128:
            raise ConfigError("audio.channels must be between 1 and 128")
        if self.blocksize < 1:
            raise ConfigError("audio.blocksize must be >= 1")


@dataclass(frozen=True)
class DspConfig(Section):
    NAME: ClassVar[str] = "dsp"
    fft_size: int = 32768  # ~1.46 Hz bins at 48 kHz
    hop: int = 2400  # ~20 updates/s
    average_tau: float = 3.0
    display_fmin: float = 20.0  # the TF5's EQ graph spans 20 Hz - 20 kHz; so does the plot
    display_fmax: float = 20000.0
    display_bands_per_octave: int = 48
    db_floor: float = -140.0

    def validate(self) -> None:
        n = self.fft_size
        if n < 1024 or n & (n - 1):
            raise ConfigError(f"dsp.fft_size must be a power of two >= 1024, got {n}")
        if not 1 <= self.hop <= n:
            raise ConfigError(f"dsp.hop must be between 1 and fft_size, got {self.hop}")
        if self.average_tau <= 0:
            raise ConfigError("dsp.average_tau must be > 0")
        if not 0 < self.display_fmin < self.display_fmax <= REQUIRED_SAMPLERATE / 2:
            raise ConfigError(f"need 0 < display_fmin < display_fmax <= {REQUIRED_SAMPLERATE / 2}")


@dataclass(frozen=True)
class SynthConfig(Section):
    """The synthetic test signal (audio.source = "synth"). Levels are AES17 dBFS."""

    NAME: ClassVar[str] = "synth"
    speech_db: float = -20.0
    noise_db: float = -50.0
    tone_hz: float = 2500.0  # the steady "ring" the feedback detector should find
    tone_db: float = -15.0  # relative to speech_db
    seed: int = 1
    # audio.source = "stage" (audio/stage.py): the simulated venue.
    house_mic: int = 31  # input the simulated house mic arrives on; -1 = none
    pa_leak_db: float = -18.0  # PA level reaching an open vocal mic, re the mains
    room_rt60: float = 0.8  # reverberation time of the simulated room, s

    def validate(self) -> None:
        if not -1 <= self.house_mic <= 127:
            raise ConfigError("synth.house_mic must be an input index (0-based) or -1")
        if not -60 <= self.pa_leak_db <= 0:
            raise ConfigError("synth.pa_leak_db must be between -60 and 0")
        if not 0.1 <= self.room_rt60 <= 4:
            raise ConfigError("synth.room_rt60 must be between 0.1 and 4 s")


@dataclass(frozen=True)
class RFConfig(Section):
    NAME: ClassVar[str] = "rf"
    source: str = "device"  # device (tinySA) | demo | off
    auto_connect: bool = True  # keep the tinySA connected and sweeping (range: the QLX-D receivers)
    start_hz: int = 88_000_000
    stop_hz: int = 108_000_000
    points: int = 290
    rbw: str = "auto"
    interval_s: float = 0.5

    @classmethod
    def _prepare(cls, d: dict[str, Any]) -> dict[str, Any]:
        if isinstance(d.get("rbw"), int) and not isinstance(d["rbw"], bool):
            d["rbw"] = str(d["rbw"])  # rbw = 100 means "100" (kHz)
        return d

    def validate(self) -> None:
        _check_source("rf", self.source, DEVICE_SOURCES)
        try:
            _ = self.sweep
        except ValueError as e:
            raise ConfigError(f"[rf] {e}") from None

    @property
    def sweep(self) -> SweepSettings:
        return SweepSettings.from_dict(
            {
                "start_hz": self.start_hz,
                "stop_hz": self.stop_hz,
                "points": self.points,
                "rbw": self.rbw,
                "interval_s": self.interval_s,
            }
        )


@dataclass(frozen=True)
class QLXDConfig(Section):
    NAME: ClassVar[str] = "qlxd"
    source: str = "device"  # device (receivers on the network) | demo | off
    interface: str = ""  # NIC for discovery; "" = first with a 169.254.x.x address
    hosts: tuple[str, ...] = ()  # receivers to always monitor, "ip" or "ip:port"
    state_file: str = "qlxd_state.json"  # UI-edited interface/hosts, overrides the two above
    meter_poll_s: float = 1.0
    full_poll_s: float = 10.0

    def validate(self) -> None:
        _check_source("qlxd", self.source, DEVICE_SOURCES)
        if not all(isinstance(h, str) for h in self.hosts):
            raise ConfigError('qlxd.hosts must be a list of "ip" or "ip:port" strings')
        for key in ("meter_poll_s", "full_poll_s"):
            if not 0.2 <= getattr(self, key) <= 300:
                raise ConfigError(f"qlxd.{key} must be between 0.2 and 300")


@dataclass(frozen=True)
class RingConfig(Section):
    """Ring / feedback detection on every analyzed input (always Main L/R)."""

    NAME: ClassVar[str] = "ring"
    enabled: bool = True
    sensitivity: str = "normal"  # low | normal | high
    fmin: float = 80.0  # Hz, detection range
    fmax: float = 12000.0
    mains_hz: float = 60.0  # skip mains hum harmonics up to 1 kHz (50 in Europe; 0 = off)

    @classmethod
    def _prepare(cls, d: dict[str, Any]) -> dict[str, Any]:
        if isinstance(d.get("sensitivity"), str):
            d["sensitivity"] = d["sensitivity"].lower()
        return d

    def validate(self) -> None:
        if self.sensitivity not in ("low", "normal", "high"):
            raise ConfigError(f"ring.sensitivity must be low, normal or high, got {self.sensitivity!r}")
        if not 20 <= self.fmin < self.fmax <= 20000:
            raise ConfigError("ring.fmin / ring.fmax must satisfy 20 <= fmin < fmax <= 20000")
        if self.mains_hz not in (0.0, 50.0, 60.0):
            raise ConfigError("ring.mains_hz must be 50, 60 or 0")


@dataclass(frozen=True)
class VocalConfig(Section):
    """Vocal EQ assist for mic checks (suggestions; the desk changes only on a click)."""

    NAME: ClassVar[str] = "vocal"
    enabled: bool = False
    inputs: tuple[int, ...] = ()  # 0-based inputs that are vocal mics; () = any input
    listen_s: float = 20.0  # seconds of speech to collect
    max_cut_db: float = 6.0
    max_boost_db: float = 3.0
    house_mic: int = -1  # input of a measurement mic in the house; -1 = none
    auto_allowed: bool = False  # allow auto EQ (still off until a person turns it on per channel)
    profiles_file: str = "eq_profiles.json"  # saved EQ profiles (e.g. per actor)
    room_tilt_db_oct: float = 0.0  # in-room target: reference speech tilted by this (dB / octave re 1 kHz)
    mic_type: str = "generic"  # default mic type (vocal/targets.py MIC_TYPES); the page can pick per channel
    eq_style: str = "speech"  # default EQ target (vocal/targets.py EQ_STYLES); the page can pick per channel
    ringout_ceiling_db: float = 6.0  # ring-out: the highest it pushes a fader (dB) looking for feedback
    ringout_margin_db: float = 6.0  # ring-out: faders end at least this far below where they rang

    def validate(self) -> None:
        from .vocal.targets import EQ_STYLES, MIC_TYPES

        if self.mic_type not in MIC_TYPES:
            raise ConfigError(f"vocal.mic_type must be one of {', '.join(MIC_TYPES)}, got {self.mic_type!r}")
        if self.eq_style not in EQ_STYLES:
            raise ConfigError(f"vocal.eq_style must be one of {', '.join(EQ_STYLES)}, got {self.eq_style!r}")
        if not -20 <= self.ringout_ceiling_db <= 10:
            raise ConfigError("vocal.ringout_ceiling_db must be between -20 and 10")
        if not 3 <= self.ringout_margin_db <= 15:
            raise ConfigError("vocal.ringout_margin_db must be between 3 and 15")
        if not all(isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in self.inputs):
            raise ConfigError("vocal.inputs must be a list of input indices (0-based)")
        if not -1 <= self.house_mic <= 127:
            raise ConfigError("vocal.house_mic must be an input index (0-based) or -1")
        if not -3 <= self.room_tilt_db_oct <= 3:
            raise ConfigError("vocal.room_tilt_db_oct must be between -3 and 3")
        if not 6 <= self.listen_s <= 120:
            raise ConfigError("vocal.listen_s must be between 6 and 120")
        if not 1 <= self.max_cut_db <= 12 or not 0 <= self.max_boost_db <= 6:
            raise ConfigError("vocal.max_cut_db must be 1..12 and vocal.max_boost_db 0..6")


@dataclass(frozen=True)
class TF5Config(Section):
    """The mixer over the network: names and settings (RCP), EQ (TF Editor protocol)."""

    NAME: ClassVar[str] = "tf5"
    source: str = "device"  # device (the console at host; off while host is "") | demo | off
    host: str = ""  # console IP
    port: int = 49280  # Yamaha remote control protocol
    username: str = ""  # the TF5's protocol has no login; kept for completeness, not sent
    password: str = ""
    poll_s: float = 30.0  # full re-read period (changes on the desk are pushed in between)
    usb_patch: tuple[str, ...] = ()  # per USB capture channel: "InCh N", "St L", "St R" or ""; () = TF default
    usb_post_fader: bool = False  # USB feeds taken after the fader? (ring attribution adds the fader if not)
    eq: bool = False  # also read the EQ (TF Editor protocol, read-only) for the spectrum overlay
    eq_port: int = 50368
    eq_write: bool = False  # let a person apply suggested EQ to the desk (vocal assist "Apply to desk")
    fader_write: bool = False  # also input faders / on (the vocal assist's ring-out session; needs eq_write)

    def validate(self) -> None:
        _check_source("tf5", self.source, DEVICE_SOURCES)
        for key in ("port", "eq_port"):
            if not 1 <= getattr(self, key) <= 65535:
                raise ConfigError(f"tf5.{key} must be 1..65535, got {getattr(self, key)}")
        if not 1 <= self.poll_s <= 3600:
            raise ConfigError("tf5.poll_s must be between 1 and 3600")
        for entry in self.usb_patch:
            if not isinstance(entry, str):
                raise ConfigError(f"tf5.usb_patch entries must be strings, got {entry!r}")
            try:
                parse_patch_entry(entry)
            except ValueError as e:
                raise ConfigError(f"[tf5] {e}") from None
        if self.eq_write and not self.eq:
            raise ConfigError("tf5.eq_write needs tf5.eq = true (EQ is written over the same connection)")
        if self.fader_write and not self.eq_write:
            raise ConfigError("tf5.fader_write needs tf5.eq_write = true (faders are written over the same connection)")

    @property
    def active_source(self) -> str:
        """``source``, except that a device without a host is off."""
        return "off" if self.source == "device" and not self.host.strip() else self.source


@dataclass(frozen=True)
class UpdateConfig(Section):
    """Where the /config page's Update button pulls from."""

    NAME: ClassVar[str] = "update"
    remote: str = "https://github.com/hcs-auditoriums/tf-daemon.git"
    branch: str = "main"


@dataclass(frozen=True)
class Channel:
    """A ``[[channels]]`` entry: a named input, always analyzed."""

    index: int
    label: str | None = None  # None: the console's name, else "Ch N"

    @property
    def name(self) -> str:
        return self.label or f"Ch {self.index + 1}"


# The TF's default USB patch puts the stereo bus on USB 33/34; every other
# input can be named from the console ([tf5]).
DEFAULT_CHANNELS = (Channel(32, "Main L"), Channel(33, "Main R"))


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    dsp: DspConfig = field(default_factory=DspConfig)
    synth: SynthConfig = field(default_factory=SynthConfig)
    rf: RFConfig = field(default_factory=RFConfig)
    qlxd: QLXDConfig = field(default_factory=QLXDConfig)
    ring: RingConfig = field(default_factory=RingConfig)
    vocal: VocalConfig = field(default_factory=VocalConfig)
    tf5: TF5Config = field(default_factory=TF5Config)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    channels: tuple[Channel, ...] = DEFAULT_CHANNELS

    def __post_init__(self) -> None:
        if not self.channels:
            raise ConfigError("at least one [[channels]] entry is required")
        seen: set[int] = set()
        for c in self.channels:
            if c.index < 0:
                raise ConfigError(f"channel index must be >= 0, got {c.index}")
            if c.index in seen:
                raise ConfigError(f"channel index {c.index} listed twice")
            seen.add(c.index)
        if self.audio.buffer_seconds * self.audio.samplerate < self.dsp.fft_size + self.dsp.hop:
            raise ConfigError("audio.buffer_seconds is too short to hold one FFT frame plus a hop")


SECTIONS: dict[str, type[Section]] = {
    "server": ServerConfig,
    "audio": AudioConfig,
    "dsp": DspConfig,
    "synth": SynthConfig,
    "rf": RFConfig,
    "qlxd": QLXDConfig,
    "ring": RingConfig,
    "vocal": VocalConfig,
    "tf5": TF5Config,
    "update": UpdateConfig,
}


def _channels_from(value: Any) -> tuple[Channel, ...]:
    if not isinstance(value, list):
        raise ConfigError("[[channels]] must be a list of tables")
    out = []
    for c in value:
        if not isinstance(c, dict) or "index" not in c:
            raise ConfigError(f"[[channels]] entry missing 'index': {c}")
        for key in sorted(set(c) - {"index", "label"}):
            log.warning("Ignoring unknown config key channels.%s", key)
        index = _coerce("channels", "index", 0, c["index"])
        label = _coerce("channels", "label", "", c["label"]) if "label" in c else None
        out.append(Channel(index, label))
    return tuple(out)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def config_from(data: dict[str, Any]) -> Config:
    """A validated Config from TOML-shaped ``data``; missing keys take the defaults."""
    for key in sorted(set(data) - set(SECTIONS) - {"channels"}):
        log.warning("Ignoring unknown config section [%s]", key)
    kwargs: dict[str, Any] = {name: cls.from_dict(data[name]) for name, cls in SECTIONS.items() if name in data}
    if "channels" in data:
        kwargs["channels"] = _channels_from(data["channels"])
    return Config(**kwargs)


def load_config(path: str | Path | None, overrides: dict[str, Any] | None = None) -> Config:
    """Read ``path`` (if given and it exists), apply ``overrides``, validate."""
    user: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("rb") as f:
                user = tomllib.load(f)
            log.info("Loaded config from %s", p)
        else:
            log.warning("Config file %s not found; using defaults", p)
    return config_from(_merge(_upgrade(user), overrides or {}))
