"""MMSXLIT dumps and a channel's EQ settings (console/editor/dump.py)."""

import struct

import pytest
from editor_fakes import (
    C,
    build_dump,
    default_mixing_values,
    dump_message,
    mixing_tree,
    wire_address,
)

from tf_daemon.console.editor.codec import (
    DESC_MD5,
    DUMP,
    T_U8,
    ProtocolError,
    element,
    message,
    parse_message,
)
from tf_daemon.console.editor.dump import Dump, channel_eq, dump_blob


def test_dump_parses_header_and_tree(mixing):
    assert mixing.func == "Mixing" and mixing.desc_md5 == DESC_MD5["Mixing"]
    names = [c.name for c in mixing.root.children]
    assert names == [  # the V4.55 tree (tf5_v455.json), the console's order
        "InputChannel",
        "StInChannel",
        "FxRtnChannel",
        "Group",
        "Mix",
        "StMix",
        "Matrix",
        "FxBus",
        "Stereo",
        "Mono",
        "DCA",
    ]
    ic = mixing.root.child("InputChannel")
    assert ic.arraysize == 40
    assert len(mixing.data) == mixing.root.datasize
    assert mixing.get("InputChannel:0/Label/Name") == "GRA"
    assert mixing.get("InputChannel:0/PEQ/Bank/BandL/Gain") == -520  # signed
    assert mixing.get("InputChannel:0/PEQ/Bank/BandH/Frequency") == 100000
    assert mixing.get("InputChannel:39/Label/Name") == "ch40"
    assert mixing.get("Stereo:1/PEQ/Bank/BandL/HPFOn") == 1


@pytest.mark.parametrize(
    "path",
    [
        "InputChannel:40/Label/Name",
        "InputChannel:0/PEQ/Bank/Band:2/Gain",
        "InputChannel:0/Nope/On",
        "InputChannel:0/PEQ/Nope",
        "Nope:0/Label/Name",
        "InputChannel:-1/Label/Name",
    ],
)
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


@pytest.mark.parametrize(
    "path, idx",
    [
        ([], []),
        ([0, 1], [0]),
        ([9, 0], [0, 0]),
        ([0, 99], [0, 0]),
        ([0, 0, 99], [0, 0, 0]),
        ([0, 0, 0], [40, 0, 0]),
        ([0, 0, 0], [0, 0, 1]),
        ([0, 0], [0, 0]),
    ],
)
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
    assert after[:off] == before[:off] and after[off + prm.elemsize :] == before[off + prm.elemsize :]
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
    bad = [
        b"",
        b"MMSXLIX\0" + good[8:],
        good[:0x60],
        good[:0x58] + b"XXXX" + good[0x5C:],
        good[:0x50] + struct.pack("<I", desc_len - 32) + good[0x54:],
        good[:0x50] + struct.pack("<I", desc_len + 10**6) + good[0x54:],
        good[:0x54] + struct.pack("<I", 4) + good[0x58 : 0x58 + desc_len] + bytes(4),
    ]
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
    assert [(c.name, c.offset) for c in bank.children] == [
        ("OneKnob", 8),
        ("HPF", 22),
        ("BandL", 28),
        ("Band", 37),
        ("BandH", 55),
    ]
    assert bank.datasize == 65 and bank.child("Band").datasize == 9 and bank.child("Band").arraysize == 2


# ====================================================================== EQ settings


def test_channel_eq_input_channel(mixing):
    eq = channel_eq(mixing, "InCh", 0)
    assert eq == {
        "on": True,
        "bank": "BOLD",
        "one_knob": False,
        "hpf": {"on": True, "freq": 200.0, "slope": 12},
        "bands": [
            {"label": "L", "type": "bell", "on": True, "freq": 160.0, "gain": -5.2, "q": 0.14},
            {"label": "LM", "type": "bell", "on": True, "freq": 800.0, "gain": -14.1, "q": 0.25},
            {"label": "HM", "type": "bell", "on": True, "freq": 3350.0, "gain": -5.7, "q": 0.35},
            {"label": "H", "type": "high_shelf", "on": True, "freq": 10000.0, "gain": 4.6, "q": 0.7},
        ],
    }
    eq2 = channel_eq(mixing, "InCh", 1)
    assert not eq2["on"] and eq2["one_knob"] and eq2["bands"][1]["gain"] == 6.0
    assert channel_eq(mixing, "InCh", 2)["bands"][0]["type"] == "low_shelf"


def test_channel_eq_stereo_bus(mixing):
    st_l, st_r = channel_eq(mixing, "St", 0), channel_eq(mixing, "St", 1)
    assert st_l["hpf"] is None and [b["label"] for b in st_l["bands"]] == ["L", "LM", "HM", "H"]
    assert st_l["bands"][0]["type"] == "bell"
    assert st_r["bands"][0] == {"label": "L", "type": "hpf", "on": True, "freq": 80.0, "gain": 0.0, "q": 0.7}
    assert st_r["bands"][2]["gain"] == -3.0


@pytest.mark.parametrize("kind, x", [("InCh", 40), ("InCh", -1), ("St", 2), ("Mix", 0), ("FxRtn", 0)])
def test_channel_eq_missing(mixing, kind, x):
    assert channel_eq(mixing, kind, x) is None


# As TF Editor's band table builder (FUN_0051a080) maps them: type 2 is the filter, and
# it is only on while the band's HPFOn / LPFOn switch is set.
@pytest.mark.parametrize(
    "band, values, expect, on",
    [
        ("BandL", {"Type": 0}, "bell", True),
        ("BandL", {"Type": 1}, "low_shelf", True),
        ("BandH", {"Type": 0}, "bell", True),
        ("BandH", {"Type": 1}, "high_shelf", True),
        ("BandH", {"Type": 2, "LPFOn": 1}, "lpf", True),
        ("BandH", {"Type": 2, "LPFOn": 0}, "lpf", False),
        ("BandH", {"Type": 1, "LPFOn": 1}, "high_shelf", True),
        ("BandH", {"Type": 0, "LPFOn": 1}, "bell", True),
        ("Band:0", {"Type": 1}, "bell", True),
        ("Band:1", {"Type": 2}, "bell", True),
    ],
)
def test_band_types(band, values, expect, on):
    v = default_mixing_values()
    v.update({f"InputChannel:4/PEQ/Bank/{band}/{k}": val for k, val in values.items()})
    eq = channel_eq(Dump(build_dump(mixing_tree(), v)), "InCh", 4)
    label = {"BandL": "L", "BandH": "H", "Band:0": "LM", "Band:1": "HM"}[band]
    b = next(b for b in eq["bands"] if b["label"] == label)
    assert (b["type"], b["on"]) == (expect, on)


@pytest.mark.parametrize(
    "values, expect, on",
    [
        ({"Type": 2, "HPFOn": 1}, "hpf", True),
        ({"Type": 2, "HPFOn": 0}, "hpf", False),
        ({"Type": 0, "HPFOn": 1}, "bell", True),
        ({"Type": 1}, "low_shelf", True),
    ],
)
def test_stereo_low_band_types(values, expect, on):
    v = default_mixing_values()
    v.update({f"Stereo:0/PEQ/Bank/BandL/{k}": val for k, val in values.items()})
    b = channel_eq(Dump(build_dump(mixing_tree(), v)), "St", 0)["bands"][0]
    assert (b["type"], b["on"]) == (expect, on)


def test_channel_eq_without_mid_bands_or_one_knob():
    tree = C(
        "Mixing",
        1,
        [],
        [
            C(
                "Stereo",
                2,
                [],
                [
                    C(
                        "PEQ",
                        1,
                        [("On", 2, 1, 1)],
                        [
                            C(
                                "Bank",
                                1,
                                [("Type", 0, 8, 1)],
                                [
                                    C(
                                        "BandL",
                                        1,
                                        [("Type", 2, 1, 1), ("Frequency", 2, 4, 1), ("Gain", 1, 2, 1), ("Q", 2, 2, 1)],
                                    ),
                                    C(
                                        "BandH",
                                        1,
                                        [("Type", 2, 1, 1), ("Frequency", 2, 4, 1), ("Gain", 1, 2, 1), ("Q", 2, 2, 1)],
                                    ),
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    eq = channel_eq(Dump(build_dump(tree, {"Stereo:0/PEQ/On": 1, "Stereo:0/PEQ/Bank/BandL/Q": 700})), "St", 0)
    assert [b["label"] for b in eq["bands"]] == ["L", "H"] and not eq["one_knob"] and eq["hpf"] is None
