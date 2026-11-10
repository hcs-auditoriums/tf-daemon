"""The desk coverage list (tf_daemon/console/coverage.py): the doc table is it, and what it says
is writable is what the write guard allows. Its route is checked in tests/test_live.py."""

from __future__ import annotations

from tf_daemon.console import coverage
from tf_daemon.console.editor.guard import FADER_WRITABLE, WRITABLE


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
    # A feature said to be written to the desk has a guard pattern for it, and nothing else is.
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
    }
    for key, path in sample.items():
        f = next(x for x in coverage.FEATURES if x.key == key)
        allowed = bool(WRITABLE.fullmatch(path) or FADER_WRITABLE.fullmatch(path))
        assert allowed == f.write.startswith("Editor"), (key, path, f.write)
