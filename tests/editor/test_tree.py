"""The V4.55 parameter tree (console/editor/tree.py) and the simulated desk built from it
(console/sim_tree.py): tags, MD5s, schema, and real wire addresses resolving as on the desk."""

import pytest
from editor_fakes import function_dump

from tf_daemon.console.editor import codec, guard, tree
from tf_daemon.console.editor.dump import Dump

# docs/desk-coverage-howto-complete.md 1.2 (from TF Editor's session with the TF5).
DOC_TABLE = {
    "Property": ("MPRO", "3a7c8d4c85f89f1eaa834f96630cec3d"),
    "Mixing": ("MMIX", "8d0d0bdd394a9abb80ab12ed1500eddc"),
    "Processing": ("MPRC", "ed786e481a8abee4c76d283374ccc574"),
    "FX": ("MFX", "bfa272e1e86bf3eebb24e07b3066526d"),
    "Setup": ("MSUP", "a538d99fa5da636d99e01a2f807245ed"),
    "Volatile": ("MVOL", "abc686b9aaf785522a219a97fae13808"),
    "Status": ("MSTS", "2e5465009cb5c09ce6ca89682acfdcea"),
    "SceneList": ("MSCL", "34f8de2039cf7651a6db7f159202865d"),
    "SceneStatus": ("MSCS", "865cba150ed9d5db9f52f8aa6ae418f6"),
    "ConsoleSetup": ("MCST", "99c891b8924cb64e5401ffaa51a32ccb"),
}


def test_tags_and_md5s_are_one_fact():
    assert tree.FIRMWARE.startswith("V4.55") and tree.FUNCTIONS == tuple(DOC_TABLE)
    assert {f: (tree.TAGS[f], tree.DESC_MD5[f]) for f in tree.FUNCTIONS} == DOC_TABLE
    assert codec.TAGS is tree.TAGS and codec.DESC_MD5 is tree.DESC_MD5  # the codec uses the tree's
    assert tree.function_of_tag("MPRC") == "Processing" and tree.function_of_tag("EEVT") is None


def test_spec():
    g = tree.spec("Mixing", "InputChannel:3/PEQ/Bank/Band:1/Gain")
    assert (g.type, g.width, g.min, g.max, g.default, g.scaling, g.unit, g.code) == (
        "int",
        2,
        -1800,
        1800,
        0,
        100,
        "dB",
        1,
    )
    ha = tree.spec("Processing", "Slot/InputPort:63/HeadAmp/Gain")
    assert (ha.min, ha.max, ha.default) == (-600, 6600, -600)
    name = tree.spec("Mixing", "Mono/Label/Name")
    assert (name.type, name.width, name.max, name.min, name.default, name.code) == ("str", 64, 63, None, None, 0)
    value = tree.spec("FX", "InsFX:5/Param/Bank/Value:63")
    assert value.size == 64 and value.type == "int" and value.as_dict()["name"] == "Value"
    assert tree.spec("Setup", "MuteMaster:5/On").max == 1


@pytest.mark.parametrize(
    "func, path",
    [
        ("Mixing", "InputChannel:40/Fader/Level"),  # 40 channels
        ("Mixing", "InputChannel:0/PEQ/Bank/Band:2/Gain"),
        ("Mixing", "InputChannel:0/Patch/Port"),  # the old fake's shape; the desk has Select
        ("Mixing", "Mono:1/Fader/On"),
        ("Mixing", "InputChannel:0/PEQ"),  # a collection, not a parameter
        ("FX", "FX:0/Param/Bank/Value:64"),
        ("Nope", "X"),
    ],
)
def test_spec_rejects_what_the_desk_doesnt_have(func, path):
    with pytest.raises(KeyError):
        tree.spec(func, path)


# Wire addresses read from the TF5's own dumps / TF Editor's traffic (docs/desk-coverage-howto-complete.md,
# guides/tf5_api.md 4.4): the simulator must put every parameter where the desk has it.
REAL_ADDRESSES = [
    ("Mixing", "InputChannel:0/Label/Name", [0, 0, 1], [0, 0, 0]),
    ("Mixing", "InputChannel:0/Patch/Select", [0, 1, 0], [0, 0, 0]),
    ("Mixing", "InputChannel:0/PEQ/Bank/BandL/Gain", [0, 7, 1, 3, 2], [0, 0, 0, 0, 0]),
    ("Mixing", "InputChannel:14/Fader/On", [0, 19, 1], [14, 0, 0]),
    ("Mixing", "InputChannel:2/DCAGroup:0/Assign", [0, 17, 0], [2, 0, 0]),
    ("Mixing", "InputChannel:0/ToStereo/Pan", [0, 15, 0], [0, 0, 0]),
    ("Mixing", "InputChannel:0/Gate/Bank/Gate/Threshold", [0, 8, 1, 1, 0], [0, 0, 0, 0, 0]),
    ("Mixing", "InputChannel:0/Comp/Bank/Compressor/Knee", [0, 9, 1, 2, 4], [0, 0, 0, 0, 0]),
    ("Mixing", "StInChannel:0/Fader/Level", [1, 15, 0], [0, 0, 0]),
    ("Mixing", "Mix:0/Fader/On", [4, 7, 1], [0, 0, 0]),
    ("Mixing", "Mix:0/GEQ/Band:10/Gain", [4, 4, 2, 0], [0, 0, 10, 0]),
    ("Mixing", "StMix:0/Fader/Level", [5, 8, 0], [0, 0, 0]),
    ("Mixing", "Stereo:0/Output/Balance", [8, 6, 0], [0, 0, 0]),
    ("Mixing", "Mono/Fader/Level", [9, 4, 1], [0, 0, 0]),
    ("Mixing", "Mono/Fader/On", [9, 4, 3], [0, 0, 0]),
    ("Mixing", "DCA:0/Fader/Level", [10, 1, 0], [0, 0, 0]),
    ("Processing", "BuiltIn/InputPort:0/HeadAmp/Gain", [0, 0, 0, 1], [0, 0, 0, 0]),
    ("Processing", "BuiltIn/InputPort:5/HeadAmp/48V", [0, 0, 0, 0], [0, 5, 0, 0]),
    ("Setup", "MuteMaster:0/On", [4, 0], [0, 0]),
    ("Setup", "PhantomMaster/On", [2, 0], [0, 0]),
    ("Volatile", "Cue/InputChannel:14/On", [0, 1, 0], [0, 14, 0]),
    ("Volatile", "Meter/RTA/Target", [5, 0, 0], [0, 0, 0]),
    ("Property", "Status/CommSession:1/ClientType", [2, 0, 0], [0, 1, 0]),
    ("FX", "FX:1/Param/Bank/Value:5", [0, 2, 2, 0], [1, 0, 0, 5]),
]


@pytest.mark.parametrize("func, path, wire, idx", REAL_ADDRESSES)
def test_simulated_desk_has_the_real_addresses(func, path, wire, idx):
    dump = Dump(function_dump(func))
    assert dump.address(path)[:2] == (wire, idx)
    assert dump.resolve(wire, idx)[0] == path


@pytest.mark.parametrize("func", tree.FUNCTIONS)
def test_simulated_dumps_cover_the_whole_tree_with_its_defaults(func):
    dump = Dump(function_dump(func))
    assert dump.func == func and dump.desc_md5 == tree.DESC_MD5[func]
    checked = 0

    def walk(node, col, prefix):
        nonlocal checked
        assert [c.name for c in col.children] == [c["name"] for c in node["children"]]
        assert [(p.name, p.elemsize, p.arraysize) for p in col.params] == [
            (p["name"], p["width"], p["size"]) for p in node["params"]
        ]
        for p in node["params"]:
            if p["type"] != "str" and p.get("default") is not None:  # the last element of every array
                path = f"{prefix}{p['name']}" + (f":{p['size'] - 1}" if p["size"] > 1 else "")
                assert dump.get(path) == p["default"], path
                checked += 1
        for ch_node, ch in zip(node["children"], col.children, strict=True):
            last = f":{ch_node['size'] - 1}" if ch_node["size"] > 1 else ""
            walk(ch_node, ch, f"{prefix}{ch_node['name']}{last}/")

    walk(tree.tree(func), dump.root, "")
    assert checked > 0 or func == "SceneList"  # SceneList holds file names only


def test_every_family_rule_names_a_function_of_the_tree():
    for fam in guard.FAMILIES.values():
        assert fam.switch.startswith("tf5.") and fam.rules
        assert all(r.func in tree.FUNCTIONS and r.func != "Property" for r in fam.rules)
