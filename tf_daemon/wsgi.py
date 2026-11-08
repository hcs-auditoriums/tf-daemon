"""WSGI entry point for gunicorn.

Configured through environment variables (CLI flags don't reach gunicorn):
    TF_DAEMON_CONFIG   path to config.toml          (default: config.toml)
    TF_DAEMON_SOURCE   device | synth | wav         (default: device)
    TF_DAEMON_WAV      WAV path when SOURCE=wav
    TF_DAEMON_RF_SOURCE  device | demo | off        (default: rf.source from config)
    TF_DAEMON_QLXD_SOURCE  device | demo | off      (default: qlxd.source from config)
    TF_DAEMON_LOG      log level                    (default: INFO)

Must be served by exactly ONE worker process: the audio device is opened when
this module is imported. gunicorn.conf.py enforces that.
"""

from __future__ import annotations

import logging
import os

from .__main__ import setup_logging
from .app import build_modules, create_app
from .config import load_config

setup_logging(os.environ.get("TF_DAEMON_LOG", "INFO"))
_cfg = load_config(os.environ.get("TF_DAEMON_CONFIG", "config.toml"))
_modules = build_modules(_cfg, os.environ.get("TF_DAEMON_SOURCE", "device").lower(), os.environ.get("TF_DAEMON_WAV"),
                         os.environ.get("TF_DAEMON_RF_SOURCE"), os.environ.get("TF_DAEMON_QLXD_SOURCE"))
app = create_app(_cfg, _modules)
logging.getLogger(__name__).info("tf-daemon WSGI app ready")
