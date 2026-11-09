"""The read-only guard (console/editor/guard.py) and the code structure that makes it the only
way out: nothing the client sends can change the console unless a person asked for it."""

import ast
import re
from pathlib import Path

import pytest
from editor_fakes import (
    EDITOR,
    PKG,
    build_dump,
    dump_message,
    mixing_tree,
)

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
    subscribe,
    sync,
)
from tf_daemon.console.editor.guard import ReadOnlyViolation, check_outbound


def _set_param_bytes(func, path, idx, value_el, origin=0xA0):
    return message(SET_PARAM, [element(T_U8, b"\0"), element(T_STR, func), element(T_STR, func),
                               element(T_U16, [len(path)]), element(T_U32, path), element(T_U32, idx),
                               element(T_U32, [origin]), value_el])


GUIDE_WRITE_EXAMPLE = (  # guides/tf5_api.md 4.4: set ch1 BandL gain to -5.20 dB. MUST be refused.
    "11 0000006D 011101 08"
    "11 00000001 00"
    "31 00000007 4d6978696e6700"
    "31 00000007 4d6978696e6700"
    "12 00000001 0005"
    "14 00000005 00000000 00000007 00000001 00000003 00000002"
    "14 00000005 00000000 00000000 00000000 00000000 00000000"
    "14 00000001 000000A0"
    "24 00000001 FFFFFDF8")


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
    "set-param console origin": ("MMIX", _set_param_bytes("Mixing", [0, 19, 1], [0, 0, 0], element(T_U32, [1]),
                                                          origin=0xA1)),
    "set-param Property": ("MPRO", _set_param_bytes("Property", [2, 0, 0], [0, 1, 0], element(T_STR, "Editor"))),
    "full dump Mixing": ("MMIX", dump_message("Mixing", build_dump(mixing_tree()))),
    "full dump Property": ("MPRO", dump_message("Property", b"MMSXLIT\0")),
    "unknown 01 05 01": ("MMIX", message((0x01, 0x05, 0x01))),
    "hash reply dir": ("MMIX", message((0x01, 0x10, 0x10), [element(T_U8, b"\0")])),
    "subscribe other function": ("MPRC", subscribe("Processing")),
    "subscribe SceneStatus": ("MSCS", subscribe("SceneStatus")),
    "tag/function mismatch": ("MPRO", subscribe("Mixing")),
    "subscribe bad flag": ("MMIX", message(SUBSCRIBE, [element(T_STR, "Mixing"), element(T_U8, b"\x00")])),
    "hash short md5": ("MMIX", message(HASH_COMPARE, [element(T_U8, b"\0"), element(T_STR, "Mixing"),
                                                      element(T_U8, b"\0" * 4), element(T_U8, bytes(16))])),
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
    "event bad ids": ("EEVT", message(EVENT, [element(T_U32, [1]), element(T_U32, [0]), element(T_STR, "KeepAlive"),
                                              element(T_STR, "")])),
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
    files = ["__init__.py", "__main__.py", "client.py", "codec.py", "dump.py", "guard.py"]
    # A new module in the package must be looked at here before this test passes.
    assert sorted(p.name for p in EDITOR.glob("*.py")) == files
    code = {name: _code(EDITOR / name) for name in files}
    everything = "\n".join(code.values())
    assert everything.count("sendall(") == 1 and code["client.py"].count("sendall(") == 1, \
        "all sending must go through TFEditorClient._send"
    assert not re.search(r"\.send\(|sendto\(|sendmsg\(", everything)
    send_def = re.search(r"def _send\(self.*?\n(.*?)\n    def ", code["client.py"], re.S).group(1)
    assert send_def.index("check_outbound(") < send_def.index("sendall(")
    assert not re.search(r"message\(\s*DUMP", everything), "no dump builder may exist"
    assert len(re.findall(r"message\(\s*SET_PARAM", everything)) == 1
    assert re.search(r"def set_param_message\(.*?\n\s+return message\(SET_PARAM", code["codec.py"], re.S)
    assert len(re.findall(r"set_param_message\(", everything)) == 2   # its definition and one call
    calls = [m.start() for m in re.finditer(r"set_param_message\(", code["client.py"])]
    write_def = re.search(r"    def write\(self.*?(?=\n    def )", code["client.py"], re.S)
    assert len(calls) == 1 and write_def.start() <= calls[0] < write_def.end()
    assert "SYNCDIR:1" not in everything and "TYPE:Editor" not in everything


def test_desk_writes_are_reachable_only_from_the_confirmed_routes():
    """write() <- TF5Module.write_eq <- vocal DeskWriter.apply_suggestion / undo <- POST /api/vocal/apply|undo
    (which need "confirm": true). Nothing else in tf-daemon calls them, so nothing writes on its own."""
    calls: dict[str, list[str]] = {}
    for p in PKG.rglob("*.py"):
        code = "\n".join(line.split("#", 1)[0] for line in p.read_text(encoding="utf-8").splitlines())
        for name, pattern in (("write", r"(\beq_client|\bec|TFEditorClient\b.*)\.write\("),
                              ("write_eq", r"\.write_eq\("),
                              ("apply_suggestion", r"\.apply_suggestion\("), ("undo", r"\.undo\(")):
            for _ in re.finditer(pattern, code):
                calls.setdefault(name, []).append(p.relative_to(PKG).as_posix())
    assert calls.get("write") == ["console/module.py"]        # ec.write(...) in TF5Module.write_eq only
    assert "ec = self.eq_client" in (PKG / "console" / "module.py").read_text(encoding="utf-8")
    assert sorted(calls.get("write_eq", [])) == ["vocal/desk.py", "vocal/desk.py"]   # apply_suggestion, undo
    vocal_src = (PKG / "vocal" / "module.py").read_text(encoding="utf-8")
    assert re.search(r"desk_write\(self\.desk\.apply_suggestion\)", vocal_src)
    assert re.search(r"desk_write\(self\.desk\.undo\)", vocal_src)
    assert "apply_suggestion" not in calls and "undo" not in calls   # never called, only handed to desk_write
    assert '.get("confirm") is not True' in vocal_src

