"""Shared by the TF Editor protocol tests: the test doubles (they live in tf_daemon.console.sim_editor,
part of the demo console), assert_read_only, and where the captures are."""

from __future__ import annotations

from pathlib import Path

from helpers import wait_until

import tf_daemon
from tf_daemon.console.editor.codec import DUMP, EVENT, EVENT_REPLY, SET_PARAM, SUBSCRIBE, parse_message
from tf_daemon.console.editor.guard import ALLOWED_PARAM_CMDS, check_outbound
from tf_daemon.console.sim_editor import (
    PASSWORD,
    C,
    SimEditor,
    build_dump,
    console_hash,
    default_mixing_values,
    dump_message,
    mixing_tree,
    property_tree,
    set_param,
    wire_address,
)

PKG = Path(tf_daemon.__file__).parent
EDITOR = PKG / "console" / "editor"
# Every EQ / fader push the production TF5 sent to tf-daemon while InCh 1's EQ was changed on the
# desk and from TF Editor (2026-11-08, tcpdump on the server; wire addresses and values only).
PUSHES = Path(__file__).parent / "data" / "tf5_eq_pushes.json"
CAPTURED_NAMES = {
    (0, 7, 0): "PEQ/On",
    (0, 7, 1, 1, 0): "PEQ/Bank/OneKnob/On",
    (0, 7, 1, 2, 0): "PEQ/Bank/HPF/On",
    (0, 7, 1, 2, 1): "PEQ/Bank/HPF/Frequency",
    (0, 7, 1, 3, 1): "PEQ/Bank/BandL/Frequency",
    (0, 7, 1, 3, 2): "PEQ/Bank/BandL/Gain",
    (0, 7, 1, 4, 1): "PEQ/Bank/Band:{3}/Frequency",
    (0, 7, 1, 4, 2): "PEQ/Bank/Band:{3}/Gain",
    (0, 7, 1, 5, 2): "PEQ/Bank/BandH/Frequency",
    (0, 7, 1, 5, 3): "PEQ/Bank/BandH/Gain",
    (0, 19, 0): "Fader/Level",
    (0, 19, 1): "Fader/On",
}


def assert_read_only(fake):
    """Every frame the client sent passes the guard and is a handshake/keepalive message."""
    assert fake.raw, "client sent nothing"
    for tag, payload in fake.raw:
        check_outbound(tag, payload)
        cmd = parse_message(payload).cmd
        assert cmd in ALLOWED_PARAM_CMDS | {EVENT, EVENT_REPLY}
        assert cmd not in (SET_PARAM, DUMP)
        assert tag in ("MPRO", "MMIX", "EEVT")
    for _cmd, name, args in fake.events_from_client():
        if name == "SetClientType":
            assert args == "TYPE:StageMix SYNCDIR:2"


__all__ = [
    "C",
    "SimEditor",
    "PASSWORD",
    "build_dump",
    "console_hash",
    "default_mixing_values",
    "dump_message",
    "mixing_tree",
    "property_tree",
    "set_param",
    "wait_until",
    "wire_address",
    "assert_read_only",
    "PKG",
    "EDITOR",
    "PUSHES",
    "CAPTURED_NAMES",
    "SUBSCRIBE",
    "EVENT_REPLY",
]
