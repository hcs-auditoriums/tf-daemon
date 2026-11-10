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
        "RCP",
        "no",
        "yes",
        "view",
        "In the sidebar when a USB input carries the stereo bus",
    ),
    Feature(
        "mix_fader",
        "AUX / mix fader and ON",
        "Sends on fader (overview strip)",
        "no",
        "no",
        "no",
        "no",
        "Only mix names are read (RCP)",
    ),
    Feature("cue", "CUE", "Home (fader)", "no", "no", "no", "no"),
    Feature(
        "dca",
        "DCA faders and assignment",
        "Home, Custom fader bank",
        "no",
        "no",
        "no",
        "no",
        "DCAGroup is in the Editor dump; parameter names not recorded yet",
    ),
    Feature(
        "mute_groups",
        "Mute groups and master mutes",
        "Utility",
        "no",
        "no",
        "no",
        "no",
        "MuteGroup is in the Editor dump; parameter names not recorded yet",
    ),
    Feature(
        "meters",
        "Level meters",
        "Home (meters)",
        "no",
        "no",
        "no",
        "no",
        "YMDP UDP framing known (guides/tf5_api.md section 8); block-to-meter mapping pending",
    ),
    # ---------------------------------------------------------------- sends
    Feature(
        "sends",
        "Sends to AUX mixes: level, ON",
        "Send to, Sends on fader",
        "RCP",
        "no",
        "yes",
        "edit",
        "ToMix/Level, ToMix/On for 20 mixes; edited on the simulated desk only (no RCP / Editor write yet)",
    ),
    Feature(
        "send_pre",
        "Send pre / post fader",
        "Send to, Sends on fader",
        "no",
        "no",
        "app",
        "edit",
        "Simulated by the app",
    ),
    Feature(
        "send_pan",
        "Send pan (stereo AUX)",
        "Send to, Sends on fader",
        "no",
        "no",
        "app",
        "edit",
        "Simulated by the app; stereo pairs",
    ),
    Feature("fx_sends", "Sends to FX 1/2 and mono", "Send to", "RCP", "no", "yes", "view"),
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
        "Patch/Port, used for the USB patch",
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
        "no",
        "yes",
        "view",
        "Stereo read (shown when a USB input carries it); mixes not",
    ),
    Feature(
        "geq",
        "GEQ (AUX 1-8, stereo): 31 bands, 12 adjustable, +-15 dB",
        "GEQ",
        "no",
        "no",
        "no",
        "no",
        "Ring detection names the nearest GEQ band",
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
