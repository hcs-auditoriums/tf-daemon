"""Test doubles for the TF Editor protocol; they live in tf_daemon.tf5sim (also the demo console)."""

from __future__ import annotations

import time

from tf_daemon.tf5sim import (
    PASSWORD,
    C,
    FakeEditorConsole,
    build_dump,
    console_hash,
    default_mixing_values,
    dump_message,
    mixing_tree,
    property_tree,
    set_param,
    wire_address,
)
from tf_daemon.tfeditor import EVENT_REPLY, SUBSCRIBE


def wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


__all__ = ["C", "FakeEditorConsole", "PASSWORD", "build_dump", "console_hash", "default_mixing_values",
           "dump_message", "mixing_tree", "property_tree", "set_param", "wait_until", "wire_address",
           "SUBSCRIBE", "EVENT_REPLY"]
