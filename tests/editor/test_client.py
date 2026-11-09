"""TFEditorClient against the fake console (console/editor/client.py), and the probe CLI."""

import json
import logging
import socket

import pytest
from editor_fakes import (
    CAPTURED_NAMES,
    PASSWORD,
    PUSHES,
    FakeEditorConsole,
    assert_read_only,
    set_param,
    wait_until,
    wire_address,
)

import tf_daemon.console.editor.__main__ as editor_cli
import tf_daemon.console.editor.client as editor_client
from tf_daemon.console.editor.client import TFEditorClient
from tf_daemon.console.editor.codec import (
    DESC_MD5,
    EVENT,
    EVENT_REPLY,
    HASH_COMPARE,
    SET_PARAM,
    SUBSCRIBE,
    SYNC_CLOSE,
    SYNC_OPEN,
    T_S16,
    T_S32,
    T_U8,
    ProtocolError,
    element,
    message,
)
from tf_daemon.console.editor.dump import channel_eq

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
    monkeypatch.setattr(editor_client, "KEEPALIVE_S", 0.2)
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
    monkeypatch.setattr(editor_client, "CLIENT_TYPE_WAIT_S", 0.3)
    fake = FakeEditorConsole(send_on_set_client_type=False)
    try:
        client = client_for(fake)
        wait_until(lambda: client.synced)
        assert fake.sent_by_client()[:len(EXPECTED_HANDSHAKE)] == EXPECTED_HANDSHAKE
        assert_read_only(fake)
    finally:
        fake.close()


def test_no_mixing_dump_times_out_and_retries(client_for, monkeypatch):
    monkeypatch.setattr(editor_client, "SYNC_TIMEOUT_S", 0.5)
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
    monkeypatch.setattr(editor_client, "CLIENT_TYPE", "TYPE:Editor SYNCDIR:1")
    with caplog.at_level(logging.ERROR, logger="tf_daemon.console.editor.client"):
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
    rc = editor_cli.main(["127.0.0.1", "--port", str(console.port), "--channels", "1-2", "--timeout", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "InCh  1 'GRA'" in out and "1:bell 800 Hz -14.1 dB" in out and "HPF on 200 Hz" in out
    assert "InCh  2 'Pastor': OFF" in out and "1-knob" in out
    assert "sent:" in out and "11 01" not in out.split("sent:")[1]
    assert_read_only(console)


def test_cli_reports_failure(capsys):
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    assert editor_cli.main(["127.0.0.1", "--port", str(port), "--timeout", "0.5"]) == 1
    assert "not synchronized" in capsys.readouterr().out


def test_parse_channels():
    assert editor_cli._parse_channels("1-3,7") == [1, 2, 3, 7]
    assert editor_cli._parse_channels("5") == [5]
