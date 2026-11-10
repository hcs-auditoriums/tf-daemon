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
| Stereo fader, ON, balance | Home (overview strip) | RCP | no | yes | view | In the sidebar when a USB input carries the stereo bus |
| AUX / mix fader and ON | Sends on fader (overview strip) | no | no | no | no | Only mix names are read (RCP) |
| CUE | Home (fader) | no | no | no | no |  |
| DCA faders and assignment | Home, Custom fader bank | no | no | no | no | DCAGroup is in the Editor dump; parameter names not recorded yet |
| Mute groups and master mutes | Utility | no | no | no | no | MuteGroup is in the Editor dump; parameter names not recorded yet |
| Level meters | Home (meters) | no | no | no | no | YMDP UDP framing known (guides/tf5_api.md section 8); block-to-meter mapping pending |
| Sends to AUX mixes: level, ON | Send to, Sends on fader | RCP | no | yes | edit | ToMix/Level, ToMix/On for 20 mixes; edited on the simulated desk only (no RCP / Editor write yet) |
| Send pre / post fader | Send to, Sends on fader | no | no | app | edit | Simulated by the app |
| Send pan (stereo AUX) | Send to, Sends on fader | no | no | app | edit | Simulated by the app; stereo pairs |
| Sends to FX 1/2 and mono | Send to | RCP | no | yes | no | Read but not shown since the sidebar's sends moved to the SEND tab (AUX mixes only) |
| Head-amp gain, +48V, phase, digital gain | Input | no | no | app | edit | Simulated by the app until the Editor dump's Input names are recorded; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Input source (INPUT / USB / SLOT), stereo link | Input | Editor | no | yes | view | Patch/Select (0 INPUT, 1 USB, 2 SLOT): read for the USB patch |
| Input PEQ: on, 4 bands type / freq / gain / Q, LPF on | EQ | Editor | Editor (tf5.eq_write) | yes | edit | Ranges: guard.WRITE_LIMITS |
| Input HPF on and frequency | EQ | Editor | Editor (tf5.eq_write) | yes | edit |  |
| HPF slope | EQ | Editor | no | yes | view |  |
| 1-knob EQ (Vocal / Intensity / Loudness, level) | EQ | Editor | no | yes | view | OneKnob/On read; Mode / Depth in the dump, not read |
| EQ type (e.g. BOLD) | EQ | Editor | no | yes | no |  |
| Stereo / AUX / matrix PEQ | EQ | Editor | no | yes | view | Stereo read (shown when a USB input carries it); mixes not |
| GEQ (AUX 1-8, stereo): 31 bands, 12 adjustable, +-15 dB | GEQ | no | no | no | no | Ring detection names the nearest GEQ band |
| Gate: on, threshold, range, attack, hold, decay | Gate | no | no | app | edit | Simulated by the app until the Gate names are recorded; CH 1-32; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Compressor: on, 1-knob, threshold, ratio, attack, release, gain, knee | Comp | no | no | app | edit | Simulated by the app until the Comp names are recorded; 1-knob amount only; ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the TF5 |
| Scene list, recall, store | Scene | no | no | no | no | A recall is noticed (RCP sscurrent) and everything re-read; RecallScene is refused by the guard |
| FX 1/2, insert FX, delay | FX, Delay | no | no | no | no |  |
| USB recorder transport | Recorder | no | no | no | no |  |
| Copy / paste / compare / default per screen | Every screen's menu | - | - | - | edit | EQ: copy, paste, A/B, gain flat, undo (page side) |
<!-- coverage:end -->
