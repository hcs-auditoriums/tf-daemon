"""Every function of the V4.55 tree through the client and the simulated desk: subscribe, dump,
pushes, writes per family and scene recall (console/editor/client.py, console/sim_editor.py)."""

import pytest
from editor_fakes import MOBILE_PASSWORD, PASSWORD, SimEditor, assert_read_only, wait_until

import tf_daemon.console.editor.client as editor_client
from tf_daemon.console.editor.codec import EVENT, SET_PARAM, SUBSCRIBE, parse_message
from tf_daemon.console.editor.guard import WriteNotAllowed, WriteRefused
from tf_daemon.console.editor.tree import FUNCTIONS, TAGS


@pytest.fixture
def synced(console, client_for):
    """A client subscribed to every function, once all of them have arrived."""

    def make(**kw):
        c = client_for(console, functions=FUNCTIONS, **kw)
        wait_until(lambda: c.synced and all(s["synced"] for s in c.status()["functions"].values()))
        return c

    return make


def _sets(fake):
    return [(t, parse_message(p)) for t, p in fake.raw if parse_message(p).cmd == SET_PARAM]


def test_every_function_is_subscribed_after_mixing(console, synced):
    c = synced()
    subscribed = [t for t, cmd in console.sent_by_client() if cmd == SUBSCRIBE]
    assert subscribed == [TAGS[f] for f in FUNCTIONS]  # Property, Mixing, then one at a time
    st = c.status()
    assert set(st["functions"]) == set(FUNCTIONS) - {"Property"}
    assert all(s["descriptor_known"] and not s["skipped"] for s in st["functions"].values())
    assert st["read_only"] and st["write_families"] == [] and not st["scene_recall"]
    assert c.get("BuiltIn/InputPort:0/HeadAmp/Gain", "Processing") == 4500
    assert c.get("Slot/InputPort:63/HeadAmp/Gain", "Processing") == -600  # the schema default
    assert c.get("MuteMaster:1/Name", "Setup") == "FX" and c.get("Scene/LastScene", "Setup") == "A00"
    assert c.get("FX:1/Param/PluginName", "FX") == "MonoDelay"
    assert c.get("InputChannel:0/DirectOut/Point") == 20 and c.get("DCA:3/Label/Name") == "DCA 4"
    assert c.get("Bank:0/Info:1/Title", "SceneStatus") == "Sunday"
    assert c.get("InputChannel:0/Label/Name", "Processing") is None  # wrong function
    # Secrets: Property is never kept, and no password is returned from any function.
    assert c.get("UserAccount/Unit/Password", "Property") is None
    assert c.get("CommSession/MobileMix/Password", "Setup") is None
    assert MOBILE_PASSWORD not in repr(st) and PASSWORD not in repr(st)
    assert_read_only(console)


def test_by_default_only_property_and_mixing(console, client_for):
    c = client_for(console)
    wait_until(lambda: c.synced)
    assert c.functions == ("Property", "Mixing") and set(c.status()["functions"]) == {"Mixing"}
    assert {t for t, _ in console.sent_by_client()} <= {"MPRO", "MMIX", "EEVT"}
    assert c.get("MuteMaster:0/Name", "Setup") is None


def test_pushes_in_every_function(console, synced):
    c = synced()
    before = c.status()["functions"]["Volatile"]["version"]
    console.push_change("Cue/InputChannel:14/On", 1, func="Volatile")
    console.push_change("BuiltIn/InputPort:2/HeadAmp/Gain", 5000, func="Processing")
    console.push_change("Scene/LastScene", "B07", func="Setup")
    wait_until(lambda: c.get("Scene/LastScene", "Setup") == "B07")
    assert c.get("Cue/InputChannel:14/On", "Volatile") == 1
    assert c.get("BuiltIn/InputPort:2/HeadAmp/Gain", "Processing") == 5000
    assert c.status()["functions"]["Volatile"]["version"] == before + 1
    assert c.changes == 0  # counts Mixing pushes (the EQ) only
    console.push_dump({"MuteMaster:0/Name": "SPEECH"}, func="Setup")  # a whole new Setup dump
    wait_until(lambda: c.get("MuteMaster:0/Name", "Setup") == "SPEECH")
    assert c.get("Scene/LastScene", "Setup") == "A00"
    assert_read_only(console)


WRITES = [
    ("mix", "Mixing", "DCA:0/Fader/Level", -1000),
    ("mix", "Volatile", "Cue/InputChannel:3/On", 1),
    ("mix", "Setup", "MuteMaster:0/On", 1),
    ("processing", "Mixing", "Mix:0/GEQ/Band:10/Gain", -600),
    ("processing", "Mixing", "InputChannel:0/Comp/Bank/Compressor/Knee", "HARD"),
    ("headamp", "Processing", "BuiltIn/InputPort:0/HeadAmp/Gain", 4000),
    ("phantom", "Processing", "Slot/InputPort:7/HeadAmp/48V", 1),
    ("label", "Mixing", "InputChannel:0/Label/Name", "Choir"),
    ("label", "Mixing", "StInChannel:1/Label/Color", "Green"),
    ("fx", "FX", "InsFX:0/Param/Bank/Value:5", -7),
]


@pytest.mark.parametrize("family, func, path, value", WRITES)
def test_write_in_each_family(console, synced, family, func, path, value):
    c = synced(families=[family])
    assert c.status()["write_families"] == [family] and not c.status()["read_only"]
    res = c.write([(path, value)], func=func)
    assert res == [{"path": path, "value": value, "confirmed": True, "actual": value}]
    assert console.all_writes == [(func, path, value)] and c.get(path, func) == value
    [(tag, msg)] = _sets(console)
    assert tag == TAGS[func] and msg.elements[1].value == msg.elements[2].value == func
    assert c.write([(path, value)], func=func)[0]["confirmed"]  # already so: not sent again
    assert len(_sets(console)) == 1


@pytest.mark.parametrize("family, func, path, value", WRITES)
def test_write_refused_without_its_family(console, synced, family, func, path, value):
    c = synced(families=[f for f in ("eq", "label", "mix") if f != family])
    with pytest.raises(WriteNotAllowed):
        c.write([(path, value)], func=func)
    assert _sets(console) == [] and console.all_writes == []


def test_write_is_all_or_nothing_and_needs_the_function(console, client_for, synced):
    c = synced(families=["mix"])
    with pytest.raises(WriteRefused):  # the second is out of range: neither is sent
        c.write([("DCA:0/Fader/Level", -1000), ("DCA:1/Fader/Level", 2000)])
    with pytest.raises(WriteNotAllowed):  # a family covers one function's paths only
        c.write([("DCA:0/Fader/Level", -1000)], func="Setup")
    plain = client_for(console, families=["headamp"])  # Processing not subscribed
    wait_until(lambda: plain.synced)
    with pytest.raises(WriteRefused, match="tf5.functions"):
        plain.write([("BuiltIn/InputPort:0/HeadAmp/Gain", 4000)], func="Processing")
    assert _sets(console) == []


def test_recall_scene(console, synced):
    c = synced(scene_recall=True)
    assert c.status()["scene_recall"] and not c.status()["read_only"]
    assert c.recall_scene("A05") == {"number": "A05", "confirmed": True, "status": "OK"}
    assert console.recalls == ["A05"] and (EVENT, "RecallScene", "NUMBER:A05") in console.events_from_client()
    wait_until(lambda: c.get("Scene/LastScene", "Setup") == "A05")
    for bad in ("C05", "A5", "A100", "A05 NUMBER:A06"):
        with pytest.raises(WriteRefused):
            c.recall_scene(bad)
    assert console.recalls == ["A05"]


def test_recall_scene_needs_its_switch(console, synced):
    c = synced(families=["mix"])
    with pytest.raises(PermissionError, match="scene_recall"):
        c.recall_scene("A05")
    assert console.recalls == []
    assert_read_only(console)


def test_unconfirmed_recall_is_reported(console, synced, monkeypatch):
    c = synced(scene_recall=True)
    monkeypatch.setattr(console, "_recall", lambda conn, args: None)  # a desk that never answers
    assert c.recall_scene("B01", timeout=0.2) == {"number": "B01", "confirmed": False, "status": None}


@pytest.mark.slow
def test_a_function_without_a_dump_is_skipped(client_for, monkeypatch):
    monkeypatch.setattr(editor_client, "FUNC_TIMEOUT_S", 0.2)
    fake = SimEditor(functions=["Property", "Mixing", "Setup"])
    try:
        c = client_for(fake, functions=FUNCTIONS)
        wait_until(lambda: len(c.skipped) == len(FUNCTIONS) - 3, timeout=15)
        st = c.status()["functions"]
        assert st["Setup"]["synced"] and st["Processing"]["skipped"] and not st["Processing"]["synced"]
        assert c.synced and c.get("MuteMaster:0/Name", "Setup") == "INPUT"
    finally:
        fake.close()
