"""The read-only guard (console/editor/guard.py) and the code structure that makes it the only
way out: nothing the client sends can change the console unless a person asked for it."""

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest
from editor_fakes import (
    EDITOR,
    PKG,
    build_dump,
    dump_message,
    function_dump,
    mixing_tree,
)

from tf_daemon.config import WRITE_SWITCHES, TF5Config
from tf_daemon.console.editor.client import TFEditorClient
from tf_daemon.console.editor.codec import (
    CLIENT_TYPE,
    EVENT,
    HASH_COMPARE,
    SET_PARAM,
    SUBSCRIBE,
    SYNC_OPEN,
    T_S32,
    T_STR,
    T_U8,
    T_U16,
    T_U32,
    element,
    event,
    frame,
    hash_compare,
    message,
    parse_message,
    set_param_message,
    subscribe,
    sync,
)
from tf_daemon.console.editor.dump import Dump
from tf_daemon.console.editor.guard import (
    BASE_FUNCTIONS,
    FADER_LIMITS,
    FADER_WRITABLE,
    FAMILIES,
    SCENE_RECALL,
    WRITABLE,
    WRITE_LIMITS,
    ReadOnlyViolation,
    Rule,
    WriteNotAllowed,
    WriteRefused,
    check_outbound,
    check_write,
    families_for,
    writable,
)
from tf_daemon.console.editor.tree import FUNCTIONS, TAGS


def _set_param_bytes(func, path, idx, value_el, origin=0xA0):
    return message(
        SET_PARAM,
        [
            element(T_U8, b"\0"),
            element(T_STR, func),
            element(T_STR, func),
            element(T_U16, [len(path)]),
            element(T_U32, path),
            element(T_U32, idx),
            element(T_U32, [origin]),
            value_el,
        ],
    )


GUIDE_WRITE_EXAMPLE = (  # guides/tf5_api.md 4.4: set ch1 BandL gain to -5.20 dB. MUST be refused.
    "11 0000006D 011101 08"
    "11 00000001 00"
    "31 00000007 4d6978696e6700"
    "31 00000007 4d6978696e6700"
    "12 00000001 0005"
    "14 00000005 00000000 00000007 00000001 00000003 00000002"
    "14 00000005 00000000 00000000 00000000 00000000 00000000"
    "14 00000001 000000A0"
    "24 00000001 FFFFFDF8"
)


def test_guide_write_example_decodes_and_is_refused():
    payload = bytes.fromhex(GUIDE_WRITE_EXAMPLE.replace(" ", ""))
    m = parse_message(payload)
    assert m.cmd == SET_PARAM and m.elements[4].value == [0, 7, 1, 3, 2] and m.elements[7].value == [-520]
    assert payload == _set_param_bytes("Mixing", [0, 7, 1, 3, 2], [0] * 5, element(T_S32, [-520]))
    with pytest.raises(ReadOnlyViolation):
        check_outbound("MMIX", payload)


def test_guard_accepts_exactly_the_handshake():
    for func, tag in (("Property", "MPRO"), ("Mixing", "MMIX")):
        for payload in (subscribe(func), hash_compare(func), sync(False), sync(True)):
            check_outbound(tag, payload)
    check_outbound("EEVT", event("KeepAlive"))
    check_outbound("EEVT", event("KeepAlive", reply=True))
    check_outbound("EEVT", event("SetClientType", CLIENT_TYPE))
    check_outbound("EEVT", event("NotifySyncStatus", "SCN0INI:23 SCN0REQ:0", reply=True))
    assert CLIENT_TYPE == "TYPE:StageMix SYNCDIR:2"


FORBIDDEN = {
    "set-param Mixing s32": ("MMIX", _set_param_bytes("Mixing", [0, 7, 0, 2, 2], [0] * 5, element(T_S32, [-520]))),
    "set-param Mixing u32": ("MMIX", _set_param_bytes("Mixing", [0, 19, 1], [14, 0, 0], element(T_U32, [0]))),
    "set-param Mixing str": ("MMIX", _set_param_bytes("Mixing", [0, 0, 0], [0, 0, 0], element(T_STR, "x"))),
    "set-param console origin": (
        "MMIX",
        _set_param_bytes("Mixing", [0, 19, 1], [0, 0, 0], element(T_U32, [1]), origin=0xA1),
    ),
    "set-param Property": ("MPRO", _set_param_bytes("Property", [2, 0, 0], [0, 1, 0], element(T_STR, "Editor"))),
    "full dump Mixing": ("MMIX", dump_message("Mixing", build_dump(mixing_tree()))),
    "full dump Property": ("MPRO", dump_message("Property", b"MMSXLIT\0")),
    "unknown 01 05 01": ("MMIX", message((0x01, 0x05, 0x01))),
    "hash reply dir": ("MMIX", message((0x01, 0x10, 0x10), [element(T_U8, b"\0")])),
    "subscribe other function": ("MPRC", subscribe("Processing")),
    "subscribe SceneStatus": ("MSCS", subscribe("SceneStatus")),
    "tag/function mismatch": ("MPRO", subscribe("Mixing")),
    "subscribe bad flag": ("MMIX", message(SUBSCRIBE, [element(T_STR, "Mixing"), element(T_U8, b"\x00")])),
    "hash short md5": (
        "MMIX",
        message(
            HASH_COMPARE,
            [element(T_U8, b"\0"), element(T_STR, "Mixing"), element(T_U8, b"\0" * 4), element(T_U8, bytes(16))],
        ),
    ),
    "sync with payload": ("MMIX", message(SYNC_OPEN, [element(T_U32, [1])])),
    "file transfer": ("d000", message((0x02, 0x01, 0x01))),
    "DS_A": ("DS_A", message((0x04, 0x01, 0x01))),
    "SYNCDIR:1": ("EEVT", event("SetClientType", "TYPE:StageMix SYNCDIR:1")),
    "TYPE:Editor": ("EEVT", event("SetClientType", "TYPE:Editor SYNCDIR:2")),
    "TYPE:Editor SYNCDIR:1": ("EEVT", event("SetClientType", "TYPE:Editor SYNCDIR:1")),
    "RecallScene": ("EEVT", event("RecallScene", "scene_a 1")),
    "ChangeSelectedCH": ("EEVT", event("ChangeSelectedCH", "0")),
    "SendWarningMessage": ("EEVT", event("SendWarningMessage", "hi")),
    "reply to unknown event": ("EEVT", event("RecallScene", "", reply=True)),
    "KeepAlive with args": ("EEVT", event("KeepAlive", "x")),
    "event as set-param": ("EEVT", _set_param_bytes("Mixing", [0], [0], element(T_U32, [0]))),
    "event bad ids": (
        "EEVT",
        message(EVENT, [element(T_U32, [1]), element(T_U32, [0]), element(T_STR, "KeepAlive"), element(T_STR, "")]),
    ),
    "garbage": ("MMIX", b"\x00\x01\x02"),
}


@pytest.mark.parametrize("name", list(FORBIDDEN))
def test_guard_refuses_anything_that_could_change_the_console(name):
    tag, payload = FORBIDDEN[name]
    with pytest.raises(ReadOnlyViolation):
        check_outbound(tag, payload)


class RecordingSocket:
    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


@pytest.mark.parametrize("name", list(FORBIDDEN))
def test_send_refuses_before_touching_the_socket(name):
    tag, payload = FORBIDDEN[name]
    client = TFEditorClient("127.0.0.1", 1)
    sock = RecordingSocket()
    with pytest.raises(ReadOnlyViolation):
        client._send(sock, tag, payload)
    assert sock.sent == b"" and client.sent == []


def test_send_passes_allowed_frames_through():
    client, sock = TFEditorClient("127.0.0.1", 1), RecordingSocket()
    client._send(sock, "MMIX", subscribe("Mixing"))
    assert sock.sent == frame("MMIX", subscribe("Mixing"))
    assert client.sent == [("MMIX", SUBSCRIBE)]


def _code(path: Path) -> str:
    """A module's source without its docstring (which documents the dangers) and comments."""
    src = path.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src), clean=False)
    if doc:
        src = src.replace(doc, "", 1)
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def test_editor_has_a_single_send_path_and_one_guarded_write_builder():
    """Across the editor package: one sendall, in TFEditorClient._send after check_outbound; no
    dump builder; one set-param builder (codec.set_param_message), called only by write()."""
    files = ["__init__.py", "__main__.py", "client.py", "codec.py", "dump.py", "guard.py", "tree.py"]
    # A new module in the package must be looked at here before this test passes.
    assert sorted(p.name for p in EDITOR.glob("*.py")) == files
    code = {name: _code(EDITOR / name) for name in files}
    everything = "\n".join(code.values())
    assert everything.count("sendall(") == 1 and code["client.py"].count("sendall(") == 1, (
        "all sending must go through TFEditorClient._send"
    )
    assert not re.search(r"\.send\(|sendto\(|sendmsg\(", everything)
    send_def = re.search(r"def _send\(self.*?\n(.*?)\n    def ", code["client.py"], re.S).group(1)
    assert send_def.index("check_outbound(") < send_def.index("sendall(")
    assert not re.search(r"message\(\s*DUMP", everything), "no dump builder may exist"
    assert len(re.findall(r"message\(\s*SET_PARAM", everything)) == 1
    assert re.search(r"def set_param_message\(.*?\n\s+return message\(\s*SET_PARAM", code["codec.py"], re.S)
    assert len(re.findall(r"set_param_message\(", everything)) == 2  # its definition and one call
    calls = [m.start() for m in re.finditer(r"set_param_message\(", code["client.py"])]
    write_def = re.search(r"    def write\(self.*?(?=\n    def )", code["client.py"], re.S)
    assert len(calls) == 1 and write_def.start() <= calls[0] < write_def.end()
    assert "SYNCDIR:1" not in everything and "TYPE:Editor" not in everything


def test_desk_writes_are_reachable_only_from_the_confirmed_routes():
    """write() <- TF5Module.write_eq <- either
      * vocal DeskWriter.apply_suggestion / undo <- POST /api/vocal/apply|undo ("confirm": true), or
      * TF5Module's eq_set <- PUT /api/tf5/eq/<usb> ("confirm": true): the page's EQ editing, or
      * vocal DeskWriter.write_values <- profile recall / copy <- POST /api/vocal/profiles/recall|copy
        ("confirm": true), or
      * vocal AutoEQ: its tick (only channels a person turned on: POST /api/vocal/auto with
        "confirm": true, and only when vocal.auto_allowed is set) and its undo (confirmed), or
      * vocal RingOut (notches, and putting the EQ back on Mute & cancel).
    write() <- TF5Module.write_fader (input faders; needs tf5.fader_write) <- either
      * TF5Module's fader_set <- PUT /api/tf5/fader/<usb> ("confirm": true): the sidebar's fader / ON, or
      * vocal RingOut: its loop (started only by start() <- POST /api/vocal/ringout/start with "confirm": true, and
    RingOut exists only when the desk's faders are writable), abort (Mute & cancel: mutes, never
    needs a confirmation) and unmute.
    write() <- TF5Module.write_params (any write family; each needs its own tf5.*_write) <- only
      TF5Module's param_set <- POST /api/tf5/param ("confirm": true): a person's edit.
    write() <- strip_desk.DeskStrip.put (input / 1-knob EQ / gate / comp; the processing, headamp
      and phantom families) <- only Strip.put <- only TF5Module's strip_set <- PUT
      /api/tf5/strip/<usb> ("confirm": true): a person's edit on the strip panel (added 2026-11-10).
    recall_scene() <- TF5Module.recall_scene (needs tf5.scene_recall) <- only
      TF5Module's recall_scene_route <- POST /api/tf5/event/recall_scene ("confirm": true).
    Nothing else in tf-daemon calls them."""
    calls: dict[str, list[str]] = {}
    for p in PKG.rglob("*.py"):
        code = "\n".join(line.split("#", 1)[0] for line in p.read_text(encoding="utf-8").splitlines())
        for name, pattern in (
            ("write", r"(\beq_client|\bec|TFEditorClient\b.*)\.write\("),
            ("write_eq", r"\.write_eq\("),
            ("write_fader", r"\.write_fader\("),
            ("write_params", r"\.write_params\("),
            ("recall_scene", r"(\beq_client|\bec|self)\.recall_scene\("),
            ("apply_suggestion", r"\.apply_suggestion\("),
            ("undo", r"\.undo\("),
            ("put", r"\.(?:strip|desk)\.put\("),
        ):
            for _ in re.finditer(pattern, code):
                calls.setdefault(name, []).append(p.relative_to(PKG).as_posix())
    # in write_eq, write_fader and write_params; and DeskStrip.put (the strip panel)
    assert sorted(calls.get("write", [])) == ["console/module.py"] * 3 + ["console/strip_desk.py"]
    strip_src = (PKG / "console" / "strip_desk.py").read_text(encoding="utf-8")
    strip_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", strip_src)}
    strip_writes = [m.start() for m in re.finditer(r"\bec\.write\(", strip_src)]
    assert [max((n for n, at in strip_defs.items() if at < w), key=lambda n: strip_defs[n]) for w in strip_writes] == [
        "put"
    ]
    # DeskStrip.put only from Strip.put, and that only from the strip route, after the confirmation.
    assert sorted(calls.get("put", [])) == ["console/module.py", "console/strip.py"]
    strip_set = (PKG / "console" / "module.py").read_text(encoding="utf-8")
    strip_set = strip_set[strip_set.index("def strip_set(usb: int):") :]
    assert strip_set.index('.get("confirm") is not True') < strip_set.index("self.strip.put(")
    console_src = (PKG / "console" / "module.py").read_text(encoding="utf-8")
    assert "ec = self.eq_client" in console_src
    console_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", console_src)}
    ec_writes = [m.start() for m in re.finditer(r"\bec\.write\(", console_src)]
    assert sorted(
        max((n for n, at in console_defs.items() if at < w), key=lambda n: console_defs[n]) for w in ec_writes
    ) == ["write_eq", "write_fader", "write_params"]
    # The generic write (any family) and the scene recall: each from its one route only, after the confirmation.
    assert calls.get("write_params") == ["console/module.py"] and calls.get("recall_scene") == ["console/module.py"] * 2
    ec_recalls = [m.start() for m in re.finditer(r"\bec\.recall_scene\(", console_src)]
    owner = lambda pos: max((n for n, at in console_defs.items() if at < pos), key=lambda n: console_defs[n])  # noqa: E731
    assert [owner(w) for w in ec_recalls] == ["recall_scene"]
    param_set = console_src[console_src.index("def param_set():") :]
    assert param_set.index('.get("confirm") is not True') < param_set.index("self.write_params(")
    recall_route = console_src[console_src.index("def recall_scene_route():") :]
    assert recall_route.index('.get("confirm") is not True') < recall_route.index("self.recall_scene(")
    assert sorted(calls.get("write_eq", [])) == [
        "console/module.py",
        "vocal/auto.py",
        "vocal/auto.py",
        "vocal/desk.py",
        "vocal/desk.py",
        "vocal/ringout.py",
        "vocal/ringout.py",
    ]
    assert sorted(calls.get("write_fader", [])) == ["console/module.py"] + ["vocal/ringout.py"] * 4
    # The sidebar's fader / ON: PUT /api/tf5/fader/<usb>, refused without "confirm": true before any write.
    fader_set = console_src[console_src.index("def fader_set(usb: int):") :]
    assert fader_set.index('.get("confirm") is not True') < fader_set.index("self.write_fader(")
    # The page's EQ editing: PUT /api/tf5/eq/<usb>, refused without "confirm": true before any write.
    eq_set = console_src[console_src.index("def eq_set(usb: int):") :]
    assert eq_set.index('.get("confirm") is not True') < eq_set.index("self.write_eq(")
    vocal_src = (PKG / "vocal" / "module.py").read_text(encoding="utf-8")
    assert re.search(r"desk_write\(self\.desk\.apply_suggestion\)", vocal_src)
    assert re.search(r"desk_write\(self\.desk\.undo\)", vocal_src)
    assert "apply_suggestion" not in calls and "undo" not in calls  # never called, only handed to desk_write
    assert '.get("confirm") is not True' in vocal_src
    # Profile recall / copy: write_values, only from those two methods, only handed to desk_write.
    assert sorted(re.findall(r"desk_write\(lambda ch: self\.(recall_profile|copy_eq)\(", vocal_src)) == [
        "copy_eq",
        "recall_profile",
    ]
    assert len(re.findall(r"\.(recall_profile|copy_eq)\(", vocal_src)) == 2
    defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", vocal_src)}
    writes = [m.start() for m in re.finditer(r"\.write_values\(", vocal_src)]
    assert sorted(max((n for n, at in defs.items() if at < w), key=lambda n: defs[n]) for w in writes) == [
        "copy_eq",
        "recall_profile",
    ]
    # Auto EQ: exists only with vocal.auto_allowed; turning it on and its undo need the confirmation.
    assert re.search(r"self\.auto = \(?\s*AutoEQ\(.*?\)\s*\\?\s*if cfg\.auto_allowed\s+else None", vocal_src, re.S)
    assert re.search(r"desk_write\(self\.auto\.arm\)", vocal_src)
    assert re.search(r"desk_write\(self\.auto\.undo\)", vocal_src)
    auto_src = (PKG / "vocal" / "auto.py").read_text(encoding="utf-8")
    tick = re.search(r"    def tick\(self.*?(?=\n    def )", auto_src, re.S).group(0)
    assert "list(self._channels.items())" in tick  # only channels a person turned on
    writers = [m.start() for m in re.finditer(r"\.write_eq\(", auto_src)]
    defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", auto_src)}
    owner = lambda pos: max((n for n, at in defs.items() if at < pos), key=lambda n: defs[n])  # noqa: E731
    assert sorted(owner(w) for w in writers) == ["_tick_channel", "undo"]
    # Ring-out: exists only with writable faders; starting it needs the confirmation; its loop
    # thread is started only by start(); Mute & cancel (abort) and unmute need none.
    assert re.search(r"getattr\(desk, \"fader_writable\", False\)", vocal_src)
    assert re.search(r"self\.ringout = \(\s*RingOut\(.*?\)\s*if faders is not None\s+else None", vocal_src, re.S)
    ro_route = vocal_src[vocal_src.index("def ringout_start():") :]
    assert ro_route.index('.get("confirm") is not True') < ro_route.index("r.start(")
    assert len(re.findall(r"\br\.start\(", vocal_src)) == 1
    ro_src = (PKG / "vocal" / "ringout.py").read_text(encoding="utf-8")
    ro_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", ro_src)}
    ro_owner = lambda pos: max((n for n, at in ro_defs.items() if at < pos), key=lambda n: ro_defs[n])  # noqa: E731
    assert sorted(ro_owner(m.start()) for m in re.finditer(r"\.write_fader\(", ro_src)) == [
        "_fader",
        "_restore",
        "abort",
        "unmute",
    ]
    assert [ro_owner(m.start()) for m in re.finditer(r"threading\.Thread\(target=self\._run", ro_src)] == ["start"]
    assert "the EQ was changed on the desk" in auto_src  # it stops when someone else touches it


# ====================================================================== write families
# The owner asked for these (2026-11-10): each family is opt-in behind its own switch, every
# value within the V4.55 schema (narrowed where the guard says so), strings only from a list.

FAMILY_CASES = [
    # family, function, path, an allowed value, a refused value
    ("eq", "Mixing", "InputChannel:3/PEQ/Bank/BandL/Gain", -400, -2400),
    ("eq", "Mixing", "InputChannel:3/PEQ/Bank/Band:1/Q", 10000, 10001),  # narrower than the schema's 16000
    ("fader", "Mixing", "InputChannel:3/Fader/Level", -32768, 1001),
    ("mix", "Mixing", "DCA:0/Fader/Level", -1000, 1001),
    ("mix", "Mixing", "Mono/Fader/On", 0, 2),
    ("mix", "Mixing", "Stereo:0/Output/Balance", -63, 64),
    ("mix", "Mixing", "InputChannel:0/ToStereo/Pan", 63, -64),
    ("mix", "Mixing", "InputChannel:2/ToMix:3/Pre", 1, 2),
    ("mix", "Mixing", "StInChannel:1/ToStMix:11/Level", 0, -32769),
    ("mix", "Mixing", "InputChannel:2/DCAGroup:7/Assign", 1, -1),
    ("mix", "Mixing", "FxRtnChannel:0/MuteGroup:3/Assign", 1, 2),
    ("mix", "FX", "InsFX:5/MuteGroup:1/Assign", 1, 2),
    ("mix", "Setup", "MuteMaster:5/On", 1, 2),
    ("mix", "Volatile", "Cue/InputChannel:14/On", 1, 2),
    ("processing", "Mixing", "InputChannel:0/Input/Gain", -2400, 1001),
    ("processing", "Mixing", "InputChannel:0/Gate/Bank/Gate/Threshold", -7200, 1),
    ("processing", "Mixing", "InputChannel:0/Gate/Bank/Type", "GATE", "DUCKING"),
    ("processing", "Mixing", "Mono/Comp/Bank/Compressor/Knee", "HARD", "SOFT-9"),
    ("processing", "Mixing", "InputChannel:0/Comp/Bank/DeEsser/Q", 1600, 10001),
    ("processing", "Mixing", "Matrix:3/PEQ/Bank/Band:1/Q", 16000, 16001),
    ("processing", "Mixing", "Mono/PEQ/Bank/LPF/Slope", 24, 30),
    ("processing", "Mixing", "InputChannel:0/PEQ/Bank/OneKnob/Mode", "Vocal", "Rock"),
    ("processing", "Mixing", "InputChannel:0/PEQ/Bank/Type", "BOLD", "PRECISE"),
    ("processing", "Mixing", "Mix:7/GEQ/Band:30/Gain", 1500, 1600),
    ("processing", "Mixing", "StInChannel:0/Ducker/Release", 3340, 3339),
    ("processing", "Mixing", "Matrix:0/Delay/Time", 1000000, 1000001),
    ("processing", "Mixing", "StMix:0/Output/Gain", -2400, 1),
    ("headamp", "Processing", "BuiltIn/InputPort:31/HeadAmp/Gain", 6600, 6601),
    ("headamp", "Processing", "Slot/InputPort:63/HeadAmp/Gain", -600, -601),
    ("phantom", "Processing", "BuiltIn/InputPort:0/HeadAmp/48V", 1, 2),
    ("phantom", "Setup", "PhantomMaster/On", 0, 2),
    ("label", "Mixing", "InputChannel:0/Label/Name", "Choir", "x" * 64),
    ("label", "Mixing", "DCA:7/Label/Name", "Band ü", "tab\there"),
    ("label", "Mixing", "Mix:0/Label/Color", "SkyBlue", "Blu"),
    ("label", "Mixing", "Mono/Label/Icon", "WirelessMic", "Guitar"),
    ("fx", "FX", "FX:0/Param/Bank/Value:63", -2147483648, "1"),
    ("fx", "FX", "InsFX:2/Bypass", 1, 2),
]

NEAR_MISSES = [  # one step off a family's patterns: in no family at all
    ("Mixing", "InputChannel:3/PEQ/OneKnobBackup/BandL/Gain", 0),  # the console's own copy
    ("Mixing", "InputChannel:3/PEQ/Bank/HPF/Slope", 12),
    ("Mixing", "InputChannel:3/DirectOut/Level", 0),
    ("Mixing", "FxBus:0/Fader/Level", 0),
    ("Mixing", "Mono:0/Fader/Level", 0),  # Mono has no index
    ("Mixing", "InputChannel:0/Input/GainOffset", 0),
    ("Mixing", "InputChannel:0/AutoMixer/Weight", 0),
    ("Mixing", "StInChannel:0/Ducker/KeyIn_TF/Source", 0),
    ("Mixing", "InputChannel:0/Patch/Select", 1),
    ("Mixing", "InputChannel:0/Library/LastRecallPreset/Title", "x"),
    ("Mixing", "InputChannel:0/Label/Name/", "x"),
    ("Mixing", "xInputChannel:0/Fader/Level", 0),
    ("Mixing", "Setup/MuteMaster:0/On", 1),  # right path, wrong function
    ("Setup", "MuteMaster:0/Name", "x"),
    ("Setup", "HAControl/On", 1),
    ("Setup", "CommSession/MobileMix/Password", "x"),
    ("Processing", "BuiltIn/OutputPort:0/Patch/Source", 0),
    ("Volatile", "Cue/FxBus:0/On", 0),
    ("Volatile", "ConsoleLock/Lock", 1),
    ("FX", "FX:0/Param/PluginName", "Echo"),
    ("Property", "UserAccount/Unit/Password", "x"),
]
ALL_FAMILIES = set(FAMILIES)


def _frame(func, path, value):
    """The set-param write() would send for ``path`` on the simulated V4.55 desk."""
    wp, idx, prm = Dump(function_dump(func)).address(path)
    return TAGS[func], set_param_message(wp, idx, prm, value, func)


@pytest.mark.parametrize("family, func, path, good, bad", FAMILY_CASES)
def test_family_allows_in_range_only(family, func, path, good, bad):
    assert check_write(func, path, good, {family}).name == family
    assert writable(func, path, {family}).name == family
    with pytest.raises(WriteRefused):
        check_write(func, path, bad, {family})
    with pytest.raises(ReadOnlyViolation):  # a WriteRefused is a ReadOnlyViolation (and a ValueError)
        check_write(func, path, bad, ALL_FAMILIES)


@pytest.mark.parametrize("family, func, path, good, bad", FAMILY_CASES)
def test_family_is_refused_with_its_switch_off(family, func, path, good, bad):
    with pytest.raises(WriteNotAllowed, match=FAMILIES[family].switch):
        check_write(func, path, good, ALL_FAMILIES - {family})
    assert writable(func, path, ALL_FAMILIES - {family}) is None


@pytest.mark.parametrize("family, func, path, good, bad", FAMILY_CASES)
def test_family_frames_are_refused_outside_a_write_call(family, func, path, good, bad):
    tag, payload = _frame(func, path, good)
    for functions in (FUNCTIONS, BASE_FUNCTIONS):
        with pytest.raises(ReadOnlyViolation):
            check_outbound(tag, payload, functions=functions)
    check_outbound(tag, payload, lambda m: True, FUNCTIONS)  # only a write call's own test lets it out
    with pytest.raises(ReadOnlyViolation):
        check_outbound(tag, payload, lambda m: False, FUNCTIONS)


@pytest.mark.parametrize("func, path, value", NEAR_MISSES)
def test_near_misses_are_in_no_family(func, path, value):
    assert families_for(func, path) == []
    with pytest.raises(WriteNotAllowed, match="not a writable parameter"):
        check_write(func, path, value, ALL_FAMILIES)


def test_existing_families_are_unchanged():
    assert FAMILIES["eq"].switch == "tf5.eq_write" and FAMILIES["fader"].switch == "tf5.fader_write"
    assert FAMILIES["eq"].rules == (Rule("Mixing", WRITABLE, WRITE_LIMITS),)
    assert FAMILIES["fader"].rules == (Rule("Mixing", FADER_WRITABLE, FADER_LIMITS),)
    assert WRITABLE.pattern == (
        r"InputChannel:\d+/PEQ/(On|Bank/HPF/(On|Frequency)|Bank/(BandL|Band:\d|BandH)/(Type|Frequency|Gain|Q|LPFOn))"
    )
    assert FADER_WRITABLE.pattern == r"InputChannel:\d+/Fader/(Level|On)"
    assert WRITE_LIMITS == {
        "On": (0, 1),
        "LPFOn": (0, 1),
        "Type": (0, 2),
        "HPF/Frequency": (200, 6000),
        "Frequency": (200, 200000),
        "Gain": (-1800, 1800),
        "Q": (100, 10000),
    }
    assert FADER_LIMITS == {"Level": (-32768, 1000), "On": (0, 1)}


def test_every_family_has_its_own_switch_off_by_default():
    defaults = {f.name: f.default for f in fields(TF5Config)}
    switches = [f.switch for f in FAMILIES.values()]
    assert len(set(switches)) == len(switches) and SCENE_RECALL not in switches
    for key in [*switches, SCENE_RECALL]:
        assert defaults[key.removeprefix("tf5.")] is False, key
    assert set(WRITE_SWITCHES) == {s.removeprefix("tf5.") for s in [*switches, SCENE_RECALL]} - {
        "eq_write",
        "fader_write",
    }


@pytest.mark.parametrize(
    "func, path, value",
    [
        ("Mixing", "InputChannel:0/Label/Category", "Vocal"),  # no category verified yet
        ("Mixing", "InputChannel:0/Label/Color", 3),  # a number for a string
        ("Mixing", "InputChannel:0/Fader/Level", "0"),  # a string for a number
        ("Mixing", "InputChannel:0/Fader/On", True),
        ("Mixing", "InputChannel:40/Fader/On", 1),  # no such channel
        ("Mixing", "Mix:8/GEQ/Band:0/Gain", 0),
        ("Mixing", "Mix:0/GEQ/Band:31/Gain", 0),
        ("Mixing", "StMix:0/ToStereo/Pan", 0),  # StMix has Assign only
        ("Mixing", "Mix:0/PEQ/Bank/LPF/On", 1),  # only Mono has the LPF
        ("FX", "FX:0/Param/Bank/Value:64", 0),
    ],
)
def test_values_and_parameters_outside_the_schema_are_refused(func, path, value):
    with pytest.raises(WriteRefused):
        check_write(func, path, value, ALL_FAMILIES)


def test_set_params_never_for_property_or_another_function():
    tag, payload = _frame("Mixing", "DCA:0/Fader/Level", 0)
    with pytest.raises(ReadOnlyViolation):  # a Mixing set-param on the Processing channel
        check_outbound("MPRC", payload, lambda m: True, FUNCTIONS)
    prop = Dump(function_dump("Property"))
    wp, idx, prm = prop.address("Status/CommSession:0/ClientType")
    with pytest.raises(ReadOnlyViolation):
        check_outbound("MPRO", set_param_message(wp, idx, prm, "Editor", "Property"), lambda m: True, FUNCTIONS)


def test_other_functions_only_when_subscribed():
    for func in FUNCTIONS:
        for payload in (subscribe(func), hash_compare(func), sync(False), sync(True)):
            check_outbound(TAGS[func], payload, functions=FUNCTIONS)
            if func not in BASE_FUNCTIONS:
                with pytest.raises(ReadOnlyViolation):
                    check_outbound(TAGS[func], payload)
                with pytest.raises(ReadOnlyViolation):
                    check_outbound(TAGS[func], payload, functions=BASE_FUNCTIONS)
    with pytest.raises(ReadOnlyViolation):
        check_outbound("MPRC", subscribe("Setup"), functions=FUNCTIONS)
    with pytest.raises(ReadOnlyViolation):
        check_outbound("MXXX", subscribe("Meter"), functions=[*FUNCTIONS, "Meter"])


def test_recall_scene_only_as_asked():
    ok = event("RecallScene", "NUMBER:A05")
    with pytest.raises(ReadOnlyViolation):  # outside recall_scene()
        check_outbound("EEVT", ok)
    check_outbound("EEVT", ok, allow_event=lambda n, a: (n, a) == ("RecallScene", "NUMBER:A05"))
    check_outbound("EEVT", event("RecallScene", "NUMBER:B99"), allow_event=lambda n, a: True)
    with pytest.raises(ReadOnlyViolation):  # not the one asked for
        check_outbound("EEVT", event("RecallScene", "NUMBER:A06"), allow_event=lambda n, a: a == "NUMBER:A05")
    for args in ("NUMBER:C05", "NUMBER:A5", "NUMBER:A100", "NUMBER:a05", "NUMBER:A05 X", "scene_a 5", ""):
        with pytest.raises(ReadOnlyViolation):
            check_outbound("EEVT", event("RecallScene", args), allow_event=lambda n, a: True)
    for other in (event("ChangeSelectedCH", "CHINFO:1"), event("Transport", "OPERATE:Play")):
        with pytest.raises(ReadOnlyViolation):  # a recall in progress opens nothing else
            check_outbound("EEVT", other, allow_event=lambda n, a: True)
    with pytest.raises(ReadOnlyViolation):
        check_outbound("EEVT", event("RecallScene", "NUMBER:A05", reply=True), allow_event=lambda n, a: True)
