"""Who may change the desk: every call of the editor client's write() / recall_scene() and of
the routes' desk changes is reached only from a route that checks "confirm" first (or from the
vocal assist's confirmed sessions). Split from test_guard.py, which checks the frames themselves;
a change here needs the same deliberate review."""

import functools
import re
from pathlib import Path

from editor_fakes import PKG


@functools.cache
def _src(path: Path) -> str:
    """A source file's text, read once per run (the checks scan the package more than once and
    reads over the dev container's bind mount are slow)."""
    return path.read_text(encoding="utf-8")


def _owner(src: str, pos: int) -> str:
    """The method / function (any indent) whose body holds ``pos``."""
    defs = [(m.start(), m.group(1)) for m in re.finditer(r"\n\s*def (\w+)\(", src)]
    return max((d for d in defs if d[0] < pos), key=lambda d: d[0])[1]


def _mix_page_writes_come_only_from_confirmed_routes(console_src: str) -> None:
    """The Mix page and the SEND tab (console/buses.py, busproc.py, sends.py): TF5Module.write_params
    <- only Buses.write <- only the setters below <- only their routes, each after confirmed()
    (which refuses a body without "confirm": true); sends.py's put <- only module.py's strip_set,
    after its own confirm check."""
    src = {n: _src(PKG / "console" / f"{n}.py") for n in ("buses", "busproc", "sends")}
    assert [_owner(src["buses"], m.start()) for m in re.finditer(r"\.write_params\(", src["buses"])] == ["write"]
    confirm = src["buses"][src["buses"].index("def confirmed()") :]
    assert confirm.index('.get("confirm") is not True') < confirm.index("return body, None")
    setters = {"buses": ["set_strip", "assign", "set_mute"], "busproc": ["set_geq", "set_eq"], "sends": ["put"]}
    for name, code in src.items():
        writers = [_owner(code, m.start()) for m in re.finditer(r"\b(?:self|self\.buses|buses)\.write\(", code)]
        assert sorted(set(writers)) == sorted(setters[name]), (name, writers)
    routes = {"set_strip": "mix_strip", "assign": "mix_assign", "set_mute": "mix_mute"}
    routes |= {"set_geq": "mix_geq_set", "set_eq": "mix_eq_set"}
    for name in ("buses", "busproc"):
        code = src[name]
        for setter in setters[name]:
            uses = [_owner(code, m.start()) for m in re.finditer(rf"\bself\.{setter}\(", code)]
            assert uses == [routes[setter]], (setter, uses)
            route = code[code.index(f"def {routes[setter]}(") :]
            assert (
                route.index("body, err = confirmed()")
                < route.index("err or desk_call(")
                < route.index(f"self.{setter}(")
            )
    others = [p for p in PKG.rglob("*.py") if p.name not in ("buses.py", "busproc.py", "sends.py", "module.py")]
    for p in others:
        assert not re.search(r"\.(set_strip|set_mute|set_geq|set_eq|assign)\(|\bsends\.put\(", _src(p)), p
    assert len(re.findall(r"\.sends\.put\(", console_src)) == 1
    strip_set = console_src[console_src.index("def strip_set(usb: int):") :]
    assert strip_set.index('.get("confirm") is not True') < strip_set.index("self.sends.put(")


def test_desk_writes_are_reachable_only_from_the_confirmed_routes():
    """write() <- TF5Module.write_eq <- either
      * vocal DeskWriter.apply_suggestion / undo <- POST /api/vocal/apply|undo ("confirm": true), or
      * TF5Module's eq_set <- PUT /api/tf5/eq/<usb> ("confirm": true): the page's EQ editing, or
      * vocal DeskWriter.write_values <- profile recall / copy <- POST /api/vocal/profiles/recall|copy
        ("confirm": true), or
      * vocal AutoEQ: its tick (only channels a person turned on: POST /api/vocal/auto with
        "confirm": true, and only when vocal.auto_allowed is set) and its undo (confirmed), or
      * vocal RingOut (notches, and putting the EQ back on Mute & cancel).
    write() <- TF5Module.write_fader (input faders; needs tf5.fader_write) <- either
      * TF5Module's fader_set <- PUT /api/tf5/fader/<usb> ("confirm": true): the sidebar's fader / ON, or
      * vocal RingOut: its loop (started only by start() <- POST /api/vocal/ringout/start with "confirm": true, and
    RingOut exists only when the desk's faders are writable), abort (Mute & cancel: mutes, never
    needs a confirmation) and unmute.
    write() <- TF5Module.write_params (any write family; each needs its own tf5.*_write) <- only
      TF5Module's param_set <- POST /api/tf5/param ("confirm": true): a person's edit.
    write() <- strip_desk.DeskStrip.put (input / 1-knob EQ / gate / comp; the processing, headamp
      and phantom families) <- only Strip.put <- only TF5Module's strip_set <- PUT
      /api/tf5/strip/<usb> ("confirm": true): a person's edit on the strip panel (added 2026-11-10).
    recall_scene() <- TF5Module.recall_scene (needs tf5.scene_recall) <- only
      TF5Module's recall_scene_route <- POST /api/tf5/event/recall_scene ("confirm": true).
    Nothing else in tf-daemon calls them."""
    calls: dict[str, list[str]] = {}
    for p in PKG.rglob("*.py"):
        code = "\n".join(line.split("#", 1)[0] for line in _src(p).splitlines())
        for name, pattern in (
            ("write", r"(\beq_client|\bec|TFEditorClient\b.*)\.write\("),
            ("write_eq", r"\.write_eq\("),
            ("write_fader", r"\.write_fader\("),
            ("write_params", r"\.write_params\("),
            ("recall_scene", r"(\beq_client|\bec|self)\.recall_scene\("),
            ("apply_suggestion", r"\.apply_suggestion\("),
            ("undo", r"\.undo\("),
            ("put", r"\.(?:strip|desk)\.put\("),
        ):
            for _ in re.finditer(pattern, code):
                calls.setdefault(name, []).append(p.relative_to(PKG).as_posix())
    # in write_eq, write_fader and write_params; and DeskStrip.put (the strip panel)
    assert sorted(calls.get("write", [])) == ["console/module.py"] * 3 + ["console/strip_desk.py"]
    strip_src = _src(PKG / "console" / "strip_desk.py")
    strip_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", strip_src)}
    strip_writes = [m.start() for m in re.finditer(r"\bec\.write\(", strip_src)]
    assert [max((n for n, at in strip_defs.items() if at < w), key=lambda n: strip_defs[n]) for w in strip_writes] == [
        "put"
    ]
    # DeskStrip.put only from Strip.put, and that only from the strip route, after the confirmation.
    assert sorted(calls.get("put", [])) == ["console/module.py", "console/strip.py"]
    strip_set = _src(PKG / "console" / "module.py")
    strip_set = strip_set[strip_set.index("def strip_set(usb: int):") :]
    assert strip_set.index('.get("confirm") is not True') < strip_set.index("self.strip.put(")
    console_src = _src(PKG / "console" / "module.py")
    assert "ec = self.eq_client" in console_src
    console_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", console_src)}
    ec_writes = [m.start() for m in re.finditer(r"\bec\.write\(", console_src)]
    assert sorted(
        max((n for n, at in console_defs.items() if at < w), key=lambda n: console_defs[n]) for w in ec_writes
    ) == ["write_eq", "write_fader", "write_params"]
    # The generic write (any family) and the scene recall: each from its one route only, after the confirmation.
    assert sorted(calls.get("write_params", [])) == ["console/buses.py", "console/module.py"]
    assert calls.get("recall_scene") == ["console/module.py"] * 2
    _mix_page_writes_come_only_from_confirmed_routes(console_src)
    ec_recalls = [m.start() for m in re.finditer(r"\bec\.recall_scene\(", console_src)]
    owner = lambda pos: max((n for n, at in console_defs.items() if at < pos), key=lambda n: console_defs[n])  # noqa: E731
    assert [owner(w) for w in ec_recalls] == ["recall_scene"]
    param_set = console_src[console_src.index("def param_set():") :]
    assert param_set.index('.get("confirm") is not True') < param_set.index("self.write_params(")
    recall_route = console_src[console_src.index("def recall_scene_route():") :]
    assert recall_route.index('.get("confirm") is not True') < recall_route.index("self.recall_scene(")
    assert sorted(calls.get("write_eq", [])) == [
        "console/module.py",
        "vocal/auto.py",
        "vocal/auto.py",
        "vocal/desk.py",
        "vocal/desk.py",
        "vocal/ringout.py",
        "vocal/ringout.py",
    ]
    assert sorted(calls.get("write_fader", [])) == ["console/module.py"] + ["vocal/ringout.py"] * 4
    # The sidebar's fader / ON: PUT /api/tf5/fader/<usb>, refused without "confirm": true before any write.
    fader_set = console_src[console_src.index("def fader_set(usb: int):") :]
    assert fader_set.index('.get("confirm") is not True') < fader_set.index("self.write_fader(")
    # The page's EQ editing: PUT /api/tf5/eq/<usb>, refused without "confirm": true before any write.
    eq_set = console_src[console_src.index("def eq_set(usb: int):") :]
    assert eq_set.index('.get("confirm") is not True') < eq_set.index("self.write_eq(")
    vocal_src = _src(PKG / "vocal" / "module.py")
    assert re.search(r"desk_write\(self\.desk\.apply_suggestion\)", vocal_src)
    assert re.search(r"desk_write\(self\.desk\.undo\)", vocal_src)
    assert "apply_suggestion" not in calls and "undo" not in calls  # never called, only handed to desk_write
    assert '.get("confirm") is not True' in vocal_src
    # Profile recall / copy: write_values, only from those two methods, only handed to desk_write.
    assert sorted(re.findall(r"desk_write\(lambda ch: self\.(recall_profile|copy_eq)\(", vocal_src)) == [
        "copy_eq",
        "recall_profile",
    ]
    assert len(re.findall(r"\.(recall_profile|copy_eq)\(", vocal_src)) == 2
    defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", vocal_src)}
    writes = [m.start() for m in re.finditer(r"\.write_values\(", vocal_src)]
    assert sorted(max((n for n, at in defs.items() if at < w), key=lambda n: defs[n]) for w in writes) == [
        "copy_eq",
        "recall_profile",
    ]
    # Auto EQ: exists only with vocal.auto_allowed; turning it on and its undo need the confirmation.
    assert re.search(r"self\.auto = \(?\s*AutoEQ\(.*?\)\s*\\?\s*if cfg\.auto_allowed\s+else None", vocal_src, re.S)
    assert re.search(r"desk_write\(self\.auto\.arm\)", vocal_src)
    assert re.search(r"desk_write\(self\.auto\.undo\)", vocal_src)
    auto_src = _src(PKG / "vocal" / "auto.py")
    tick = re.search(r"    def tick\(self.*?(?=\n    def )", auto_src, re.S).group(0)
    assert "list(self._channels.items())" in tick  # only channels a person turned on
    writers = [m.start() for m in re.finditer(r"\.write_eq\(", auto_src)]
    defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", auto_src)}
    owner = lambda pos: max((n for n, at in defs.items() if at < pos), key=lambda n: defs[n])  # noqa: E731
    assert sorted(owner(w) for w in writers) == ["_tick_channel", "undo"]
    # Ring-out: exists only with writable faders; starting it needs the confirmation; its loop
    # thread is started only by start(); Mute & cancel (abort) and unmute need none.
    assert re.search(r"getattr\(desk, \"fader_writable\", False\)", vocal_src)
    assert re.search(r"self\.ringout = \(\s*RingOut\(.*?\)\s*if faders is not None\s+else None", vocal_src, re.S)
    ro_route = vocal_src[vocal_src.index("def ringout_start():") :]
    assert ro_route.index('.get("confirm") is not True') < ro_route.index("r.start(")
    assert len(re.findall(r"\br\.start\(", vocal_src)) == 1
    ro_src = _src(PKG / "vocal" / "ringout.py")
    ro_defs = {m.group(1): m.start() for m in re.finditer(r"\n    def (\w+)\(", ro_src)}
    ro_owner = lambda pos: max((n for n, at in ro_defs.items() if at < pos), key=lambda n: ro_defs[n])  # noqa: E731
    assert sorted(ro_owner(m.start()) for m in re.finditer(r"\.write_fader\(", ro_src)) == [
        "_fader",
        "_restore",
        "abort",
        "unmute",
    ]
    assert [ro_owner(m.start()) for m in re.finditer(r"threading\.Thread\(target=self\._run", ro_src)] == ["start"]
    assert "the EQ was changed on the desk" in auto_src  # it stops when someone else touches it
