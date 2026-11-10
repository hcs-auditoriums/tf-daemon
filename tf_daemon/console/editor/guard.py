"""What the client may send.

READ-ONLY UNLESS WRITES ARE ENABLED, and then only for what a person asked for.
Every outgoing frame goes through ``_send``, which passes it to ``check_outbound``
first; that accepts only the handshake / keepalive messages listed in ALLOWED_*
below and raises ReadOnlyViolation for anything else, notably:

- set-param (``01 11``), except as below, and full dump (``01 14``);
- ``SYNCDIR:1``: makes the console take the client's memories (overwrite);
- ``TYPE:Editor``: the console allows only one editor;
- any function the client wasn't configured to subscribe (``functions``: Property and Mixing
  unless config ``tf5.functions`` says more), any other event.

The exception: WRITE FAMILIES (FAMILIES below). Each is a named set of parameters (a function
and full-match path patterns), with raw limits per parameter (the V4.55 schema's min / max from
tree.py unless narrowed here; a string only from an explicit allowed set), behind its own config
switch, all off by default:

- ``eq`` (``tf5.eq_write``): input-channel PEQ switch, HPF and bands (WRITABLE, WRITE_LIMITS);
- ``fader`` (``tf5.fader_write``, needs eq_write): an input channel's fader level and on switch
  (FADER_WRITABLE, FADER_LIMITS), for the vocal assist's ring-out session;
- ``mix``, ``processing``, ``headamp``, ``phantom``, ``label``, ``fx`` (``tf5.<name>_write``):
  the rest of the desk, see each family's ``doc``.

The widening beyond ``eq`` / ``fader`` was deliberately requested by the owner (2026-11-10);
each family is opt-in. ``write()`` is called only from an explicit user request (a person's
edit on a page, the vocal assist's "Apply to desk", or a ring-out a person started and can stop
at any time); nothing in tf-daemon writes on its own. While a ``write()`` runs,
``check_outbound`` lets through exactly the frames of that request and nothing else; at any
other time a set-param is refused as before. Likewise the one action event,
``RecallScene "NUMBER:A05"`` (``tf5.scene_recall``), only while ``recall_scene()`` runs for a
person's request, with exactly those arguments.

The Property dump contains the console password in plain text; it is dropped
unparsed and never logged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field

from . import tree
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
    Element,
    Message,
    ProtocolError,
    parse_message,
)

# What may be sent. Everything else raises ReadOnlyViolation.
ALLOWED_PARAM_CMDS = {SUBSCRIBE, HASH_COMPARE, SYNC_OPEN, SYNC_CLOSE}
ALLOWED_EVENTS = {"KeepAlive": "", "SetClientType": CLIENT_TYPE}  # request name -> exact args
ALLOWED_EVENT_REPLIES = {"KeepAlive", "NotifySyncStatus"}  # console events we acknowledge
BASE_FUNCTIONS = ("Property", "Mixing")  # subscribed unless the client is given more

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

# The one action event a person may trigger (tf5.scene_recall): bank A / B, the number as the
# desk formats it (Setup/Scene/LastScene reads "A13").
RECALL_SCENE = "RecallScene"
RECALL_ARGS = re.compile(r"NUMBER:[AB]\d{2,3}")


class ReadOnlyViolation(RuntimeError):
    """An outgoing frame could change console state; it was not sent."""


class WriteRefused(ReadOnlyViolation, ValueError):
    """A requested change isn't allowed as asked (bad value, unknown parameter); nothing was sent."""


class WriteNotAllowed(WriteRefused):
    """No enabled write family covers the parameter (or its switch is off); nothing was sent."""


# ---------------------------------------------------------------------- write families


@dataclass(frozen=True)
class Rule:
    """Parameters of one function that a family may write: a full-match name-path pattern;
    raw limits by parameter name or path suffix (the longest matching suffix wins), which
    narrow the schema's; and the allowed values of string parameters, by suffix (None: any
    printable text that fits). A string parameter without an entry can't be written."""

    func: str
    path: re.Pattern[str]
    limits: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    strings: Mapping[str, frozenset[str] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Family:
    name: str
    switch: str  # the config key that enables it
    doc: str
    rules: tuple[Rule, ...]


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives))


_IDX = r":\d+"
# Strips other than input channels (Mono has no index: there is one).
_BUSES = rf"(?:StInChannel|FxRtnChannel|Group|Mix|StMix|Matrix|Stereo|DCA){_IDX}|Mono"
_STRIPS = rf"InputChannel{_IDX}|{_BUSES}"
_SOURCES = rf"(?:InputChannel|StInChannel|FxRtnChannel){_IDX}"  # strips with DCA / mute group assignment
_PEQ_BUSES = rf"(?:StInChannel|FxRtnChannel|Group|Mix|StMix|Matrix|Stereo){_IDX}|Mono"
_OUTPUTS = rf"(?:Mix|StMix|Matrix|Stereo){_IDX}|Mono"
_CUE = rf"(?:InputChannel|StInChannel|FxRtnChannel|Group|Mix|StMix|Matrix|Stereo|DCA|FX|InsFX){_IDX}|Mono"

# Strings with an enumerated meaning: only the values seen on the auditorium desk
# (docs/desk-coverage-howto-complete.md 3.1, 3.4, 3.5). Extend when another is verified.
COLORS = frozenset({"Blue", "Orange", "Purple", "Pink", "Red", "SkyBlue", "Green", "Yellow"})
ICONS = frozenset({"DynamicMic", "WirelessMic", "Keyboard", "Mixer", "Star2", "Blank"})
CATEGORIES: frozenset[str] = frozenset()  # none verified yet: Category can't be written
EQ_TYPES = frozenset({"BOLD"})
ONE_KNOB_MODES = frozenset({"Vocal", "Intensity", "Loudness"})

FAMILIES: dict[str, Family] = {
    f.name: f
    for f in (
        Family(
            "eq", "tf5.eq_write", "input-channel PEQ switch, HPF and bands", (Rule("Mixing", WRITABLE, WRITE_LIMITS),)
        ),
        Family(
            "fader",
            "tf5.fader_write",
            "input-channel fader level and on (the ring-out); needs eq_write",
            (Rule("Mixing", FADER_WRITABLE, FADER_LIMITS),),
        ),
        Family(
            "mix",
            "tf5.mix_write",
            "faders / ON of every other strip, balance, pan, sends, DCA and mute group assignment, mute masters, cue",
            (
                Rule(
                    "Mixing",
                    _rx(
                        rf"(?:{_BUSES})/Fader/(?:Level|On)",
                        rf"(?:{_OUTPUTS})/Output/Balance",
                        rf"(?:{_STRIPS})/ToStereo/(?:Pan|Assign)",
                        rf"(?:{_STRIPS})/(?:ToMix{_IDX}|ToStMix{_IDX}|ToFX{_IDX}|ToMono|ToMatrix{_IDX})/(?:Level|On|Pre|Pan)",
                        rf"{_SOURCES}/(?:DCAGroup|MuteGroup){_IDX}/Assign",
                    ),
                ),
                Rule("FX", _rx(rf"(?:FX|InsFX){_IDX}/MuteGroup{_IDX}/Assign")),
                Rule("Setup", _rx(rf"MuteMaster{_IDX}/On")),
                Rule("Volatile", _rx(rf"Cue/(?:{_CUE})/On")),
            ),
        ),
        Family(
            "processing",
            "tf5.processing_write",
            "phase / digital gain, gate, compressor, bus PEQ, 1-knob EQ, EQ type, GEQ, leveler / ducker, "
            "delay, output gain / phase",
            (
                Rule(
                    "Mixing",
                    _rx(
                        rf"(?:InputChannel|StInChannel){_IDX}/Input/(?:Phase|Gain)",
                        rf"InputChannel{_IDX}/Gate/(?:On|Bank/Type|Bank/Gate/(?:Threshold|Attack|Range|Hold|Decay))",
                        rf"(?:{_STRIPS})/Comp/(?:On|Bank/Type|Bank/OneKnob/(?:On|Depth)"
                        r"|Bank/Compressor/(?:Threshold|Attack|Release|Ratio|Knee|Gain)"
                        r"|Bank/DeEsser/(?:Threshold|Frequency|Q|Type))",
                        rf"(?:{_PEQ_BUSES})/PEQ/(?:On|Bank/(?:BandL|Band{_IDX}|BandH)/(?:Type|Frequency|Gain|Q|HPFOn|LPFOn)"
                        r"|Bank/LPF/(?:On|Frequency|Slope))",
                        rf"(?:InputChannel{_IDX}|{_PEQ_BUSES})/PEQ/Bank/(?:Type|OneKnob/(?:On|Mode|Depth))",
                        rf"(?:Mix|Stereo){_IDX}/GEQ/(?:On|LRLink|Band{_IDX}/Gain)",
                        rf"StInChannel{_IDX}/(?:Leveler/(?:On|Threshold|Gain)|Ducker/(?:On|Threshold|Range|Release))",
                        rf"(?:StInChannel|Matrix){_IDX}/Delay/(?:On|Time|TimeOffset|LRLink)",
                        rf"(?:{_OUTPUTS})/Output/(?:Gain|Phase)",
                    ),
                    strings={
                        "Gate/Bank/Type": frozenset({"GATE"}),
                        "Comp/Bank/Type": frozenset({"COMP"}),
                        "Compressor/Knee": frozenset({"SOFT-2", "HARD"}),
                        "DeEsser/Type": frozenset({"BPF"}),
                        "PEQ/Bank/Type": EQ_TYPES,
                        "OneKnob/Mode": ONE_KNOB_MODES,
                    },
                ),
            ),
        ),
        Family(
            "headamp",
            "tf5.headamp_write",
            "head-amp gain of the local and slot input ports",
            (Rule("Processing", _rx(rf"(?:BuiltIn|Slot)/InputPort{_IDX}/HeadAmp/Gain")),),
        ),
        Family(
            "phantom",
            "tf5.phantom_write",
            "+48V per input port and the phantom master (phantom power can damage gear)",
            (
                Rule("Processing", _rx(rf"(?:BuiltIn|Slot)/InputPort{_IDX}/HeadAmp/48V")),
                Rule("Setup", _rx(r"PhantomMaster/On")),
            ),
        ),
        Family(
            "label",
            "tf5.label_write",
            "strip names, colours, icons (categories once a value is verified)",
            (
                Rule(
                    "Mixing",
                    _rx(rf"(?:{_STRIPS})/Label/(?:Name|Color|Icon|Category)"),
                    strings={"Name": None, "Color": COLORS, "Icon": ICONS, "Category": CATEGORIES},
                ),
            ),
        ),
        Family(
            "fx",
            "tf5.fx_write",
            "FX 1/2 and insert FX bypass, mix balance and parameter values",
            (Rule("FX", _rx(rf"(?:FX|InsFX){_IDX}/(?:Bypass|MixBalance|Param/Bank/Value{_IDX})")),),
        ),
    )
}
SCENE_RECALL = "tf5.scene_recall"  # the switch for RecallScene (an event, not a family)


def write_limits(path: str) -> tuple[int, int]:
    """Raw (min, max) for a WRITABLE or FADER_WRITABLE parameter path."""
    name = path.rsplit("/", 1)[-1]
    if FADER_WRITABLE.fullmatch(path):
        return FADER_LIMITS[name]
    return WRITE_LIMITS["HPF/Frequency"] if path.endswith("HPF/Frequency") else WRITE_LIMITS[name]


def _suffix(table: Mapping[str, object], path: str) -> str | None:
    keys = [k for k in table if path == k or path.endswith("/" + k)]
    return max(keys, key=len) if keys else None


def families_for(func: str, path: str) -> list[tuple[Family, Rule]]:
    """Every family (and its rule) whose pattern covers ``func``/``path``, enabled or not."""
    return [(f, r) for f in FAMILIES.values() for r in f.rules if r.func == func and r.path.fullmatch(path)]


def check_write(func: str, path: str, value: object, enabled: Collection[str]) -> Family:
    """The family that allows setting ``func``/``path`` to raw ``value`` with the families
    ``enabled``; WriteNotAllowed if none covers it or none of those is enabled, WriteRefused
    if the value isn't allowed (out of range, wrong type, a string not on the allowed list) or
    the parameter isn't on a V4.55 desk."""
    found = families_for(func, path)
    usable = [(f, r) for f, r in found if f.name in enabled]
    if not usable:
        if found:
            raise WriteNotAllowed(f"{func}/{path}: writing it is off ({' / '.join(f.switch for f, _ in found)})")
        raise WriteNotAllowed(f"{func}/{path}: not a writable parameter")
    try:
        s = tree.spec(func, path)
    except KeyError as e:
        raise WriteRefused(f"{func}/{path}: no such parameter on the desk ({e.args[0]})") from None
    errors = []
    for fam, rule in usable:
        problem = _value_problem(rule, s, path, value)
        if problem is None:
            return fam
        errors.append(problem)
    raise WriteRefused(f"{func}/{path}: {errors[0]}")


def _value_problem(rule: Rule, s: tree.Spec, path: str, value: object) -> str | None:
    if s.type == "str":
        key = _suffix(rule.strings, path)
        if key is None:
            return "this text parameter can't be written"
        if not isinstance(value, str):
            return f"{value!r} isn't text"
        allowed = rule.strings[key]
        if allowed is None:
            if not value.isprintable() or len(value.encode("utf-8")) > (s.max or 0):
                return f"{value!r} isn't printable text of at most {s.max} bytes"
        elif value not in allowed:
            return f"{value!r} isn't one of {', '.join(sorted(allowed)) or 'the verified values (none yet)'}"
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{value!r} isn't a whole number"
    lo, hi = s.min, s.max
    key = _suffix(rule.limits, path)
    if key is not None:
        nlo, nhi = rule.limits[key]
        lo, hi = (nlo if lo is None else max(lo, nlo)), (nhi if hi is None else min(hi, nhi))
    if lo is None or hi is None or not lo <= value <= hi:
        return f"{value!r} outside {lo}..{hi}"
    return None


def writable(func: str, path: str, enabled: Collection[str]) -> Family | None:
    """The enabled family covering ``func``/``path`` (for display; the value is checked on write)."""
    return next((f for f, _ in families_for(func, path) if f.name in enabled), None)


def check_recall(args: str) -> None:
    """WriteRefused unless ``args`` are a RecallScene's ``NUMBER:<A|B><00..99>``."""
    if not RECALL_ARGS.fullmatch(args) or int(args[8:]) > 99:
        raise WriteRefused(f"scene {args[7:]!r}: not A00..A99 / B00..B99")


# ---------------------------------------------------------------------- the outbound check


def check_outbound(
    tag: str,
    payload: bytes,
    allow_set: Callable[[Message], bool] | None = None,
    functions: Collection[str] = BASE_FUNCTIONS,
    allow_event: Callable[[str, str], bool] | None = None,
) -> None:
    """Raise ReadOnlyViolation unless the frame is one of the read-only handshake messages
    (for one of ``functions``), a set-param that ``allow_set`` (given only while a
    user-requested write runs) accepts, or a RecallScene that ``allow_event`` (only while a
    user-requested recall runs) accepts."""
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
            if name == RECALL_SCENE and allow_event is not None:
                check_recall(args)
                if not allow_event(name, args):
                    raise ReadOnlyViolation("RecallScene isn't the recall the user asked for")
            elif name not in ALLOWED_EVENTS or args != ALLOWED_EVENTS[name]:
                raise ReadOnlyViolation(f"event {name!r} {args!r} not allowed")
        elif name not in ALLOWED_EVENT_REPLIES:
            raise ReadOnlyViolation(f"event reply {name!r} not allowed")
        return
    func = tree.function_of_tag(tag)
    if func is None or func not in functions:
        allowed = sorted(tree.TAGS[f] for f in functions if f in tree.TAGS)
        raise ReadOnlyViolation(f"frame tag {tag!r} not allowed (only {allowed} and EEVT)")
    if msg.cmd == SET_PARAM and func != "Property" and allow_set is not None:
        if len(els) < 3 or els[1] != Element(T_STR, func) or els[2] != Element(T_STR, func):
            raise ReadOnlyViolation(f"set-param on {tag} for another function")
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
