# What tf-daemon reads and writes on the TF5

tf-daemon is meant to replace TF StageMix, Yamaha's iPad app for the TF series. This page lists
every StageMix feature and how far tf-daemon has it. Each row says:

- **Read**: how the server reads it from a real desk. RCP is Yamaha's remote control protocol
  (TCP 49280, read-only here). Editor is the TF Editor protocol (TCP 50368), the one StageMix
  uses. The wire details are in [guides/tf5_api.md](../guides/tf5_api.md).
- **Write**: what can change it on a real desk, and the config switch that allows it. Every write
  goes through `tf_daemon/console/editor/guard.py`, which refuses anything not listed there.
- **Simulated**: whether the demo / test simulator models it (`tf_daemon/console/sim*.py`).
  `app` means the application keeps it in its own simulated store until the desk protocol for it
  is done: the page and API work in demo mode, and say so on a real desk.
- **Page**: what the pages offer: `edit`, `view` or `no`.

To add a feature's protocol: record its parameter names from a real desk (the read-only probe,
`python -m tf_daemon.console.editor`, dumps the Mixing tree), read it in
`console/editor/dump.py`, model it in `console/sim_editor.py`, and if it is to be written,
add it to `guard.py` deliberately (with tests in `tests/editor/test_guard.py`). The address,
type and range of every feature still missing are in
[desk-coverage-howto-complete.md](desk-coverage-howto-complete.md).

The table is generated from `tf_daemon/console/coverage.py` (also served at
`GET /api/tf5/coverage`); edit that file and run `python -m tf_daemon.console.coverage`.

<!-- coverage:begin -->
| Feature | StageMix screen | Read | Write | Simulated | Page | Notes |
|---|---|---|---|---|---|---|
| Channel name and colour | Home (CH name) | RCP + Editor | no | yes | view |  |
| Input fader level | Home (fader) | RCP + Editor | Editor (tf5.fader_write) | yes | edit | Input channels only |
| Input channel ON | Home (fader) | RCP + Editor | Editor (tf5.fader_write) | yes | edit | Input channels only |
| Input pan | Home (CH name) | RCP | no | yes | view | ToStereo/Pan |
| Stereo fader, ON, balance | Home (overview strip) | RCP + Editor | Editor (tf5.mix_write) | yes | edit | Mix page (console/buses.py; L and R set together); also in the sidebar when a USB input carries it |
| AUX / mix fader and ON | Sends on fader (overview strip) | Editor | Editor (tf5.mix_write) | yes | edit | Mix page: AUX 1-8 (Mix), AUX 9-20 as pairs (StMix, both halves), balance; MATRIX and MONO too |
| CUE | Home (fader) | Editor | Editor (tf5.mix_write) | yes | edit | Mix page: Volatile/Cue per strip; Cue/ActiveCategory read; write acceptance in Volatile unverified |
| DCA faders and assignment | Home, Custom fader bank | Editor | Editor (tf5.mix_write) | yes | edit | Mix page: DCA 1-8 masters and labels, DCAGroup assignment of CH / ST IN / FX RTN |
| Mute groups and master mutes | Utility | Editor | Editor (tf5.mix_write) | yes | edit | Mix page: Setup/MuteMaster on (names read), MuteGroup assignment; group -> master mapping inferred (channel g = master g, FX = masters 5/6); write acceptance in Setup unverified |
| Level meters | Home (meters) | RCP (mtr, unverified on a real TF5) | no | yes | view | Input channels + stereo bus, console/meters.py (tf5.meters); YMDP mapping pending (guide section 8) |
| Sends to AUX mixes: level, ON | Send to, Sends on fader | RCP + Editor | Editor (tf5.mix_write) | yes | edit | SEND tab from the desk once the editor protocol is synced (console/sends.py: ToMix / ToStMix, a stereo pair on both halves); RCP before |
| Send pre / post fader | Send to, Sends on fader | Editor | Editor (tf5.mix_write) | yes | edit | ToMix / ToStMix / ToFX Pre; the tap point (SendPoint/Pre) is read, not written |
| Send pan (stereo AUX) | Send to, Sends on fader | Editor | Editor (tf5.mix_write) | yes | edit | ToStMix Pan on both halves of a pair; Signal/PanLink read |
| Sends to FX 1/2 and mono | Send to | RCP + Editor | Editor (tf5.mix_write) | yes | edit | SEND tab rows FX 1/2 (level, ON, pre) and MONO (level, ON) |
| Head-amp gain, +48V, phase, digital gain | Input | no | no | app | edit | Simulated by the app until the Editor dump's Input names are recorded; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Input source (INPUT / USB / SLOT), stereo link | Input | Editor | no | yes | view | Patch/Select (0 INPUT, 1 USB, 2 SLOT): read for the USB patch |
| Input PEQ: on, 4 bands type / freq / gain / Q, LPF on | EQ | Editor | Editor (tf5.eq_write) | yes | edit | Ranges: guard.WRITE_LIMITS |
| Input HPF on and frequency | EQ | Editor | Editor (tf5.eq_write) | yes | edit |  |
| HPF slope | EQ | Editor | no | yes | view |  |
| 1-knob EQ (Vocal / Intensity / Loudness, level) | EQ | Editor | no | yes | view | OneKnob/On read; Mode / Depth in the dump, not read |
| EQ type (e.g. BOLD) | EQ | Editor | no | yes | no |  |
| Stereo / AUX / matrix PEQ | EQ | Editor | Editor (tf5.processing_write) | yes | edit | Mix page (console/busproc.py): ST, AUX, MATRIX, MONO with the desk's curve; a stereo bus's halves set together; MONO's LPF shown, not drawn |
| GEQ (AUX 1-8, stereo): 31 bands, 12 adjustable, +-15 dB | GEQ | Editor | Editor (tf5.processing_write) | yes | edit | Mix page; the 12-band limit enforced here (the console's own behaviour past 12 pending); ST per side unless L/R linked; the ring list links each ring to its band |
| Gate: on, threshold, range, attack, hold, decay | Gate | no | no | app | edit | Simulated by the app until the Gate names are recorded; CH 1-32; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Compressor: on, 1-knob, threshold, ratio, attack, release, gain, knee | Comp | no | no | app | edit | Simulated by the app until the Comp names are recorded; 1-knob amount only; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Scene list, recall, store | Scene | no | no | no | no | A recall is noticed (RCP sscurrent) and everything re-read; RecallScene is refused by the guard |
| FX 1/2, insert FX, delay | FX, Delay | no | no | no | no |  |
| USB recorder transport | Recorder | no | no | no | no |  |
| Copy / paste / compare / default per screen | Every screen's menu | - | - | - | edit | EQ: copy, paste, A/B, gain flat, undo (page side) |
<!-- coverage:end -->
