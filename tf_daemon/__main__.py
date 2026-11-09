"""Command-line entry point (development server).

    python -m tf_daemon --source synth
    python -m tf_daemon --source wav recording.wav
    python -m tf_daemon --source device          # the TF5
    python -m tf_daemon --list-devices
    python -m tf_daemon --demo          # no hardware at all: simulated stage, tinySA, QLX-D and TF5

Production on the Pi uses gunicorn instead; see gunicorn.conf.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from .app import build_modules, create_app
from .audio.sources import SourceError
from .config import ConfigError, load_config


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def source_overrides(audio: str | None = None, wav: str | None = None, rf: str | None = None,
                     qlxd: str | None = None, tf5: str | None = None, demo: bool = False) -> dict[str, Any]:
    """Config overrides for the sources picked on the command line (or gunicorn's
    environment). ``demo`` simulates everything; None leaves the config's choice."""
    if demo:
        audio, rf, qlxd, tf5 = "stage", "demo", "demo", "demo"
    out: dict[str, Any] = {}
    if audio:
        out["audio"] = {"source": audio.lower(), **({"wav": wav} if wav else {})}
    for section, source in (("rf", rf), ("qlxd", qlxd), ("tf5", tf5)):
        if source:
            out[section] = {"source": source.lower()}
    return out


def parse_source(values: list[str]) -> tuple[str, str | None]:
    kind = values[0].lower()
    if kind == "wav":
        if len(values) != 2:
            raise SystemExit("usage: --source wav PATH.wav")
        return kind, values[1]
    if len(values) != 1:
        raise SystemExit(f"--source {kind} takes no extra argument")
    return kind, None


def list_devices() -> int:
    try:
        import sounddevice as sd
    except OSError as e:
        print(f"PortAudio not available: {e}\nOn Raspberry Pi OS: sudo apt install libportaudio2")
        return 1
    print(sd.query_devices())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tf-daemon", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.toml", help="path to config.toml (default: ./config.toml)")
    p.add_argument("--source", nargs="+", metavar="KIND",
                   help="audio: device | synth | stage | wav PATH (default: audio.source from config)")
    p.add_argument("--rf-source", choices=["device", "demo", "off"],
                   help="tinySA RF module: device | demo | off (default: rf.source from config)")
    p.add_argument("--qlxd-source", choices=["device", "demo", "off"],
                   help="Shure QLX-D module: device | demo | off (default: qlxd.source from config)")
    p.add_argument("--tf5-source", choices=["device", "demo", "off"],
                   help="TF5 network module: device (tf5.host) | demo (simulated console) | off "
                        "(default: tf5.source from config)")
    p.add_argument("--demo", action="store_true",
                   help="simulate all hardware: --source stage --rf-source demo --qlxd-source demo --tf5-source demo")
    p.add_argument("--host", help="override server.host")
    p.add_argument("--port", type=int, help="override server.port")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    args = p.parse_args(argv)

    setup_logging(args.log_level)
    if args.list_devices:
        return list_devices()

    kind, wav = parse_source(args.source) if args.source else (None, None)
    overrides = source_overrides(kind, wav, args.rf_source, args.qlxd_source, args.tf5_source, demo=args.demo)
    if args.host or args.port:
        overrides["server"] = {k: v for k, v in (("host", args.host), ("port", args.port)) if v}
    try:
        cfg = load_config(args.config, overrides)
        modules = build_modules(cfg)
    except (ConfigError, SourceError) as e:
        logging.getLogger("tf_daemon").error("%s", e)
        return 2

    app = create_app(cfg, modules, config_path=args.config)
    host, port = cfg.server.host, cfg.server.port
    # Reloader off: it would fork a second process and open the device twice.
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
