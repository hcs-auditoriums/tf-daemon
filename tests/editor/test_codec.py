"""The TF Editor protocol's wire format (console/editor/codec.py): elements, messages, frames."""

import struct

import pytest

from tf_daemon.console.editor.codec import (
    DESC_MD5,
    MAX_FRAME,
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
    Element,
    FrameReader,
    ProtocolError,
    element,
    event,
    frame,
    hash_compare,
    message,
    parse_elements,
    parse_message,
    subscribe,
    sync,
)


@pytest.mark.parametrize(
    "t, value",
    [
        (T_U16, [0, 1, 65535]),
        (T_U32, [0, 0xA0, 0xFFFFFFFF]),
        (T_S8, [-128, 127]),
        (T_S16, [-32768, 5]),
        (T_S32, [-520, 2**31 - 1]),
        (T_U32, []),
        (T_U8, b"\x00\x80\xff"),
        (T_U8, b""),
        (T_STR, "Mixing"),
        (T_STR, ""),
        (T_STR, "Lead Vox ü"),
    ],
)
def test_element_roundtrip(t, value):
    raw = element(t, value)
    assert parse_elements(raw, 1) == [Element(t, value)]


def test_element_wire_format_is_big_endian_with_count():
    assert element(T_U16, [5]) == bytes.fromhex("12 00000001 0005")
    assert element(T_S32, [-520]) == bytes.fromhex("24 00000001 FFFFFDF8")
    assert element(T_STR, "Mixing") == bytes.fromhex("31 00000007") + b"Mixing\0"  # count includes the NUL
    assert element(T_U32, [0, 7, 0, 2, 2]) == bytes.fromhex(
        "14 00000005" + "00000000 00000007 0000000000000002 00000002".replace(" ", "")
    )
    with pytest.raises(ValueError):
        element(0x44, [1])


@pytest.mark.parametrize(
    "raw",
    [
        b"\x14\x00\x00",  # header cut short
        bytes.fromhex("14 00000002 00000001"),  # data cut short
        bytes.fromhex("31 00000010") + b"abc",  # string cut short
        bytes.fromhex("44 00000001 00"),  # unknown type
    ],
)
def test_parse_elements_rejects_truncated_or_unknown(raw):
    with pytest.raises(ProtocolError):
        parse_elements(raw, 1)


def test_message_roundtrip_and_layout():
    payload = message(SYNC_OPEN)
    assert payload == bytes.fromhex("11 00000004 010401 00")  # 9 bytes, as on the wire
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
    assert len(event("SetClientType", "TYPE:Editor SYNCDIR:2")) == 73  # we send StageMix: 2 bytes more
    hc = parse_message(hash_compare("Mixing")).elements
    assert hc[2].value.hex() == DESC_MD5["Mixing"] and hc[3].value == bytes(16)  # zero data hash -> full dump


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


@pytest.mark.parametrize(
    "data", [b"\x00\x01\x02\x03" + bytes(4), b"MM X" + bytes(4), b"MMIX" + struct.pack(">I", MAX_FRAME + 1)]
)
def test_frame_reader_rejects_garbage(data):
    with pytest.raises(ProtocolError):
        FrameReader().feed(data)


def test_frame_rejects_bad_tags():
    for tag in ("", "TOOLONG"):
        with pytest.raises(ValueError):
            frame(tag, b"")
