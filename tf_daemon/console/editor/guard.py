"""What the client may send.

READ-ONLY UNLESS WRITES ARE ENABLED, and then only for what a person asked for.
Every outgoing frame goes through ``_send``, which passes it to ``check_outbound``
first; that accepts only the handshake / keepalive messages listed in ALLOWED_*
below and raises ReadOnlyViolation for anything else, notably:

- set-param (``01 11``), except as below, and full dump (``01 14``);
- ``SYNCDIR:1``: makes the console take the client's memories (overwrite);
- ``TYPE:Editor``: the console allows only one editor;
- any function other than Property / Mixing, any other event.

The one exception: with ``writes=True`` (config ``tf5.eq_write``), ``write()``
sends set-params for input-channel PEQ parameters (WRITABLE, values within
WRITE_LIMITS). With ``fader_writes=True`` as well (config ``tf5.fader_write``),
also an input channel's fader level and on switch (FADER_WRITABLE, within
FADER_LIMITS), for the vocal assist's ring-out session. It is called only from an
explicit user request (the vocal assist's "Apply to desk", or a ring-out a person
started and can stop at any time); nothing in tf-daemon writes on its own. While a
``write()`` runs, ``check_outbound`` lets through exactly the frames of that
request and nothing else; at any other time a set-param is refused as before.

The Property dump contains the console password in plain text; it is dropped
unparsed and never logged.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .codec import (
    CLIENT_TYPE,
    EVENT,
    EVENT_REPLY,
    EVENT_TAG,
    HASH_COMPARE,
    SET_PARAM,
    SUBSCRIBE,
    SYNC_CLOSE,
    SYNC_OPEN,
    T_STR,
    T_U8,
    T_U32,
    TAGS,
    Element,
    Message,
    ProtocolError,
    parse_message,
)

# What may be sent. Everything else raises ReadOnlyViolation.
ALLOWED_PARAM_CMDS = {SUBSCRIBE, HASH_COMPARE, SYNC_OPEN, SYNC_CLOSE}
ALLOWED_EVENTS = {"KeepAlive": "", "SetClientType": CLIENT_TYPE}  # request name -> exact args
ALLOWED_EVENT_REPLIES = {"KeepAlive", "NotifySyncStatus"}  # console events we acknowledge

# The only parameters write() may change: an input channel's PEQ switch, high-pass
# and bands (guides/tf5_api.md section 5). Raw limits per parameter name.
WRITABLE = re.compile(
    r"InputChannel:\d+/PEQ/(On|Bank/HPF/(On|Frequency)"
    r"|Bank/(BandL|Band:\d|BandH)/(Type|Frequency|Gain|Q|LPFOn))"
)
WRITE_LIMITS = {
    "On": (0, 1),
    "LPFOn": (0, 1),
    "Type": (0, 2),
    "HPF/Frequency": (200, 6000),  # 20-600 Hz (raw Hz x 10)
    "Frequency": (200, 200000),  # 20 Hz - 20 kHz
    "Gain": (-1800, 1800),  # +-18 dB (raw dB x 100)
    "Q": (100, 10000),  # 0.1 - 10 (raw x 1000)
}


# With fader writes on as well: an input channel's fader level (raw dB x 100; -32768 = -inf, as
# over RCP) and on switch. Nothing else: not the stereo bus, mixes, sends or DCAs.
FADER_WRITABLE = re.compile(r"InputChannel:\d+/Fader/(Level|On)")
FADER_LIMITS = {
    "Level": (-32768, 1000),  # -inf .. +10 dB
    "On": (0, 1),
}


class ReadOnlyViolation(RuntimeError):
    """An outgoing frame could change console state; it was not sent."""


def write_limits(path: str) -> tuple[int, int]:
    """Raw (min, max) for a WRITABLE or FADER_WRITABLE parameter path."""
    name = path.rsplit("/", 1)[-1]
    if FADER_WRITABLE.fullmatch(path):
        return FADER_LIMITS[name]
    return WRITE_LIMITS["HPF/Frequency"] if path.endswith("HPF/Frequency") else WRITE_LIMITS[name]


def check_outbound(tag: str, payload: bytes, allow_set: Callable[[Message], bool] | None = None) -> None:
    """Raise ReadOnlyViolation unless the frame is one of the read-only handshake messages,
    or a Mixing set-param that ``allow_set`` (given only while a user-requested write runs)
    accepts."""
    try:
        msg = parse_message(payload)
    except ProtocolError as e:
        raise ReadOnlyViolation(f"unparseable outgoing {tag} frame: {e}") from None
    els = msg.elements
    if tag == EVENT_TAG:
        if msg.cmd not in (EVENT, EVENT_REPLY):
            raise ReadOnlyViolation(f"EEVT command {bytes(msg.cmd).hex(' ')} not allowed")
        if (
            len(els) != 4
            or [e.type for e in els] != [T_U32, T_U32, T_STR, T_STR]
            or els[0].value != [0]
            or els[1].value != [0]
        ):
            raise ReadOnlyViolation("malformed event")
        name, args = els[2].value, els[3].value
        if msg.cmd == EVENT:
            if name not in ALLOWED_EVENTS or args != ALLOWED_EVENTS[name]:
                raise ReadOnlyViolation(f"event {name!r} {args!r} not allowed")
        elif name not in ALLOWED_EVENT_REPLIES:
            raise ReadOnlyViolation(f"event reply {name!r} not allowed")
        return
    func = next((f for f, t in TAGS.items() if t == tag), None)
    if func is None:
        raise ReadOnlyViolation(f"frame tag {tag!r} not allowed (only {sorted(TAGS.values())} and EEVT)")
    if msg.cmd == SET_PARAM and tag == TAGS["Mixing"] and allow_set is not None:
        if not allow_set(msg):
            raise ReadOnlyViolation("set-param isn't one of the changes the user asked for")
        return
    if msg.cmd not in ALLOWED_PARAM_CMDS:
        raise ReadOnlyViolation(f"{tag} command {bytes(msg.cmd).hex(' ')} not allowed (read-only client)")
    if msg.cmd in (SYNC_OPEN, SYNC_CLOSE):
        if els:
            raise ReadOnlyViolation("sync bracket carries no elements")
    elif msg.cmd == SUBSCRIBE:
        if len(els) != 2 or els[0] != Element(T_STR, func) or els[1] != Element(T_U8, b"\x80"):
            raise ReadOnlyViolation("malformed subscribe")
    elif msg.cmd == HASH_COMPARE:
        if (
            len(els) != 4
            or els[0] != Element(T_U8, b"\0")
            or els[1] != Element(T_STR, func)
            or els[2].type != T_U8
            or len(els[2].value) != 16
            or els[3].type != T_U8
            or len(els[3].value) != 16
        ):
            raise ReadOnlyViolation("malformed hash compare")
