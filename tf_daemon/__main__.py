"""Command-line entry point (development server).

    python -m tf_daemon --source synth
    python -m tf_daemon --source wav recording.wav
    python -m tf_daemon --source device          # the TF5
    python -m tf_daemon --list-devices

Production on the Pi uses gunicorn instead; see gunicorn.conf.py.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .app import build_modules, create_app
from .audio.sources import SourceError
from .config import ConfigError, load_config


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


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
    p = argparse.ArgumentParser(prog="tf-daemon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.toml", help="path to config.toml (default: ./config.toml)")
    p.add_argument("--source", nargs="+", default=["device"], metavar="KIND",
                   help="device | synth | wav PATH (default: device)")
    p.add_argument("--host", help="override server.host")
    p.add_argument("--port", type=int, help="override server.port")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--list-devices", action="store_true", help="print audio devices and exit")
    args = p.parse_args(argv)

    setup_logging(args.log_level)
    if args.list_devices:
        return list_devices()

    try:
        cfg = load_config(args.config)
        kind, wav = parse_source(args.source)
        modules = build_modules(cfg, kind, wav)
    except (ConfigError, SourceError) as e:
        logging.getLogger("tf_daemon").error("%s", e)
        return 2

    app = create_app(cfg, modules)
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    # Reloader off: it would fork a second process and open the device twice.
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
