"""The desk coverage list (tf_daemon/console/coverage.py): the doc table is it, and what it says
is writable is what the write guard allows. Its route is checked in tests/test_live.py."""

from __future__ import annotations

from tf_daemon.console import coverage
from tf_daemon.console.editor.guard import FADER_WRITABLE, WRITABLE, families_for


def test_the_doc_table_is_the_list():
    doc = coverage.DOC.read_text(encoding="utf-8")
    table = doc.split(coverage.BEGIN, 1)[1].split(coverage.END, 1)[0].strip()
    assert table == coverage.markdown_table(), "run: python -m tf_daemon.console.coverage"


def test_every_feature_is_described_once():
    keys = [f.key for f in coverage.FEATURES]
    assert len(keys) == len(set(keys))
    for f in coverage.FEATURES:
        assert f.page in {"edit", "view", "no"} and f.sim in {"yes", "no", "app", "-"}, f


def test_what_is_writable_matches_the_guard():
    # A feature said to be written to the desk names the switch of a guard family that covers it
    # (guard.FAMILIES), and nothing else is said to be written.
    sample = {
        "fader": "InputChannel:0/Fader/Level",
        "on": "InputChannel:0/Fader/On",
        "peq": "InputChannel:0/PEQ/Bank/Band:1/Gain",
        "hpf": "InputChannel:0/PEQ/Bank/HPF/Frequency",
        "hpf_slope": "InputChannel:0/PEQ/Bank/HPF/Slope",
        "one_knob_eq": "InputChannel:0/PEQ/Bank/OneKnob/On",
        "eq_bank_type": "InputChannel:0/PEQ/Bank/Type",
        "stereo_eq": "Stereo:0/PEQ/Bank/Band:1/Gain",
        "pan": "InputChannel:0/ToStereo/Pan",
        "sends": "InputChannel:0/ToMix:0/Level",
        "gate": "InputChannel:0/Gate/Bank/Gate/Threshold",
        "comp": "InputChannel:0/Comp/Bank/Compressor/Ratio",
        "input_gain": ("Processing", "BuiltIn/InputPort:0/HeadAmp/Gain"),
        "stereo_fader": "Stereo:1/Output/Balance",
        "mix_fader": "StMix:3/Fader/Level",
        "dca": "InputChannel:0/DCAGroup:2/Assign",
        "send_pre": "InputChannel:0/ToStMix:4/Pre",
        "send_pan": "InputChannel:0/ToStMix:1/Pan",
        "fx_sends": "InputChannel:0/ToFX:1/Level",
        "geq": "Mix:0/GEQ/Band:3/Gain",
        "cue": ("Volatile", "Cue/Mix:0/On"),
        "mute_groups": ("Setup", "MuteMaster:0/On"),
    }
    for key, where in sample.items():
        func, path = where if isinstance(where, tuple) else ("Mixing", where)
        f = next(x for x in coverage.FEATURES if x.key == key)
        allowed = any(fam.switch in f.write for fam, _ in families_for(func, path))
        assert allowed == f.write.startswith("Editor"), (key, path, f.write)
    # The original two families as the guard defines them.
    assert WRITABLE.fullmatch(sample["peq"]) and FADER_WRITABLE.fullmatch(sample["fader"])
