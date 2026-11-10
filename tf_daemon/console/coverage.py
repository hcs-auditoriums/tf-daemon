"""What tf-daemon can read from and write to the TF5 today, feature by feature, against what
TF StageMix offers: the one list of it (served at /api/tf5/coverage, and the table in
docs/desk-coverage.md, which tests/test_coverage.py keeps identical to this).

Columns:
  read   how the server reads it from a real desk: "RCP" (remote control protocol, TCP 49280),
         "Editor" (TF Editor protocol, TCP 50368), "RCP + Editor", or "no"
  write  what can change it on a real desk: "Editor" plus the config switch that allows it, or "no"
         (every write goes through console/editor/guard.py, which refuses anything else)
  sim    whether the demo / test simulator models it ("yes", "no", or "app" when the
         application keeps it in its own simulated store until the desk protocol exists)
  page   what the pages offer: "edit", "view", or "no"

Update the row when a feature's protocol or page changes, then run
``python -m tf_daemon.console.coverage`` to rewrite the table in docs/desk-coverage.md.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    screen: str  # where TF StageMix has it
    read: str
    write: str
    sim: str
    page: str
    notes: str = ""


FEATURES: list[Feature] = [
    # ---------------------------------------------------------------- channel strip
    Feature("name", "Channel name and colour", "Home (CH name)", "RCP + Editor", "no", "yes", "view"),
    Feature(
        "fader",
        "Input fader level",
        "Home (fader)",
        "RCP + Editor",
        "Editor (tf5.fader_write)",
        "yes",
        "edit",
        "Input channels only",
    ),
    Feature(
        "on",
        "Input channel ON",
        "Home (fader)",
        "RCP + Editor",
        "Editor (tf5.fader_write)",
        "yes",
        "edit",
        "Input channels only",
    ),
    Feature("pan", "Input pan", "Home (CH name)", "RCP", "no", "yes", "view", "ToStereo/Pan"),
    Feature(
        "stereo_fader",
        "Stereo fader, ON, balance",
        "Home (overview strip)",
        "RCP + Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "Mix page (console/buses.py; L and R set together); also in the sidebar when a USB input carries it",
    ),
    Feature(
        "mix_fader",
        "AUX / mix fader and ON",
        "Sends on fader (overview strip)",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "Mix page: AUX 1-8 (Mix), AUX 9-20 as pairs (StMix, both halves), balance; MATRIX and MONO too",
    ),
    Feature(
        "cue",
        "CUE",
        "Home (fader)",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "Mix page: Volatile/Cue per strip; Cue/ActiveCategory read; write acceptance in Volatile unverified",
    ),
    Feature(
        "dca",
        "DCA faders and assignment",
        "Home, Custom fader bank",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "Mix page: DCA 1-8 masters and labels, DCAGroup assignment of CH / ST IN / FX RTN",
    ),
    Feature(
        "mute_groups",
        "Mute groups and master mutes",
        "Utility",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "Mix page: Setup/MuteMaster on (names read), MuteGroup assignment; group -> master mapping inferred "
        "(channel g = master g, FX = masters 5/6); write acceptance in Setup unverified",
    ),
    Feature(
        "meters",
        "Level meters",
        "Home (meters)",
        "RCP (mtr, unverified on a real TF5)",
        "no",
        "yes",
        "view",
        "Input channels + stereo bus, console/meters.py (tf5.meters); YMDP mapping pending (guide section 8)",
    ),
    # ---------------------------------------------------------------- sends
    Feature(
        "sends",
        "Sends to AUX mixes: level, ON",
        "Send to, Sends on fader",
        "RCP + Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "SEND tab from the desk once the editor protocol is synced (console/sends.py: ToMix / ToStMix, "
        "a stereo pair on both halves); RCP before",
    ),
    Feature(
        "send_pre",
        "Send pre / post fader",
        "Send to, Sends on fader",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "ToMix / ToStMix / ToFX Pre; the tap point (SendPoint/Pre) is read, not written",
    ),
    Feature(
        "send_pan",
        "Send pan (stereo AUX)",
        "Send to, Sends on fader",
        "Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "ToStMix Pan on both halves of a pair; Signal/PanLink read",
    ),
    Feature(
        "fx_sends",
        "Sends to FX 1/2 and mono",
        "Send to",
        "RCP + Editor",
        "Editor (tf5.mix_write)",
        "yes",
        "edit",
        "SEND tab rows FX 1/2 (level, ON, pre) and MONO (level, ON)",
    ),
    # ---------------------------------------------------------------- input
    Feature(
        "input_gain",
        "Head-amp gain, +48V, phase, digital gain",
        "Input",
        "no",
        "no",
        "app",
        "edit",
        "Simulated by the app until the Editor dump's Input names are recorded; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5",
    ),
    Feature(
        "input_source",
        "Input source (INPUT / USB / SLOT), stereo link",
        "Input",
        "Editor",
        "no",
        "yes",
        "view",
        "Patch/Select (0 INPUT, 1 USB, 2 SLOT): read for the USB patch",
    ),
    # ---------------------------------------------------------------- EQ
    Feature(
        "peq",
        "Input PEQ: on, 4 bands type / freq / gain / Q, LPF on",
        "EQ",
        "Editor",
        "Editor (tf5.eq_write)",
        "yes",
        "edit",
        "Ranges: guard.WRITE_LIMITS",
    ),
    Feature("hpf", "Input HPF on and frequency", "EQ", "Editor", "Editor (tf5.eq_write)", "yes", "edit"),
    Feature("hpf_slope", "HPF slope", "EQ", "Editor", "no", "yes", "view"),
    Feature(
        "one_knob_eq",
        "1-knob EQ (Vocal / Intensity / Loudness, level)",
        "EQ",
        "Editor",
        "no",
        "yes",
        "view",
        "OneKnob/On read; Mode / Depth in the dump, not read",
    ),
    Feature("eq_bank_type", "EQ type (e.g. BOLD)", "EQ", "Editor", "no", "yes", "no"),
    Feature(
        "stereo_eq",
        "Stereo / AUX / matrix PEQ",
        "EQ",
        "Editor",
        "Editor (tf5.processing_write)",
        "yes",
        "edit",
        "Mix page (console/busproc.py): ST, AUX, MATRIX, MONO with the desk's curve; a stereo bus's halves "
        "set together; MONO's LPF shown, not drawn",
    ),
    Feature(
        "geq",
        "GEQ (AUX 1-8, stereo): 31 bands, 12 adjustable, +-15 dB",
        "GEQ",
        "Editor",
        "Editor (tf5.processing_write)",
        "yes",
        "edit",
        "Mix page; the 12-band limit enforced here (the console's own behaviour past 12 pending); ST per side "
        "unless L/R linked; the ring list links each ring to its band",
    ),
    # ---------------------------------------------------------------- dynamics
    Feature(
        "gate",
        "Gate: on, threshold, range, attack, hold, decay",
        "Gate",
        "no",
        "no",
        "app",
        "edit",
        "Simulated by the app until the Gate names are recorded; CH 1-32; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5",
    ),
    Feature(
        "comp",
        "Compressor: on, 1-knob, threshold, ratio, attack, release, gain, knee",
        "Comp",
        "no",
        "no",
        "app",
        "edit",
        "Simulated by the app until the Comp names are recorded; 1-knob amount only; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5",
    ),
    # ---------------------------------------------------------------- the rest
    Feature(
        "scenes",
        "Scene list, recall, store",
        "Scene",
        "no",
        "no",
        "no",
        "no",
        "A recall is noticed (RCP sscurrent) and everything re-read; RecallScene is refused by the guard",
    ),
    Feature("fx", "FX 1/2, insert FX, delay", "FX, Delay", "no", "no", "no", "no"),
    Feature("recorder", "USB recorder transport", "Recorder", "no", "no", "no", "no"),
    Feature(
        "copy_paste",
        "Copy / paste / compare / default per screen",
        "Every screen's menu",
        "-",
        "-",
        "-",
        "edit",
        "EQ: copy, paste, A/B, gain flat, undo (page side)",
    ),
]

BEGIN, END = "<!-- coverage:begin -->", "<!-- coverage:end -->"
DOC = Path(__file__).resolve().parents[2] / "docs" / "desk-coverage.md"


def as_dicts() -> list[dict[str, str]]:
    return [asdict(f) for f in FEATURES]


def markdown_table() -> str:
    """The table as it stands in docs/desk-coverage.md (between the coverage markers)."""
    rows = ["| Feature | StageMix screen | Read | Write | Simulated | Page | Notes |", "|---|---|---|---|---|---|---|"]
    for f in FEATURES:
        rows.append(f"| {f.name} | {f.screen} | {f.read} | {f.write} | {f.sim} | {f.page} | {f.notes} |")
    return "\n".join(rows)


def rewrite_doc(path: Path = DOC) -> None:
    text = path.read_text(encoding="utf-8")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    path.write_text(f"{head}{BEGIN}\n{markdown_table()}\n{END}{tail}", encoding="utf-8")


if __name__ == "__main__":
    rewrite_doc(Path(sys.argv[1]) if len(sys.argv) > 1 else DOC)
