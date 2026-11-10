"""The desk coverage list (tf_daemon/console/coverage.py): the doc table is it, and what it says
is writable is what the write guard allows. Its route is checked in tests/test_live.py."""

from __future__ import annotations

import re

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
    # A feature said to be written to the desk has a guard family for it behind the switch it
    # names; one said not to be written isn't in the eq / fader patterns (the other families are
    # opt-in groundwork for features still to come, each behind its own switch).
    sample = {
        "fader": "InputChannel:0/Fader/Level",
        "on": "InputChannel:0/Fader/On",
        "peq": "InputChannel:0/PEQ/Bank/Band:1/Gain",
        "hpf": "InputChannel:0/PEQ/Bank/HPF/Frequency",
        "hpf_slope": "InputChannel:0/PEQ/Bank/HPF/Slope",
        "one_knob_eq": "InputChannel:0/PEQ/Bank/OneKnob/On",
        "stereo_eq": "Stereo:0/PEQ/Bank/Band:1/Gain",
        "pan": "InputChannel:0/ToStereo/Pan",
        "sends": "InputChannel:0/ToMix:0/Level",
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
        if f.write.startswith("Editor"):
            switches = set(re.findall(r"tf5\.\w+", f.write))
            assert switches and switches <= {fam.switch for fam, _ in families_for(func, path)}, (key, f.write)
        else:
            assert not (WRITABLE.fullmatch(path) or FADER_WRITABLE.fullmatch(path)), (key, path, f.write)
