"""TF Editor protocol client (tf_daemon/tfeditor.py): framing, the read-only guard,
MMSXLIT parsing, EQ extraction / curves, the client against a fake console, and
the /api/tf5/eq route.

The most important property under test: the client NEVER sends anything that
could change the console (see check_outbound).
"""

import binascii
import csv
import json
import logging
import re
import socket
import struct
from pathlib import Path

import numpy as np
import pytest
import tf_editor_reference as ref
from tfeditor_fakes import (
    PASSWORD,
    C,
    FakeEditorConsole,
    build_dump,
    default_mixing_values,
    dump_message,
    mixing_tree,
    set_param,
    wait_until,
    wire_address,
)

import tf_daemon.tfeditor as tfe
from tf_daemon.app import build_modules, create_app
from tf_daemon.config import ConfigError, load_config
from tf_daemon.tfeditor import (
    ALLOWED_PARAM_CMDS,
    CLIENT_TYPE,
    CURVE_FREQS,
    DESC_MD5,
    DUMP,
    EVENT,
    EVENT_REPLY,
    HASH_COMPARE,
    SET_PARAM,
    SUBSCRIBE,
    SYNC_CLOSE,
    SYNC_OPEN,
    T_S8,
    T_S16,
    T_S32,
    T_STR,
    T_U8,
    T_U16,
    T_U32,
    Dump,
    Element,
    FrameReader,
    ProtocolError,
    ReadOnlyViolation,
    TFEditorClient,
    biquad,
    channel_eq,
    check_outbound,
    dump_blob,
    element,
    eq_curve,
    event,
    frame,
    hash_compare,
    message,
    parse_elements,
    parse_message,
    section_db,
    subscribe,
    sync,
)

RE_DIR = Path.home() / "yamaha-tf-re" / "capture"   # the reverse-engineering workspace, if present
REAL_DUMP = RE_DIR / "dump_MMIX.bin"
REAL_CSV = RE_DIR / "tcp50368.csv"
EDITOR_IP = "10.99.40.20"
# Every EQ / fader push the production TF5 sent to tf-daemon while InCh 1's EQ was changed on the
# desk and from TF Editor (2026-11-08, tcpdump on the server; wire addresses and values only).
PUSHES = Path(__file__).parent / "data" / "tf5_eq_pushes.json"
CAPTURED_NAMES = {
    (0, 7, 0): "PEQ/On", (0, 7, 1, 1, 0): "PEQ/Bank/OneKnob/On", (0, 7, 1, 2, 0): "PEQ/Bank/HPF/On",
    (0, 7, 1, 2, 1): "PEQ/Bank/HPF/Frequency", (0, 7, 1, 3, 1): "PEQ/Bank/BandL/Frequency",
    (0, 7, 1, 3, 2): "PEQ/Bank/BandL/Gain", (0, 7, 1, 4, 1): "PEQ/Bank/Band:{3}/Frequency",
    (0, 7, 1, 4, 2): "PEQ/Bank/Band:{3}/Gain", (0, 7, 1, 5, 2): "PEQ/Bank/BandH/Frequency",
    (0, 7, 1, 5, 3): "PEQ/Bank/BandH/Gain", (0, 19, 0): "Fader/Level", (0, 19, 1): "Fader/On",
}


@pytest.fixture
def mixing():
    return Dump(build_dump(mixing_tree(), default_mixing_values(), md5=DESC_MD5["Mixing"]))


@pytest.fixture
def console():
    fake = FakeEditorConsole()
    yield fake
    fake.close()


@pytest.fixture
def client_for():
    clients = []

    def make(fake, **kw):
        c = TFEditorClient("127.0.0.1", fake.port, **kw)
        clients.append(c)
        c.start()
        return c
    yield make
    for c in clients:
        c.stop()


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


# ====================================================================== elements, messages, frames

@pytest.mark.parametrize("t, value", [
    (T_U16, [0, 1, 65535]), (T_U32, [0, 0xA0, 0xFFFFFFFF]), (T_S8, [-128, 127]), (T_S16, [-32768, 5]),
    (T_S32, [-520, 2**31 - 1]), (T_U32, []), (T_U8, b"\x00\x80\xff"), (T_U8, b""), (T_STR, "Mixing"),
    (T_STR, ""), (T_STR, "Lead Vox ü"),
])
def test_element_roundtrip(t, value):
    raw = element(t, value)
    assert parse_elements(raw, 1) == [Element(t, value)]


def test_element_wire_format_is_big_endian_with_count():
    assert element(T_U16, [5]) == bytes.fromhex("12 00000001 0005")
    assert element(T_S32, [-520]) == bytes.fromhex("24 00000001 FFFFFDF8")
    assert element(T_STR, "Mixing") == bytes.fromhex("31 00000007") + b"Mixing\0"   # count includes the NUL
    assert element(T_U32, [0, 7, 0, 2, 2]) == bytes.fromhex("14 00000005" + "00000000 00000007 00000000"
                                                            "00000002 00000002".replace(" ", ""))
    with pytest.raises(ValueError):
        element(0x44, [1])


@pytest.mark.parametrize("raw", [
    b"\x14\x00\x00",                              # header cut short
    bytes.fromhex("14 00000002 00000001"),       # data cut short
    bytes.fromhex("31 00000010") + b"abc",        # string cut short
    bytes.fromhex("44 00000001 00"),              # unknown type
])
def test_parse_elements_rejects_truncated_or_unknown(raw):
    with pytest.raises(ProtocolError):
        parse_elements(raw, 1)


def test_message_roundtrip_and_layout():
    payload = message(SYNC_OPEN)
    assert payload == bytes.fromhex("11 00000004 010401 00")        # 9 bytes, as on the wire
    m = parse_message(subscribe("Mixing"))
    assert m.cmd == SUBSCRIBE and m.elements == [Element(T_STR, "Mixing"), Element(T_U8, b"\x80")]
    for bad in (b"", b"\x14" + bytes(8), bytes.fromhex("11 000000ff 010401 00"), bytes.fromhex("11 00000002 0104")):
        with pytest.raises(ProtocolError):
            parse_message(bad)


def test_handshake_messages_match_the_captured_lengths():
    # Payload lengths of TF Editor's own frames in the capture (guides/tf5_api.md, session 65).
    assert len(subscribe("Property")) == 29
    assert len(hash_compare("Property")) == 71
    assert len(subscribe("Mixing")) == 27
    assert len(hash_compare("Mixing")) == 69
    assert len(sync(False)) == len(sync(True)) == 9
    assert len(event("KeepAlive")) == len(event("KeepAlive", reply=True)) == 48
    assert len(event("SetClientType", "TYPE:Editor SYNCDIR:2")) == 73   # we send StageMix: 2 bytes more
    hc = parse_message(hash_compare("Mixing")).elements
    assert hc[2].value.hex() == DESC_MD5["Mixing"] and hc[3].value == bytes(16)   # zero data hash -> full dump


def test_frame_reader_reassembles_split_and_joined_frames():
    a, b = frame("MMIX", sync(False)), frame("MFX", message(SYNC_CLOSE))
    assert b[:4] == b"MFX\0"
    r = FrameReader()
    out = []
    for byte in a + b + a:
        out += r.feed(bytes([byte]))
    assert [t for t, _ in out] == ["MMIX", "MFX", "MMIX"]
    assert FrameReader().feed(a + b) == [("MMIX", sync(False)), ("MFX", message(SYNC_CLOSE))]
    assert FrameReader().feed(a[:7]) == []


@pytest.mark.parametrize("data", [b"\x00\x01\x02\x03" + bytes(4), b"MM X" + bytes(4),
                                  b"MMIX" + struct.pack(">I", tfe.MAX_FRAME + 1)])
def test_frame_reader_rejects_garbage(data):
    with pytest.raises(ProtocolError):
        FrameReader().feed(data)


def test_frame_rejects_bad_tags():
    for tag in ("", "TOOLONG"):
        with pytest.raises(ValueError):
            frame(tag, b"")


# ====================================================================== the read-only guard

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


def test_module_has_a_single_send_path_and_one_guarded_write_builder():
    src = Path(tfe.__file__).read_text(encoding="utf-8")
    src_code = src.replace(tfe.__doc__, "")                            # the docstring documents the dangers
    code = "\n".join(line.split("#", 1)[0] for line in src_code.splitlines())   # ignore comments
    assert code.count("sendall(") == 1, "all sending must go through TFEditorClient._send"
    assert not re.search(r"\.send\(|sendto\(|sendmsg\(", code)
    send_def = re.search(r"def _send\(self.*?\n(.*?)\n    def ", src, re.S).group(1)
    assert send_def.index("check_outbound(") < send_def.index("sendall(")
    assert not re.search(r"message\(\s*DUMP", code), "no dump builder may exist"
    # Exactly one set-param builder, used only by write() (which the guard checks frame by frame).
    assert len(re.findall(r"message\(\s*SET_PARAM", code)) == 1
    assert re.search(r"def set_param_message\(.*?\n\s+return message\(SET_PARAM", code, re.S)
    users = [m.start() for m in re.finditer(r"set_param_message\(", code)]
    write_def = re.search(r"    def write\(self.*?(?=\n    def )", code, re.S)
    assert len(users) == 2 and write_def.start() <= users[1] < write_def.end()
    assert "SYNCDIR:1" not in code and "TYPE:Editor" not in code


def test_desk_writes_are_reachable_only_from_the_confirmed_routes():
    """write() <- TF5Module.write_eq <- VocalModule.apply_suggestion / undo <- POST /api/vocal/apply|undo
    (which need "confirm": true). Nothing else in tf-daemon calls them, so nothing writes on its own."""
    pkg = Path(tfe.__file__).parent
    calls: dict[str, list[str]] = {}
    for p in pkg.rglob("*.py"):
        code = "\n".join(line.split("#", 1)[0] for line in p.read_text(encoding="utf-8").splitlines())
        for name, pattern in (("write", r"(\beq_client|\bec|TFEditorClient\b.*)\.write\("),
                              ("write_eq", r"\.write_eq\("),
                              ("apply_suggestion", r"\.apply_suggestion\("), ("undo", r"\.undo\(")):
            for _ in re.finditer(pattern, code):
                calls.setdefault(name, []).append(p.name)
    assert calls.get("write") == ["tf5.py"]                    # ec.write(...) in write_eq only
    assert "ec = self.eq_client" in (pkg / "tf5.py").read_text(encoding="utf-8")
    assert sorted(calls.get("write_eq", [])) == ["vocal.py", "vocal.py"]
    vocal_src = (pkg / "vocal.py").read_text(encoding="utf-8")
    assert re.search(r"desk_write\(self\.apply_suggestion\)", vocal_src)
    assert re.search(r"desk_write\(self\.undo\)", vocal_src)
    assert "apply_suggestion" not in calls and "undo" not in calls   # never called, only handed to desk_write
    assert '.get("confirm") is not True' in vocal_src


@pytest.mark.skipif(not REAL_CSV.exists(), reason="TF Editor capture not available")
def test_guard_against_tf_editors_own_traffic():
    """Replay what TF Editor sent: the handshake passes, every write / Editor claim is refused."""
    rows = list(csv.reader(open(REAL_CSV)))
    data = b"".join(binascii.unhexlify(p) for _, _, st, src, _, p in rows if st == "65" and src == EDITOR_IP)
    accepted, refused = [], []
    for tag, payload in FrameReader().feed(data):
        try:
            check_outbound(tag, payload)
            accepted.append((tag, parse_message(payload)))
        except ReadOnlyViolation:
            refused.append((tag, parse_message(payload)))
    assert ("MPRO", SUBSCRIBE) in [(t, m.cmd) for t, m in accepted]
    assert ("MMIX", HASH_COMPARE) in [(t, m.cmd) for t, m in accepted]
    assert all(m.cmd in ALLOWED_PARAM_CMDS | {EVENT, EVENT_REPLY} for _, m in accepted)
    assert all(t in ("MPRO", "MMIX", "EEVT") for t, _ in accepted)
    refused_cmds = {(t, m.cmd) for t, m in refused}
    assert ("MSCS", SET_PARAM) in refused_cmds                   # its 545 SceneStatus writes
    assert any(t == "EEVT" and m.elements[2].value == "SetClientType" for t, m in refused)   # TYPE:Editor
    assert not any(m.cmd == SET_PARAM for _, m in accepted)


@pytest.mark.skipif(not REAL_CSV.exists(), reason="TF Editor capture not available")
def test_console_side_of_the_capture_parses():
    rows = list(csv.reader(open(REAL_CSV)))
    data = b"".join(binascii.unhexlify(p) for _, _, st, src, _, p in rows if st == "65" and src != EDITOR_IP)
    frames = FrameReader().feed(data)
    msgs = [(t, parse_message(p)) for t, p in frames]
    dumps = {t: Dump(dump_blob(m)) for t, m in msgs if m.cmd == DUMP and t == "MMIX"}
    assert dumps["MMIX"].func == "Mixing" and dumps["MMIX"].desc_md5 == DESC_MD5["Mixing"]
    sets = [m for t, m in msgs if t == "MMIX" and m.cmd == SET_PARAM]
    assert sets
    for m in sets:
        name, _, _ = dumps["MMIX"].resolve(m.elements[4].value, m.elements[5].value)
        assert name.endswith("Fader/On")                        # the console-side mute changes


# ====================================================================== MMSXLIT dump

def test_dump_parses_header_and_tree(mixing):
    assert mixing.func == "Mixing" and mixing.desc_md5 == DESC_MD5["Mixing"]
    names = [c.name for c in mixing.root.children]
    assert names == ["InputChannel", "StInChannel", "Mix", "Stereo"]
    ic = mixing.root.child("InputChannel")
    assert ic.arraysize == 40
    assert len(mixing.data) == mixing.root.datasize
    assert mixing.get("InputChannel:0/Label/Name") == "GRA"
    assert mixing.get("InputChannel:0/PEQ/Bank/BandL/Gain") == -520      # signed
    assert mixing.get("InputChannel:0/PEQ/Bank/BandH/Frequency") == 100000
    assert mixing.get("InputChannel:39/Label/Name") == "ch40"
    assert mixing.get("Stereo:1/PEQ/Bank/BandL/HPFOn") == 1


@pytest.mark.parametrize("path", ["InputChannel:40/Label/Name", "InputChannel:0/PEQ/Bank/Band:2/Gain",
                                  "InputChannel:0/Nope/On", "InputChannel:0/PEQ/Nope", "Nope:0/Label/Name",
                                  "InputChannel:-1/Label/Name"])
def test_locate_rejects_missing_or_out_of_range(mixing, path):
    with pytest.raises(KeyError):
        mixing.locate(path)


def test_resolve_matches_wire_addresses(mixing):
    wp, idx = wire_address(mixing, "InputChannel:14/Fader/On")
    name, off, prm = mixing.resolve(wp, idx)
    assert name == "InputChannel:14/Fader/On" and prm.name == "On"
    assert off == mixing.locate("InputChannel:14/Fader/On")[0]
    wp, idx = wire_address(mixing, "InputChannel:3/PEQ/Bank/Band:1/Gain")
    assert mixing.resolve(wp, idx)[0] == "InputChannel:3/PEQ/Bank/Band:1/Gain"


@pytest.mark.parametrize("path, idx", [([], []), ([0, 1], [0]), ([9, 0], [0, 0]), ([0, 99], [0, 0]),
                                       ([0, 0, 99], [0, 0, 0]), ([0, 0, 0], [40, 0, 0]), ([0, 0, 0], [0, 0, 1]),
                                       ([0, 0], [0, 0])])
def test_resolve_rejects_bad_addresses(mixing, path, idx):
    with pytest.raises(ProtocolError):
        mixing.resolve(path, idx)


def test_apply_updates_only_our_copy(mixing):
    before = bytes(mixing.data)
    wp, idx = wire_address(mixing, "InputChannel:5/PEQ/Bank/Band:0/Gain")
    assert mixing.apply(wp, idx, -1234) == "InputChannel:5/PEQ/Bank/Band:0/Gain"
    assert mixing.get("InputChannel:5/PEQ/Bank/Band:0/Gain") == -1234
    off, prm = mixing.locate("InputChannel:5/PEQ/Bank/Band:0/Gain")
    after = bytes(mixing.data)
    assert after[:off] == before[:off] and after[off + prm.elemsize:] == before[off + prm.elemsize:]
    wp, idx = wire_address(mixing, "InputChannel:5/Label/Name")
    mixing.apply(wp, idx, "A very long channel name that is far more than sixty-four bytes long, really")
    assert len(mixing.get("InputChannel:5/Label/Name")) == 63
    wp, idx = wire_address(mixing, "InputChannel:5/PEQ/Bank/BandH/Frequency")
    mixing.apply(wp, idx, 200000)
    assert mixing.get("InputChannel:5/PEQ/Bank/BandH/Frequency") == 200000


def test_apply_rejects_wrong_types_and_overflow(mixing):
    gain = wire_address(mixing, "InputChannel:0/PEQ/Bank/BandL/Gain")
    name = wire_address(mixing, "InputChannel:0/Label/Name")
    on = wire_address(mixing, "InputChannel:0/PEQ/On")
    for (wp, idx), v in ((gain, "x"), (name, 5), (gain, 40000), (on, 256), (on, -1)):
        with pytest.raises(ProtocolError):
            mixing.apply(wp, idx, v)
    assert mixing.get("InputChannel:0/PEQ/Bank/BandL/Gain") == -520


def test_dump_rejects_corrupt_blobs():
    good = build_dump(mixing_tree())
    desc_len = struct.unpack_from("<I", good, 0x50)[0]
    bad = [b"", b"MMSXLIX\0" + good[8:], good[:0x60], good[:0x58] + b"XXXX" + good[0x5c:],
           good[:0x50] + struct.pack("<I", desc_len - 32) + good[0x54:],
           good[:0x50] + struct.pack("<I", desc_len + 10 ** 6) + good[0x54:],
           good[:0x54] + struct.pack("<I", 4) + good[0x58:0x58 + desc_len] + bytes(4)]
    for blob in bad:
        with pytest.raises(ProtocolError):
            Dump(blob)


def test_dump_blob_finds_mmsxlit_payload():
    blob = build_dump(mixing_tree())
    assert dump_blob(parse_message(dump_message("Mixing", blob))) == blob
    assert dump_blob(parse_message(message(DUMP, [element(T_U8, b"nope")]))) is None


def test_builder_layout_matches_the_real_peq_layout(mixing):
    # Offsets of the real console's InputChannel/PEQ collection (from its dump, guide section 5).
    peq = mixing.root.child("InputChannel").child("PEQ")
    bank = peq.child("Bank")
    assert [(c.name, c.offset) for c in bank.children] == [("OneKnob", 8), ("HPF", 22), ("BandL", 28), ("Band", 37),
                                                         ("BandH", 55)]
    assert bank.datasize == 65 and bank.child("Band").datasize == 9 and bank.child("Band").arraysize == 2


@pytest.mark.skipif(not REAL_DUMP.exists(), reason="captured Mixing dump not available")
def test_real_console_dump():
    d = Dump(REAL_DUMP.read_bytes())
    assert d.func == "Mixing" and d.desc_md5 == DESC_MD5["Mixing"] and len(d.data) == 38285
    eq = channel_eq(d, "InCh", 0)   # ch1 as in the guide
    assert eq["on"] and eq["bank"] == "BOLD" and not eq["one_knob"]
    assert eq["hpf"] == {"on": True, "freq": 200.0, "slope": 12}
    low, b1, _, high = eq["bands"]
    assert (low["type"], low["freq"], low["gain"], low["q"]) == ("bell", 160.0, -5.2, 0.14)
    assert (b1["freq"], b1["gain"], b1["q"]) == (800.0, -14.1, 0.25)
    assert (high["type"], high["freq"], high["gain"]) == ("high_shelf", 10000.0, 4.6)
    assert d.resolve([0, 19, 1], [14, 0, 0])[0] == "InputChannel:14/Fader/On"
    # Addresses as the TF5 sends them (parameters first, then collections, at each level).
    assert d.resolve([0, 7, 1, 3, 2], [0, 0, 0, 0, 0])[0] == "InputChannel:0/PEQ/Bank/BandL/Gain"
    assert d.resolve([0, 7, 0], [3, 0, 0])[0] == "InputChannel:3/PEQ/On"
    for push in json.loads(PUSHES.read_text())["pushes"]:
        name, _, prm = d.resolve(push["path"], push["idx"])
        assert (prm.type == 1) == (push["type"] == T_S32), name
    for x in range(40):
        assert channel_eq(d, "InCh", x) is not None
    assert channel_eq(d, "St", 0)["hpf"] is None and len(channel_eq(d, "St", 1)["bands"]) == 4
    assert np.all(np.isfinite(eq_curve(eq)))


# ====================================================================== EQ settings

def test_channel_eq_input_channel(mixing):
    eq = channel_eq(mixing, "InCh", 0)
    assert eq == {
        "on": True, "bank": "BOLD", "one_knob": False,
        "hpf": {"on": True, "freq": 200.0, "slope": 12},
        "bands": [
            {"label": "L", "type": "bell", "on": True, "freq": 160.0, "gain": -5.2, "q": 0.14},
            {"label": "1", "type": "bell", "on": True, "freq": 800.0, "gain": -14.1, "q": 0.25},
            {"label": "2", "type": "bell", "on": True, "freq": 3350.0, "gain": -5.7, "q": 0.35},
            {"label": "H", "type": "high_shelf", "on": True, "freq": 10000.0, "gain": 4.6, "q": 0.7},
        ]}
    eq2 = channel_eq(mixing, "InCh", 1)
    assert not eq2["on"] and eq2["one_knob"] and eq2["bands"][1]["gain"] == 6.0
    assert channel_eq(mixing, "InCh", 2)["bands"][0]["type"] == "low_shelf"


def test_channel_eq_stereo_bus(mixing):
    st_l, st_r = channel_eq(mixing, "St", 0), channel_eq(mixing, "St", 1)
    assert st_l["hpf"] is None and [b["label"] for b in st_l["bands"]] == ["L", "1", "2", "H"]
    assert st_l["bands"][0]["type"] == "bell"
    assert st_r["bands"][0] == {"label": "L", "type": "hpf", "on": True, "freq": 80.0, "gain": 0.0, "q": 0.7}
    assert st_r["bands"][2]["gain"] == -3.0


@pytest.mark.parametrize("kind, x", [("InCh", 40), ("InCh", -1), ("St", 2), ("Mix", 0), ("FxRtn", 0)])
def test_channel_eq_missing(mixing, kind, x):
    assert channel_eq(mixing, kind, x) is None


# As TF Editor's band table builder (FUN_0051a080) maps them: type 2 is the filter, and
# it is only on while the band's HPFOn / LPFOn switch is set.
@pytest.mark.parametrize("band, values, expect, on", [
    ("BandL", {"Type": 0}, "bell", True), ("BandL", {"Type": 1}, "low_shelf", True),
    ("BandH", {"Type": 0}, "bell", True), ("BandH", {"Type": 1}, "high_shelf", True),
    ("BandH", {"Type": 2, "LPFOn": 1}, "lpf", True), ("BandH", {"Type": 2, "LPFOn": 0}, "lpf", False),
    ("BandH", {"Type": 1, "LPFOn": 1}, "high_shelf", True), ("BandH", {"Type": 0, "LPFOn": 1}, "bell", True),
    ("Band:0", {"Type": 1}, "bell", True), ("Band:1", {"Type": 2}, "bell", True),
])
def test_band_types(band, values, expect, on):
    v = default_mixing_values()
    v.update({f"InputChannel:4/PEQ/Bank/{band}/{k}": val for k, val in values.items()})
    eq = channel_eq(Dump(build_dump(mixing_tree(), v)), "InCh", 4)
    label = {"BandL": "L", "BandH": "H", "Band:0": "1", "Band:1": "2"}[band]
    b = next(b for b in eq["bands"] if b["label"] == label)
    assert (b["type"], b["on"]) == (expect, on)


@pytest.mark.parametrize("values, expect, on", [
    ({"Type": 2, "HPFOn": 1}, "hpf", True), ({"Type": 2, "HPFOn": 0}, "hpf", False),
    ({"Type": 0, "HPFOn": 1}, "bell", True), ({"Type": 1}, "low_shelf", True),
])
def test_stereo_low_band_types(values, expect, on):
    v = default_mixing_values()
    v.update({f"Stereo:0/PEQ/Bank/BandL/{k}": val for k, val in values.items()})
    b = channel_eq(Dump(build_dump(mixing_tree(), v)), "St", 0)["bands"][0]
    assert (b["type"], b["on"]) == (expect, on)


def test_channel_eq_without_mid_bands_or_one_knob():
    tree = C("Mixing", 1, [], [C("Stereo", 2, [], [C("PEQ", 1, [("On", 2, 1, 1)], [C("Bank", 1, [("Type", 0, 8, 1)], [
        C("BandL", 1, [("Type", 2, 1, 1), ("Frequency", 2, 4, 1), ("Gain", 1, 2, 1), ("Q", 2, 2, 1)]),
        C("BandH", 1, [("Type", 2, 1, 1), ("Frequency", 2, 4, 1), ("Gain", 1, 2, 1), ("Q", 2, 2, 1)])])])])])
    eq = channel_eq(Dump(build_dump(tree, {"Stereo:0/PEQ/On": 1, "Stereo:0/PEQ/Bank/BandL/Q": 700})), "St", 0)
    assert [b["label"] for b in eq["bands"]] == ["L", "H"] and not eq["one_knob"] and eq["hpf"] is None


# ====================================================================== EQ curve (TF Editor's maths)

KINDS = ["bell", "low_shelf", "high_shelf", "lpf", "hpf", "lpf1", "hpf1"]


def _eq(*bands, on=True, hpf=None):
    return {"on": on, "bank": "BOLD", "one_knob": False, "hpf": hpf,
            "bands": [dict(label=str(i), **{"on": True, **b}) for i, b in enumerate(bands)]}


def _at(curve_db, freqs, f):
    return float(np.interp(np.log(f), np.log(freqs), curve_db))


def _ref_sections(eq):
    """The 6-section table TF Editor builds for this EQ (FUN_0051a080), for the reference."""
    out = []
    if eq["hpf"]:
        out.append(("hpf1" if eq["hpf"]["slope"] == 6 else "hpf", eq["hpf"]["freq"], 1.0 if eq["hpf"]["on"] else 0.0,
                    0.707106))
    for b in eq["bands"]:
        if b["type"] in ("hpf", "lpf"):
            out.append((b["type"], b["freq"], 1.0 if b["on"] else 0.0, 0.707106))
        else:
            out.append((b["type"], b["freq"], b["gain"], b["q"]))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_section_matches_tf_editors_designer(seed):
    """Every section type against a line-by-line transcription of the decompiled code."""
    rng = np.random.default_rng(seed)
    for kind in KINDS:
        f0, q = float(10 ** rng.uniform(1.2, 4.3)), float(10 ** rng.uniform(-1.2, 1.3))
        g = float(rng.uniform(-18, 18))
        filt = kind in ("lpf", "hpf", "lpf1", "hpf1")
        mine = section_db(kind, f0, 0.0 if filt else g, q)
        theirs = [ref.response_db(ref.design(48000, q, 1.0 if filt else g, f0, ref.TYPE[kind]), f, 48000)
                  for f in CURVE_FREQS]
        assert np.allclose(mine, theirs, atol=1e-6), kind


def test_curve_matches_tf_editor_for_board_like_settings(mixing):
    for x in (0, 1, 2, 3):
        eq = channel_eq(mixing, "InCh", x)
        expect = ref.curve(_ref_sections(eq), CURVE_FREQS) if eq["on"] else [0.0] * len(CURVE_FREQS)
        assert np.allclose(eq_curve(eq), expect, atol=1e-6)
    for x in (0, 1):
        eq = channel_eq(mixing, "St", x)
        assert np.allclose(eq_curve(eq), ref.curve(_ref_sections(eq), CURVE_FREQS), atol=1e-6)


@pytest.mark.skipif(not REAL_DUMP.exists(), reason="captured Mixing dump not available")
def test_curve_matches_tf_editor_for_every_captured_channel():
    d = Dump(REAL_DUMP.read_bytes())
    for kind, n in (("InCh", 40), ("St", 2)):
        for x in range(n):
            eq = channel_eq(d, kind, x)
            expect = ref.curve(_ref_sections(eq), CURVE_FREQS) if eq["on"] else [0.0] * len(CURVE_FREQS)
            assert np.allclose(eq_curve(eq), expect, atol=1e-6), (kind, x)


def test_curve_flat_when_off_missing_or_zero_gain():
    freqs = CURVE_FREQS
    loud = _eq({"type": "bell", "freq": 1000, "gain": 12, "q": 1}, hpf={"on": True, "freq": 100, "slope": 12})
    assert np.all(eq_curve({**loud, "on": False}) == 0)
    assert np.all(eq_curve(None) == 0)
    flat = _eq(*({"type": t, "freq": 1000, "gain": 0, "q": 0.7} for t in ("bell", "low_shelf", "high_shelf")))
    assert np.all(eq_curve(flat) == 0)
    assert np.all(eq_curve(_eq(hpf={"on": False, "freq": 500, "slope": 12})) == 0)
    assert np.all(eq_curve(_eq({"type": "lpf", "on": False, "freq": 500, "gain": 0, "q": 0.7})) == 0)
    assert len(eq_curve(loud)) == len(freqs) == 241 and freqs[0] == 20 and freqs[-1] == 20000


@pytest.mark.parametrize("gain", [-18, -6, 3, 15])
@pytest.mark.parametrize("q", [0.1, 0.7, 4, 16])
def test_bell_hits_its_gain_at_its_frequency(gain, q):
    c = section_db("bell", 1000, gain, q, np.array([1000.0, 500.0, 2000.0, 20.0]))
    assert c[0] == pytest.approx(gain, abs=1e-6)            # bilinear with pre-warping: exact at f0
    assert abs(c[1]) < abs(gain) and abs(c[2]) < abs(gain)
    assert abs(c[1] - c[2]) < 0.5                            # ~symmetric an octave either side
    if q >= 0.7:
        assert abs(c[3]) < 0.5


@pytest.mark.parametrize("gain", [-12, -3, 6, 15])
def test_bell_cut_mirrors_boost(gain):
    f = np.geomspace(20, 20000, 60)
    assert np.allclose(section_db("bell", 900, gain, 0.8, f), -section_db("bell", 900, -gain, 0.8, f))


def test_bell_q_sets_width():
    f = np.array([1000.0, 2000.0])
    wide = section_db("bell", 1000, 12, 0.5, f)
    narrow = section_db("bell", 1000, 12, 8, f)
    assert wide[0] == pytest.approx(narrow[0]) and wide[1] > 6 > narrow[1]


@pytest.mark.parametrize("gain", [-12, 6])
def test_shelves_are_two_first_order_sections(gain):
    f = np.array([10.0, 1000.0, 23000.0])
    v = 10 ** (abs(gain) / 20)
    corner = 20 * np.log10((1 + v) / 2) * np.sign(gain)     # |(j + sqrt V) / (j + 1)|^2, twice
    low = section_db("low_shelf", 1000, gain, 0.7, f)
    high = section_db("high_shelf", 1000, gain, 0.7, f)
    assert low[0] == pytest.approx(gain, abs=0.05) and abs(low[2]) < 0.3
    assert high[2] == pytest.approx(gain, abs=0.3) and abs(high[0]) < 0.05
    assert low[1] == pytest.approx(corner, abs=1e-6) and high[1] == pytest.approx(corner, abs=1e-6)
    assert np.allclose(section_db("low_shelf", 1000, gain, 0.2, f), low)    # Q isn't used
    assert np.allclose(section_db("low_shelf", 1000, -gain, 0.7, f), -low)  # cut mirrors boost


def test_hpf_and_lpf_slopes():
    f = np.array([25.0, 50.0, 100.0, 200.0, 400.0, 10000.0])
    c = section_db("hpf", 100, 0, 0.707106, f)
    assert c[2] == pytest.approx(-3.01, abs=0.01)             # -3 dB at the corner
    assert c[0] - c[1] == pytest.approx(-12, abs=0.3)         # 12 dB/oct below it
    assert c[5] == pytest.approx(0, abs=0.01)
    assert np.allclose(eq_curve(_eq(hpf={"on": True, "freq": 100, "slope": 12}), f), c)
    band_hpf = eq_curve(_eq({"type": "hpf", "freq": 100, "gain": 9, "q": 3}), f)
    assert np.allclose(band_hpf, c)                           # band gain / Q don't matter for filters
    lpf = section_db("lpf", 100, 0, 0.707106, f)
    assert lpf[2] == pytest.approx(-3.01, abs=0.01) and lpf[3] - lpf[4] == pytest.approx(12, abs=0.3)
    f1 = np.array([5.0, 10.0, 1000.0, 2000.0, 4000.0])   # first order: asymptotic further from the corner
    first = eq_curve(_eq(hpf={"on": True, "freq": 1000, "slope": 6}), f1)
    assert first[2] == pytest.approx(-3.01, abs=0.01) and first[1] - first[0] == pytest.approx(6, abs=0.1)
    lpf1 = section_db("lpf1", 100, 0, 0.7, np.array([100.0, 2000.0, 4000.0]))
    assert lpf1[0] == pytest.approx(-3.01, abs=0.01) and lpf1[1] - lpf1[2] == pytest.approx(6, abs=0.3)


def test_bands_add_up_in_db():
    f = np.geomspace(20, 20000, 50)
    a = {"type": "bell", "freq": 100, "gain": -6, "q": 2}
    b = {"type": "high_shelf", "freq": 8000, "gain": 4, "q": 0.7}
    h = {"on": True, "freq": 40, "slope": 12}
    assert np.allclose(eq_curve(_eq(a, b, hpf=h), f), eq_curve(_eq(a), f) + eq_curve(_eq(b), f)
                       + eq_curve(_eq(hpf=h), f))


def test_curve_of_the_captured_channel(mixing):
    c = eq_curve(channel_eq(mixing, "InCh", 0))
    assert np.all(np.isfinite(c))
    assert _at(c, CURVE_FREQS, 20000) > _at(c, CURVE_FREQS, 3350) + 6   # the high shelf lifts the top
    assert _at(c, CURVE_FREQS, 800) < -14.1                             # the cut, deepened by its neighbours
    assert _at(c, CURVE_FREQS, 50) < -20                                # HPF at 200 Hz


def test_parameters_are_clamped_like_tf_editor():
    f = np.array([100.0, 1000.0])
    assert np.allclose(section_db("bell", 1000, 6, 0.0, f), section_db("bell", 1000, 6, 0.1, f))   # Q >= 0.1
    assert np.allclose(section_db("bell", 5, 6, 1, f), section_db("bell", 16, 6, 1, f))           # f >= 16 Hz
    assert np.allclose(section_db("bell", 1000, 6, 1, f, fs=8000), section_db("bell", 1000, 6, 1, f, fs=44100))
    with pytest.raises(ValueError):
        biquad("notch", 1000, 0, 1)


# ====================================================================== client <-> fake console

EXPECTED_HANDSHAKE = [
    ("MPRO", SUBSCRIBE), ("MPRO", HASH_COMPARE), ("MPRO", SYNC_OPEN), ("MPRO", SYNC_CLOSE),
    ("EEVT", EVENT),
    ("MMIX", SUBSCRIBE), ("MMIX", HASH_COMPARE), ("MMIX", SYNC_OPEN), ("MMIX", SYNC_CLOSE),
]


def test_handshake_sync_and_eq(console, client_for):
    changes = []
    client = client_for(console, on_change=lambda: changes.append(1))
    wait_until(lambda: client.synced)
    assert console.sent_by_client()[:len(EXPECTED_HANDSHAKE)] == EXPECTED_HANDSHAKE
    assert console.events_from_client()[0] == (EVENT, "SetClientType", "TYPE:StageMix SYNCDIR:2")
    sub = console.received[0][1]
    assert sub.elements[0].value == "Property"
    assert console.received[5][1].elements[0].value == "Mixing"
    st = client.status()
    assert st["connected"] and st["synced"] and st["read_only"] and st["descriptor_known"]
    assert st["descriptor_md5"] == DESC_MD5["Mixing"] and st["last_error"] is None
    assert client.eq("InCh", 0)["bands"][1]["gain"] == -14.1
    assert client.eq("St", 1)["bands"][0]["type"] == "hpf"
    assert client.eq("InCh", 99) is None and client.eq("Mix", 0) is None
    assert client.get("InputChannel:1/Label/Name") == "Pastor" and client.get("Nope/Name") is None
    assert changes
    assert_read_only(console)


def test_nothing_before_connect():
    client = TFEditorClient("127.0.0.1", 1)
    assert client.eq("InCh", 0) is None and client.get("InputChannel:0/Label/Name") is None
    assert client.status()["read_only"] and not client.status()["connected"]


def _captured():
    return json.loads(PUSHES.read_text())["pushes"]


def test_captured_board_pushes_resolve(mixing):
    """Real addresses: each level indexes parameters first, then collections."""
    pushes = _captured()
    assert len(pushes) == 393
    for push in pushes:
        name, _, prm = mixing.resolve(push["path"], push["idx"])
        expect = "InputChannel:{}/".format(push["idx"][0]) + CAPTURED_NAMES[tuple(push["path"])].format(*push["idx"])
        assert name == expect
        assert (prm.type == 1) == (push["type"] == T_S32)          # signed values land on signed params
        assert push["origin"] == 0xA1
    gains = [p["value"] for p in pushes if p["path"] == [0, 7, 1, 3, 2]]
    assert min(gains) == -1800 and max(gains) == 1800             # a sweep over the full +-18 dB


def test_captured_board_pushes_apply_in_order(mixing):
    last = {}
    for push in _captured():
        name = mixing.apply(push["path"], push["idx"], push["value"])
        last[name] = push["value"]
    for name, value in last.items():
        assert mixing.get(name) == value
    eq = channel_eq(mixing, "InCh", 0)
    assert eq["bands"][0]["gain"] == last["InputChannel:0/PEQ/Bank/BandL/Gain"] / 100
    assert eq["bands"][3]["freq"] == last["InputChannel:0/PEQ/Bank/BandH/Frequency"] / 10


@pytest.mark.parametrize("path, idx", [
    ([0, 7, 0, 2, 2], [0, 0, 0, 0, 0]),   # the guide's old children-only address for BandL/Gain
    ([0, 7, 1, 3], [0, 0, 0, 0]),         # ends at a collection
    ([0, 7, 3], [0, 0, 0]),               # PEQ has 1 parameter + 2 collections
])
def test_resolve_rejects_children_only_addresses(mixing, path, idx):
    with pytest.raises(ProtocolError):
        mixing.resolve(path, idx)


def test_live_updates_replayed_from_the_board(console, client_for):
    """The production capture, replayed by the fake console: every push is applied."""
    changes = []
    client = client_for(console, on_change=lambda: changes.append(1))
    wait_until(lambda: client.synced)
    pushes = _captured()
    for push in pushes:
        console.push("MMIX", set_param("Mixing", push["path"], push["idx"], push["value"], vtype=push["type"]))
    wait_until(lambda: client.changes == len(pushes))
    final = {}
    for push in pushes:
        final[(tuple(push["path"]), tuple(push["idx"]))] = push["value"]
    eq = client.eq("InCh", 0)
    assert eq["bands"][1]["gain"] == final[((0, 7, 1, 4, 2), (0, 0, 0, 0, 0))] / 100
    assert eq["bands"][2]["gain"] == final[((0, 7, 1, 4, 2), (0, 0, 0, 1, 0))] / 100
    assert eq["on"] == bool(final[((0, 7, 0), (0, 0, 0))])
    assert eq["hpf"]["freq"] == final[((0, 7, 1, 2, 1), (0, 0, 0, 0, 0))] / 10
    assert_read_only(console)


def test_live_changes_from_the_desk(console, client_for):
    changes = []
    client = client_for(console, on_change=lambda: changes.append(1))
    wait_until(lambda: client.synced)
    n = len(changes)
    console.push_change("InputChannel:0/PEQ/Bank/Band:0/Gain", -300)
    wait_until(lambda: client.eq("InCh", 0)["bands"][1]["gain"] == -3.0)
    console.push_change("InputChannel:0/PEQ/Bank/BandH/Frequency", 63000)
    wait_until(lambda: client.eq("InCh", 0)["bands"][3]["freq"] == 6300.0)
    console.push_change("InputChannel:0/PEQ/On", 0)
    wait_until(lambda: not client.eq("InCh", 0)["on"])
    console.push_change("InputChannel:0/Label/Name", "Choir")
    wait_until(lambda: client.get("InputChannel:0/Label/Name") == "Choir")
    console.push_change("Stereo:0/PEQ/Bank/Band:1/Gain", 250, origin=0xA0)         # origin doesn't matter to us
    wait_until(lambda: client.eq("St", 0)["bands"][2]["gain"] == 2.5)
    console.push_change("InputChannel:3/PEQ/Bank/BandL/Gain", 120, vtype=T_S16)     # narrow types decode too
    wait_until(lambda: client.eq("InCh", 3)["bands"][0]["gain"] == 1.2)
    console.push_change("InputChannel:3/PEQ/Bank/BandL/Type", 1, vtype=T_U8)
    wait_until(lambda: client.eq("InCh", 3)["bands"][0]["type"] == "low_shelf")
    wait_until(lambda: client.changes == 7 and len(changes) >= n + 7)
    assert_read_only(console)


def test_bad_pushes_are_ignored(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    before = client.eq("InCh", 0)
    console.push("MMIX", set_param("Mixing", [0, 99, 0], [0, 0, 0], 5))                  # no such parameter
    console.push("MMIX", set_param("Mixing", [0, 0, 0], [45, 0, 0], 5))                  # channel 46
    console.push("MMIX", set_param("Processing", [0, 0, 0], [0, 0, 0], 5))               # other function
    gain = wire_address(console.mixing, "InputChannel:0/PEQ/Bank/BandL/Gain")
    console.push("MMIX", set_param("Mixing", *gain, "text"))                             # wrong type
    console.push("MMIX", set_param("Mixing", *gain, 70000))                              # doesn't fit int16
    console.push("MMIX", message(SET_PARAM, [element(T_U8, b"\0")]))                    # truncated
    console.push("MMIX", message((0x01, 0x05, 0x01)))                                    # unknown command
    console.push("MPRO", set_param("Property", [0], [0], 1))                             # Property: ignored
    console.push_change("InputChannel:9/PEQ/Bank/Band:0/Gain", -100)                     # a good one last
    wait_until(lambda: client.eq("InCh", 9)["bands"][1]["gain"] == -1.0)
    assert client.eq("InCh", 0) == before and client.connected and client.changes == 1
    # The four Mixing pushes that didn't fit are counted and shown, not silently dropped.
    st = client.status()
    assert st["rejected"] == 4 and "doesn't fit" in st["last_rejected"]
    assert st["received"]["MMIX 01 11 01"] >= 6 and st["received"]["MMIX 01 05 01"] == 1
    assert_read_only(console)


def test_events_are_answered_like_tf_editor(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    n = len(console.raw)
    console.push_event("KeepAlive")
    console.push_event("NotifySyncStatus", "SCN0INI:23 SCN0REQ:0 LIB0INI:142")
    console.push_event("RecallScene", "scene_a 3")          # not ours to acknowledge
    console.push_event("SendWarningMessage", "x")
    wait_until(lambda: (EVENT_REPLY, "NotifySyncStatus", "SCN0INI:23 SCN0REQ:0 LIB0INI:142")
               in console.events_from_client())
    replies = [e for e in console.events_from_client() if e[0] == EVENT_REPLY]
    assert (EVENT_REPLY, "KeepAlive", "") in replies
    assert not any(name in ("RecallScene", "SendWarningMessage") for _, name, _ in console.events_from_client())
    assert len(console.raw) - n == 2
    assert_read_only(console)


def test_client_sends_keepalives(console, client_for, monkeypatch):
    monkeypatch.setattr(tfe, "KEEPALIVE_S", 0.2)
    client = client_for(console)
    wait_until(lambda: sum(1 for e in console.events_from_client() if e == (EVENT, "KeepAlive", "")) >= 3)
    assert client.connected
    assert_read_only(console)


def test_scene_recall_dump_replaces_the_data(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    closes = console.sent_by_client().count(("MMIX", SYNC_CLOSE))
    console.push_dump({"InputChannel:0/PEQ/Bank/Band:0/Gain": 900, "InputChannel:0/Label/Name": "Recalled"})
    wait_until(lambda: client.eq("InCh", 0)["bands"][1]["gain"] == 9.0)
    assert client.get("InputChannel:0/Label/Name") == "Recalled"
    wait_until(lambda: console.sent_by_client().count(("MMIX", SYNC_CLOSE)) == closes + 1)
    assert_read_only(console)


def test_falls_back_without_on_set_client_type(client_for, monkeypatch):
    monkeypatch.setattr(tfe, "CLIENT_TYPE_WAIT_S", 0.3)
    fake = FakeEditorConsole(send_on_set_client_type=False)
    try:
        client = client_for(fake)
        wait_until(lambda: client.synced)
        assert fake.sent_by_client()[:len(EXPECTED_HANDSHAKE)] == EXPECTED_HANDSHAKE
        assert_read_only(fake)
    finally:
        fake.close()


def test_no_mixing_dump_times_out_and_retries(client_for, monkeypatch):
    monkeypatch.setattr(tfe, "SYNC_TIMEOUT_S", 0.5)
    fake = FakeEditorConsole(send_mixing_dump=False)
    try:
        client = client_for(fake)
        wait_until(lambda: client.connects >= 2, timeout=8)
        assert "no Mixing dump" in (client.last_error or "") and not client.synced
        assert client.eq("InCh", 0) is None
        assert_read_only(fake)
    finally:
        fake.close()


def test_unknown_firmware_descriptor_is_reported(client_for):
    fake = FakeEditorConsole(mixing_md5="00112233445566778899aabbccddeeff")
    try:
        client = client_for(fake)
        wait_until(lambda: client.synced)
        st = client.status()
        assert st["descriptor_md5"] == "00112233445566778899aabbccddeeff" and not st["descriptor_known"]
        assert client.eq("InCh", 0) is not None   # read by name, not by position
    finally:
        fake.close()


def test_reconnects_after_the_console_drops(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    console.drop_connections()
    wait_until(lambda: client.connects >= 2 and client.synced, timeout=10)
    assert console.sent_by_client().count(("MMIX", SUBSCRIBE)) == 2
    assert_read_only(console)


def test_garbage_from_the_console_reconnects(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    console.push_raw(b"\x00\xff\x00\xff" + bytes(8))
    wait_until(lambda: client.last_error is not None and "tag" in client.last_error)
    wait_until(lambda: client.connects >= 2 and client.synced, timeout=10)


def test_unreachable_console_is_reported():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    client = TFEditorClient("127.0.0.1", port)
    client.start()
    try:
        wait_until(lambda: client.last_error is not None)
        assert not client.connected and not client.synced
    finally:
        client.stop()


def test_stop_is_prompt(console, client_for):
    client = client_for(console)
    wait_until(lambda: client.synced)
    client.stop()
    assert not client.connected and client._thread is None


def test_a_bug_that_tries_to_write_stops_the_client(console, client_for, monkeypatch, caplog):
    # Simulate a bug: the client tries to claim TYPE:Editor with SYNCDIR:1. The guard
    # refuses, nothing reaches the console, and the client stops for good.
    monkeypatch.setattr(tfe, "CLIENT_TYPE", "TYPE:Editor SYNCDIR:1")
    with caplog.at_level(logging.ERROR, logger="tf_daemon.tfeditor"):
        client = client_for(console)
        wait_until(lambda: (client.last_error or "").startswith("stopped"))
    assert not client.connected and not client.synced
    assert not any(name == "SetClientType" for _, name, _ in console.events_from_client())
    assert not any(t == "MMIX" for t, _ in console.sent_by_client())
    assert "refused" in caplog.text
    n = client.connects
    import time
    time.sleep(0.5)
    assert client.connects == n                    # no retry


def test_password_never_leaks(console, client_for, caplog):
    with caplog.at_level(logging.DEBUG):
        client = client_for(console)
        wait_until(lambda: client.synced)
        console.push_change("InputChannel:0/PEQ/On", 0)
        wait_until(lambda: client.changes == 1)
        client.stop()
    assert PASSWORD.encode() in console.property_blob          # it really was sent to the client
    assert PASSWORD not in caplog.text
    assert PASSWORD not in repr(client.status()) and PASSWORD not in repr(vars(client))


# ====================================================================== probe CLI

def test_cli_prints_eq(console, capsys):
    rc = tfe.main(["127.0.0.1", "--port", str(console.port), "--channels", "1-2", "--timeout", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "InCh  1 'GRA'" in out and "1:bell 800 Hz -14.1 dB" in out and "HPF on 200 Hz" in out
    assert "InCh  2 'Pastor': OFF" in out and "1-knob" in out
    assert "sent:" in out and "11 01" not in out.split("sent:")[1]
    assert_read_only(console)


def test_cli_reports_failure(capsys):
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    assert tfe.main(["127.0.0.1", "--port", str(port), "--timeout", "0.5"]) == 1
    assert "not synchronized" in capsys.readouterr().out


def test_parse_channels():
    assert tfe._parse_channels("1-3,7") == [1, 2, 3, 7]
    assert tfe._parse_channels("5") == [5]


# ====================================================================== config, module, routes

def test_eq_config(tmp_path):
    t = load_config(None)["tf5"]
    assert t["eq"] is False and t["eq_port"] == 50368
    p = tmp_path / "c.toml"
    for bad in ('[tf5]\neq = "yes"\n', '[tf5]\neq = 1\n', '[tf5]\neq_port = 0\n', '[tf5]\neq_port = 70000\n'):
        p.write_text(bad)
        with pytest.raises(ConfigError, match="tf5.eq"):
            load_config(p)
    p.write_text('[tf5]\nhost = "10.0.0.5"\neq = true\neq_port = 50369\n')
    t = load_config(p)["tf5"]
    assert t["eq"] is True and t["eq_port"] == 50369


def test_shipped_config_keeps_eq_off():
    cfg = load_config(Path(__file__).parent.parent / "config.toml")
    assert cfg["tf5"]["eq"] is False


def _closed_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def _app(eq, eq_port=None, usb_patch=None):
    cfg = load_config(None)
    cfg["tf5"].update(host="127.0.0.1", port=_closed_port(), eq=eq)   # no RCP console: EQ is independent
    if eq_port:
        cfg["tf5"]["eq_port"] = eq_port
    if usb_patch is not None:
        cfg["tf5"]["usb_patch"] = usb_patch
    modules = build_modules(cfg, "synth", rf_source="off", qlxd_source="off")
    return create_app(cfg, modules), modules


def test_eq_route_is_404_when_off():
    app, modules = _app(eq=False)
    try:
        c = app.test_client()
        assert c.get("/api/tf5/eq/0").status_code == 404
        assert c.get("/api/tf5/status").get_json()["eq"] is None
        assert modules[1].eq_client is None
    finally:
        app.extensions["tf_stop"]()


def test_eq_route(console):
    app, _ = _app(eq=True, eq_port=console.port, usb_patch=["InCh 1", "InCh 2", "", "St R", "InCh 40"])
    try:
        c = app.test_client()
        wait_until(lambda: c.get("/api/tf5/eq/0").get_json()["synced"])
        d = c.get("/api/tf5/eq/0").get_json()
        assert d["index"] == 0 and d["source"] == "InCh 1" and d["connected"]
        assert d["eq"]["bands"][1] == {"label": "1", "type": "bell", "on": True, "freq": 800.0, "gain": -14.1,
                                         "q": 0.25}
        assert len(d["freqs"]) == len(d["curve"]) == 241
        assert d["freqs"][0] == 20.0 and d["freqs"][-1] == 20000.0
        assert min(d["curve"]) < -20 and all(isinstance(v, float) for v in d["curve"])
        off = c.get("/api/tf5/eq/1").get_json()
        assert off["eq"]["on"] is False and set(off["curve"]) == {0.0}
        assert c.get("/api/tf5/eq/2").get_json() == {"index": 2, "source": None, "connected": True, "synced": True,
                                                     "eq": None, "freqs": [], "curve": []}
        st = c.get("/api/tf5/eq/3").get_json()
        assert st["source"] == "St R" and st["eq"]["bands"][0]["type"] == "hpf"
        assert c.get("/api/tf5/eq/4").get_json()["eq"]["bands"][1]["freq"] == 1000.0
        assert c.get("/api/tf5/eq/99").get_json()["source"] is None
        status = c.get("/api/tf5/status").get_json()
        assert status["eq"]["synced"] and status["eq"]["read_only"]
        assert c.get("/api/status").get_json()["modules"]["tf5"]["eq"]["connected"]
        console.push_change("InputChannel:0/PEQ/Bank/Band:0/Gain", 0)
        wait_until(lambda: c.get("/api/tf5/eq/0").get_json()["eq"]["bands"][1]["gain"] == 0.0)
        assert PASSWORD not in c.get("/api/tf5/status").get_data(as_text=True)
    finally:
        app.extensions["tf_stop"]()
    assert_read_only(console)


def test_page_has_the_eq_toggle():
    static = Path(tfe.__file__).parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="eq-toggle" hidden' in html and 'id="show-eq"' in html
    # The EQ is pushed over /ws/audio ("tf5-eq" messages) and drawn.
    assert '"tf5-eq"' in js and "drawEq()" in js
    # The page only ever reads the EQ.
    assert not re.search(r"fetch\(`/api/tf5/eq[^`]*`\s*,", js)
    assert "--eq:" in (static / "style.css").read_text(encoding="utf-8")
    draw = re.search(r"function drawEq\(\) \{(.*?)\n  \}\n", js, re.S).group(1)
    # Same dB scale as the spectrum (the View range; shared with the vocal assist
    # through eqHalf / eqY), and the handles sit on the curve.
    assert re.search(r"const eqHalf = \(\) => \(state\.dbMax - state\.dbMin\) / 2", js)
    assert "eqHalf()" in draw and "eqY" in draw
    assert "yEq(eqAt(f))" in draw
    # The console sidebar has its own EQ graph, redrawn on every pushed change.
    side = re.search(r"function drawPanelEq\(\) \{(.*?)\n  \}\n", js, re.S).group(1)
    assert "eqAt(f)" in side and "eqHalf()" in side and "peqRange" not in js   # the spectrum's scale
    set_range = re.search(r"function setRange\(min, max\) \{(.*?)\n  \}\n", js, re.S).group(1)
    assert "drawPanelEq()" in set_range
    on_eq = re.search(r"function onConsoleEq\(msg\) \{(.*?)\n  \}\n", js, re.S).group(1)
    assert "drawPanelEq()" in on_eq
    css = (static / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.cp-eq \{[^}]*position: static", css)   # not the full-page canvas rule
