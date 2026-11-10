# How to control every StageMix feature on the TF5 remotely

[desk-coverage.md](desk-coverage.md) lists what tf-daemon reads and writes today. This page
covers the rest: for every row of that table, which protocol address controls the feature on a
real TF5, in what units, and how sure we are. The wire format itself (framing, the `01 11`
set-param message, sync, the MMSXLIT dump) is in [guides/tf5_api.md](../guides/tf5_api.md);
this page only adds addresses and per-feature details on top of it.

Sources, with the confidence tags from the guide:

- **[VERIFIED-CAP]**: seen on the wire (the 2026-11-08 TF Editor capture, or tf-daemon against
  the auditorium TF5).
- **[VERIFIED-DUMP]**: the parameter exists with this name, type and wire address in the TF5's
  own dumps (firmware **V4.55**, captured from TF Editor's session; 10 functions, below). The
  address is real. That a write to it is accepted is not verified unless the row says so.
- **[VERIFIED-CODE]**: read in the Ghidra decompile of `tf_editor.exe` (function address given).
- **[EXTERNAL]**: from [BrenekH/yamaha-rcp-docs](https://github.com/BrenekH/yamaha-rcp-docs)
  (TF parameter list from `prminfo`, via the Companion module). Not re-tested here.
- **[INFERRED]**: strong evidence, not proven. **[PENDING]**: unknown; how to find out is given.

Ranges and scaling come from TF Editor's own schema (`mms_<Function>.xml`, embedded in the
binary). Every value on the wire is the raw integer: `raw / scaling = value in unit`.

---

## 1. Two protocols, and which one to use

| | RCP (TCP 49280, text) | TF Editor / StageMix (TCP 50368, binary) |
|---|---|---|
| What it covers | ~120 addresses: names, colours, faders, ONs, sends, pan, DCA, mute masters, scene recall | Everything the editor can do: all of the above plus head-amp, EQ, GEQ, dynamics, delay, FX, cue, recorder, setup |
| Read | `get ADDR x y`, live `NOTIFY set ...` | dump on subscribe, live `01 11` pushes |
| Write | `set ADDR x y value` [EXTERNAL] | `01 11` set-param, origin `0xA0`, echoed in ~10 ms [VERIFIED-CAP for Mixing] |
| Session | none; many clients | at most 3 MMS sessions (`Status/CommSession/MMSSync/SessionNum` max 3); only one `TYPE:Editor` |
| tf-daemon today | read-only (`console/rcp.py`) | `console/editor/`; writes only through `guard.py` |

Rule of thumb: anything RCP lists can be done over RCP with one text line, and that path needs
no sync. Everything else needs the Editor protocol. The Editor protocol is a superset, and its
addresses come from the console's dump, so one generic `write(func, path, value)` covers every
feature below.

### 1.1 RCP in one paragraph [EXTERNAL; NOTIFY / sscurrent VERIFIED-CAP in `console/rcp.py`]

LF-terminated lines. `get MIXER:Current/InCh/Fader/Level 4 0` → `OK get ... 4 0 -1250`.
`set MIXER:Current/InCh/Fader/Level 4 0 -1000` → `OK set ...` (or `OKm`); other clients get
`NOTIFY set ...`. Two 0-based indices: `x` = channel, `y` = sub-index (which mix / FX / matrix).
dB values are dB×100, −32768 = −∞, max 1000. `prminfo N` enumerates the console's address list
with sizes and ranges; `prmnum` gives the count. Scene recall is `ssrecall_ex scene_a <n>` and
the console notifies `sscurrent_ex scene_a <n>`.

The editor binary also contains the SCP client's meter and scene commands: `mtrinfo`, `mtrnum`,
`mtrstart`, `mtrstop`, `sscurrent(_ex)`, `ssinfo(_ex)`, `ssnum(_ex)`, `ssrecall(_ex)`,
`ssupdate(_ex)`, `scpmode`, `devinfo`, `devstatus` [VERIFIED-CODE, strings at file offset
`0xb7621c`–`0xb76b90`]. It uses them for Yamaha I/O racks; whether the TF5 answers all of them
is [PENDING] (try `mtrnum` / `mtrinfo 0` with `nc <desk> 49280`).

### 1.2 The Editor protocol's functions on a TF5 V4.55 [VERIFIED-CAP]

Each function is its own channel (4-character frame tag) with its own subscribe, dump and
set-params. tf-daemon subscribes `Property` (handshake; dropped unparsed) and `Mixing`, then the
others in `tf5.functions` one at a time (default: all ten). Tags, MD5s and the tree come from
`console/editor/tree.py` (`tf5_v455.json`).

| Function | Tag | Descriptor MD5 (V4.55) | Holds |
|---|---|---|---|
| Property | `MPRO` | `3a7c8d4c85f89f1eaa834f96630cec3d` | model, firmware, sessions |
| Mixing | `MMIX` | `8d0d0bdd394a9abb80ab12ed1500eddc` | every channel and bus: names, EQ, GEQ, dynamics, sends, faders, DCA, delay |
| Processing | `MPRC` | `ed786e481a8abee4c76d283374ccc574` | head-amp gain and +48V, output patch |
| FX | `MFX` | `bfa272e1e86bf3eebb24e07b3066526d` | FX 1/2, insert FX |
| Setup | `MSUP` | `a538d99fa5da636d99e01a2f807245ed` | mute masters, cue, monitor, oscillator, recorder setup, recall safe, phantom master |
| Volatile | `MVOL` | `abc686b9aaf785522a219a97fae13808` | cue ON per channel, oscillator ON, console lock, RTA target |
| Status | `MSTS` | `2e5465009cb5c09ce6ca89682acfdcea` | recorder transport and playlist, word clock, USB |
| SceneList | `MSCL` | `34f8de2039cf7651a6db7f159202865d` | scene file names |
| SceneStatus | `MSCS` | `865cba150ed9d5db9f52f8aa6ae418f6` | scene titles, comments, protect, fade time |
| ConsoleSetup | `MCST` | `99c891b8924cb64e5401ffaa51a32ccb` | selected channel, fader banks, sends-on-fader target, user keys |

The schema also has `StaticSetup`, `Security`, `Calibration`, `SceneInfo`, `PresetInfo`,
`ProjectInfo`, `Diagnostics`, `ConsoleStatus`, `Fading` and `Meter`. TF Editor did not subscribe
to these in the capture, and their tags are [PENDING].

### 1.3 Generic recipe: control any parameter [VERIFIED-CAP for Mixing; same code path for the rest, VERIFIED-CODE]

1. Subscribe the function (`01 01`), send a hash compare with its descriptor MD5 and a zero data
   hash (`01 10`), open the bracket (`01 04 01`); the console answers with the dump (`01 14`).
2. Parse the dump (`console/editor/dump.py`: `Dump`). The **wire address** of a parameter is
   `Dump.address("DCA:0/Fader/Level")` → `([10, 1, 0], [0, 0, 0], Prm)`. Never hard-code the
   numbers; they are firmware specific (the MD5 above is the check).
3. Send `01 11 01` with e1 = e2 = the function name, e3 = depth, e4 = path, e5 = array index per
   level, e6 = origin `0xA0`, e7 = value: `0x24` s32 if the parameter is signed, `0x14` u32 if
   unsigned, `0x31` NUL-terminated string for a string parameter.
4. Wait for the echo (same address, origin `0xA0`). If the console rounded the value, the echo
   carries what it used. Changes from the desk or other clients arrive the same way, with origin
   ≠ `0xA0`.

Examples, built with the repo's own codec (`codec.element` / `codec.message` / `codec.frame`)
against the V4.55 dumps:

```
DCA 1 fader to -10.00 dB   Mixing  path [10,1,0]  idx [0,0,0]   value 24:-1000
  4d4d4958 00000062 11 0000005d 011101 08
  11 00000001 00 | 31 00000007 "Mixing\0" | 31 00000007 "Mixing\0" | 12 00000001 0003
  14 00000003 0000000a 00000001 00000000 | 14 00000003 00000000 00000000 00000000
  14 00000001 000000a0 | 24 00000001 fffffc18

Head-amp of INPUT 1 to +40 dB   Processing  path [0,0,0,1]  idx [0,0,0,0]   value 24:4000
  4d505243 00000072 11 0000006d 011101 08
  11 00000001 00 | 31 0000000b "Processing\0" | 31 0000000b "Processing\0" | 12 00000001 0004
  14 00000004 00000000 00000000 00000000 00000001 | 14 00000004 00000000 x4
  14 00000001 000000a0 | 24 00000001 00000fa0

Mute master 1 ON   Setup  path [4,0]  idx [0,0]   value 14:1
  4d535550 00000058 11 00000053 011101 08 ... 12 00000001 0002
  14 00000002 00000004 00000000 | 14 00000002 00000000 00000000 | 14 00000001 000000a0 | 14 00000001 00000001
```

Note the index rule: e5 has one entry per level, so the channel goes at the level of its
collection (`InputChannel:2/DCAGroup:0/Assign` → path `[0,17,0]`, idx `[2,0,0]`), and an array
inside a collection goes at its own level (`Mix:0/GEQ/Band:10/Gain` → path `[4,4,2,0]`,
idx `[0,0,10,0]`).

**Not yet verified:** that the console accepts set-params from a `TYPE:StageMix` client in
functions other than Mixing (and whether some functions refuse writes). The first write to each
new function should be done on a quiet desk and watched for the echo. Also untested: the string
form of e4. The editor's UI binds widgets with paths like `/Setup/Recorder:0/Playback:0/Mode:0`
and `#SEL_CH/Patch:0/Select:0`, which suggests the syntax, but that may be the editor's internal
facade and not the wire [PENDING].

### 1.4 Events (`EEVT`) that drive actions [VERIFIED-CODE `CitrusEventMaker.cpp`, `FUN_00472680`]

Some actions are not parameters but events: `EEVT` class 03, elements `14:[0] 14:[0] 31:name
31:args`, echoed with dir `10`. Arguments are space-separated `KEY:value` tokens (writer
`FUN_0066bb50`, parser `FUN_0066bbe0`; a backslash escapes a space or another backslash in a
value).

| Event (client → console) | Args | What it does |
|---|---|---|
| `RecallScene` | `NUMBER:A05` | recall scene A05 (`FUN_00500820`) |
| `ChangeSelectedCH` | `CHINFO:<decimal channel id>` | select a channel (`FUN_004ffb20`; id: 3.11) |
| `Transport` | `OPERATE:<op> [FILENAME:<f>] [NUMBER:<n>]` | USB recorder transport (`FUN_00500a70`) |
| `PlayList` | `OPERATE:Sort MODE:<m> DIR:<d>` | sort the playlist (`FUN_005006b0`) |
| `PlayList` | `OPERATE:Mark KIND:<k> FILENAME:<f> NUMBER:<n>` | check-mark a song (`FUN_005004e0`) |
| `PlayList` | `OPERATE:ChangeDirectory DIRNAME:<d> NUMBER:<n>` | change folder (`FUN_00500370`) |
| `SetClientType`, `KeepAlive` | see the guide 4.5 | session |

| Event (console → client) | Args |
|---|---|
| `onRecalledScene` | `STATUS:<s> NUMBER:<A05>` (`FUN_004ff520`): a recall happened |
| `ChangeSelectedCH` | `CHINFO:<id>`: the desk's selected channel changed (`FUN_004fef40`) |
| `NotifySyncStatus` | `SCN0INI:n SCN0REQ:n ... LIB0INI:n ...`: scene / library transfer progress |
| `onSetWarningMessage` | warning text for the screen |

`tests/editor/test_guard.py` refuses every event except the handshake, keepalive and (behind
`tf5.scene_recall`, only while a person's recall runs) `RecallScene "NUMBER:[AB]nn"`. Sending
any other is a guard change that needs a deliberate review.

---

## 2. Channel layout and numbering

| StageMix / desk | Editor (Mixing) | Count | RCP |
|---|---|---|---|
| CH 1–40 | `InputChannel:0..39` | 40 | `InCh` x 0–39 |
| ST IN 1L…2R | `StInChannel:0..3` | 4 | `StInCh` x 0–3 |
| FX RTN 1L…2R | `FxRtnChannel:0..3` | 4 | `FxRtnCh` x 0–3 |
| AUX 1–8 (mono) | `Mix:0..7` | 8 | `Mix` x 0–7 |
| AUX 9–20 (stereo pairs) | `StMix:0..11` (AUX 9/10 = `StMix:0,1`) | 12 | `Mix` x 8–19 |
| MATRIX 1–4 | `Matrix:0..3` | 4 | `Mtrx` x 0–3 |
| STEREO L/R | `Stereo:0,1` | 2 | `St` x 0–1 |
| MONO (SUB) | `Mono` | 1 | `Mono` x 0 |
| DCA 1–8 | `DCA:0..7` | 8 | `DCA` x 0–7 |
| FX 1/2, insert FX | `FX` function: `FX:0,1`, `InsFX:0..5` | | none |
| Groups | `Group:0..15`, per-channel `SubGroup[8]` | 16 | none (in the tree; not a StageMix screen) |

The AUX split was checked against the dump: `StMix` labels read `Aux 9/10, Aux 9/10, Aux11/12, ...`
[VERIFIED-DUMP]. Over RCP all 20 AUX are one `Mix` array, and a send's y is 0–19. In Mixing, the
sends are `ToMix:0..7` (AUX 1–8) and `ToStMix:0..11` (AUX 9–20).

Common value encodings [VERIFIED-DUMP schema; fader scale VERIFIED-CAP]:

| Kind | Type | Raw | Meaning |
|---|---|---|---|
| Level (fader, send, DCA) | s16 | −32768, −13800…1000 | −∞, dB×100, max +10 dB |
| Pan / balance | s8 | −63…63 | L63…R63, 0 = centre |
| On / switch | u8 | 0 / 1 | |
| Frequency | u32 | Hz×10 | e.g. 200 = 20 Hz |
| Gain / threshold | s16 | dB×100 | |
| Q | u16 | ×1000 | |
| Time (release, hold, decay, delay) | u32 | ms×1000 | |
| Attack | s8 | ms | |

---

## 3. Feature by feature

Paths are relative to their function. `[N]` = array size. "Wire" is the V4.55 path from the
dump (re-derive it with `Dump.address`).

### 3.1 Home screen

**Channel name and colour.** `Mixing/<Ch>/Label/{Category,Name,Color,Icon}`, strings of 16 / 64
/ 8 / 12 bytes. Input: wire `[0,0,0..3]`. Colours seen: `Blue Orange Purple Pink Red SkyBlue
Green` (`Yellow` and others in the schema). Icons: `DynamicMic WirelessMic Keyboard Mixer Star2
Blank`, ... (the schema's `kind=26` list). RCP: `MIXER:Current/<Ch>/Label/{Name,Color,Icon,Category}`
rw [EXTERNAL]. *Write:* string set-param `0x31` [VERIFIED-DUMP]. The simulator builds every
function from the V4.55 tree (`console/sim_tree.py`), so real Label wire indices (`Name` = 1)
resolve there too.

**Input fader level / ON.** Already written by tf-daemon: `InputChannel:N/Fader/{Level,On}`,
wire `[0,19,0..1]` [VERIFIED-CAP, guide 6.3]. The same `Fader/{Level,On}` exists on every
strip: `StInChannel [1,15,*]`, `FxRtnChannel [2,8,*]`, `Group [3,6,*]`, `Mix [4,7,*]`,
`StMix [5,8,*]`, `Matrix [6,4,*]`, `Stereo [8,5,*]`, `Mono [9,4,1]` (Level) / `[9,4,3]` (On),
`DCA [10,1,*]`. RCP: `MIXER:Current/<Ch>/Fader/{Level,On}` for every strip type [EXTERNAL].

**Input pan.** `InputChannel:N/ToStereo/Pan` s8 wire `[0,15,0]`; `ToStereo/Assign` (to the ST
bus) `[0,15,1]`. RCP: `InCh/ToSt/Pan` (alias `ToStereo/Pan`).

**Stereo fader, ON, balance.** `Stereo:0/Fader/{Level,On}`; balance `Stereo:0/Output/Balance`
s8 wire `[8,6,0]`; also `Output/Phase`, `Output/Gain` (s16 −2400…0, output trim). RCP:
`St/Fader/*`, `St/Out/Balance`.

**AUX / mix master fader and ON.** `Mix:N/Fader/{Level,On}` wire `[4,7,0..1]`, `StMix:N/Fader/*`
wire `[5,8,0..1]` (set both halves of a stereo pair). Balance: `Mix:N/Output/Balance` (meaningful
when two mono mixes are paired), `StMix:N/Output/Balance`. RCP: `Mix/Fader/{Level,On}` x 0–19,
`Mix/Out/Balance`.

**CUE.** Cue is volatile state: `Volatile/Cue/<Ch>[N]/On` u8, one collection per strip type:
`InputChannel [0,1,0]` (plus `KeyIn/On` `[0,1,1,0]`), `StInChannel [0,2,0]`, `FxRtnChannel
[0,3,0]`, `Group [0,4,0]`, `Mix [0,5,0]`, `StMix [0,6,0]`, `Matrix [0,7,0]`, `Stereo [0,9,0]`,
`Mono [0,10,0]`, `DCA [0,11,0]`, `FX [0,12,0]`, `InsFX [0,13,0]`, recorder
`Cue/Recorder/{Record,Playback}/On`. `Volatile/Cue/ActiveCategory` u8 0–9 (which kind of cue is
active; it read 6 in the capture). The console pushes cue changes to clients: CH15 / CH16 cue
off arrived as `MVOL [0,1,0] idx [0,14,0]` / `[0,15,0]`, origin 0xA1 [VERIFIED-CAP], as the
per-level index rule (1.3) predicts. Cue setup:
`Setup/Cue/Mode` (0 = mix cue, 1 = last cue [INFERRED from the TF manual's two modes]),
`Setup/Cue/{Input,Output}/{Point,Trim}`. Not on RCP.

**DCA faders and assignment.** Master: `Mixing/DCA:N/Fader/{Level,On}` wire `[10,1,0..1]`,
labels `DCA:N/Label/*` `[10,0,*]`. Assignment, per source strip: `InputChannel:C/DCAGroup:D/Assign`
u8 wire `[0,17,0]` idx `[C,D,0]`; `StInChannel [1,13,0]`; `FxRtnChannel [2,6,0]`. RCP has the DCA
masters (`DCA/Fader/{Level,On}`, `DCA/Label/*`) but no assignment [EXTERNAL]. DCA roll-out on the
desk: `ConsoleSetup/Common/DCAGroupRollOut/TargetCH` u8 0–16 (local view state).

**Mute groups and master mutes.** Masters: `Setup/MuteMaster[6]/{On,Name}` wire `[4,0]` /
`[4,1]`, idx `[g,0]`. Names on this desk: `INPUT, FX, MUTE 3, MUTE 4, MUTE 5, MUTE 6`
[VERIFIED-DUMP]. Membership: `Mixing/<InputChannel|StInChannel|FxRtnChannel>:C/MuteGroup[4]/Assign`
(input wire `[0,18,0]` idx `[C,g,0]`) and `FX/<FX|InsFX>:n/MuteGroup[2]/Assign`. That channels
have 4 and FX have 2 suggests masters 1–4 are the channel groups and 5–6 the FX groups, but the
index mapping is [INFERRED]; verify by toggling one assignment on the desk and watching the push.
Related: `Setup/MuteSafe/<Ch>[N]/On`, `Volatile/MuteTemporaryOff/<Ch>[N]/On`. RCP:
`MIXER:Current/MuteMaster/On` x 0–5 rw, `MuteMaster/Label/Name` read-only [EXTERNAL].

**Level meters.** Three routes, none finished:

1. YMDP multicast `239.192.0.164:50240`: framing known, block ids grouped by strip type
   (guide section 8); the block → meter-point map and the 1-byte scale are [PENDING]. To finish:
   capture YMDP while sweeping the oscillator (`Setup/Oscillator`, assign to one bus) to
   known levels.
2. RCP meters: the SCP client's `mtrinfo` / `mtrstart <address> <interval>` / `mtrstop`, with
   meter addresses that are in the binary: `MIXER:Current/<InCh|StInCh|FxRtnCh|Mix|Mtrx|St|Mono>/
   {PreHPF,PreEQ,PreFader,PostOn}` [VERIFIED-CODE strings; TF5 support PENDING]. This is the
   cheapest thing to try: `mtrnum`, then `mtrinfo 0..n`, then `mtrstart` one address.
3. Gain reduction is probably YMDP element type `0x02` (guide 8) [INFERRED].

Meter points on the desk: `Setup/Meter/{Input,Output}/Point` u8 0–2, `Setup/Meter/PeakHold/On`.

### 3.2 Sends (SEND tab, Sends on Fader)

All per source strip, in Mixing:

| Send | Path | Input wire | Values |
|---|---|---|---|
| To AUX 1–8 | `<Ch>:C/ToMix:m/{Level,Pan,On,Pre}` | `[0,11,0..3]` idx `[C,m,0]` | Level s16, Pan s8, On u8, Pre u8 (1 = pre) |
| To AUX 9–20 | `<Ch>:C/ToStMix:s/{Level,Pan,On,Pre}` | `[0,12,0..3]` idx `[C,s,0]` | s = 0..11, AUX 9 + s |
| To FX 1/2 | `<Ch>:C/ToFX:f/{Level,On,Pre}` | `[0,13,0..2]` | Pre default 0 (post) |
| To MONO | `<Ch>:C/ToMono/{Level,On}` | `[0,14,0..1]` | |
| Bus → matrix | `<Mix|StMix|Stereo|Mono>:n/ToMatrix:t/{Level,Pan,On}` | Mix `[4,6,*]` | |

Source strips with sends: `InputChannel` (above), `StInChannel` (`ToMix [1,8]`, `ToStMix [1,9]`,
`ToFX [1,10]`, `ToMono [1,11]`), `FxRtnChannel` (`ToMix [2,2]`, `ToStMix [2,3]`, `ToMono [2,4]`),
`Group` (`ToMix [3,3]`, `ToFX [3,4]`) [VERIFIED-DUMP].

- **Pre / post** (desk-coverage: "simulated by the app"): `ToMix/Pre`, `ToStMix/Pre`, `ToFX/Pre`.
  Where pre taps: `Mix:N/SendPoint/Pre` and `StMix:N/SendPoint/Pre` u8 50–100 (100 = pre-fader,
  50 is likely pre-EQ [INFERRED]). RCP: `InCh/ToMix/PrePost` rw [EXTERNAL].
- **Send pan** (desk-coverage: "simulated by the app"): `ToStMix/Pan` (and `ToMix/Pan` for paired
  mono mixes). Whether the pan follows the channel pan is `StMix:N/Signal/PanLink` /
  `Mix:N/Signal/PanLink`. RCP: `InCh/ToMix/Pan` y 8–19 [EXTERNAL].
- **Sends on fader**: the desk's target bus is `ConsoleSetup/Common/SendsOnFader/TargetBus` u8
  0–16 (local view state). A remote client doesn't need it; it writes the send levels directly.

Send levels and ONs are the one place tf-daemon already reads over RCP; writing them is
`set MIXER:Current/InCh/ToMix/Level C m value` over RCP, or the Mixing set-param above.

### 3.3 Input screen

**Head-amp gain and +48V** (desk-coverage: "simulated by the app"). They live on the input
**port**, not the channel, in the Processing function:

| Parameter | Path | Wire | Range |
|---|---|---|---|
| HA gain, INPUT 1–32 | `Processing/BuiltIn/InputPort:P/HeadAmp/Gain` | `[0,0,0,1]` idx `[0,P,0,0]` | s16 −600…6600 = −6…+66 dB |
| +48V, INPUT 1–32 | `Processing/BuiltIn/InputPort:P/HeadAmp/48V` | `[0,0,0,0]` idx `[0,P,0,0]` | u8 |
| HA gain / +48V, slot | `Processing/Slot/InputPort:P/HeadAmp/{48V,Gain}` (64 ports) | `[1,0,0,*]` | same |
| Phantom master | `Setup/PhantomMaster/On` | `[2,0]` | u8 |
| HA control allowed | `Setup/HAControl/On` | `[17,0]` | u8 |

The HA gains in the dump are plausible desk settings (+45, +40, +42 dB ... on INPUT 1–3)
[VERIFIED-DUMP]. Not on RCP for the console itself: the `IO:Current/InCh/{HAGain,48VOn}`
addresses in the binary are for Yamaha I/O racks [VERIFIED-CODE strings], and the TF5's
`prminfo` lists no HA parameter (`console/rcp.py` docstring).

To map a channel to its port, read `InputChannel:C/Patch/Select`. TF Editor builds the HA path
from it (`FUN_0054c040`, `FUN_005a07b0`) [VERIFIED-CODE]:

| `Patch/Select` | Source | Head amp |
|---|---|---|
| 0 | INPUT (local jack) | `Processing/BuiltIn/InputPort:<port>` |
| 1 | USB | none |
| 2 | SLOT (NY64-D card) | `Processing/Slot/InputPort:<port>` |

On the auditorium desk, CH 1–32 read 0 and CH 33–40 read 1 [VERIFIED-DUMP], which fits: CH 33–40
have no local jack and the desk has no card. The channel → port number is `FUN_005a0710` /
`FUN_005a0ff0` (CH n → INPUT n for CH 1–32 [INFERRED]).

**Phase, digital gain.** `InputChannel:C/Input/{Phase,Gain,GainOffset}`, wire `[0,5,0..2]`.
Gain s16 −2400…1000 = −24…+10 dB (CH 28 read −500 in the dump). `GainOffset` (±34 dB) and
`BuiltIn/InputPort/GainOffset` / `Slot/InputPort/GainOffset` (±72 dB, wire `[0,3,0,0]` /
`[0,4,0,0]`) are the gain-compensation offsets [INFERRED]. `Volatile/InputChannel:C/GainUnlink`.
`StInChannel` has `Input/{Phase,Gain,GainOffset}` at `[1,3,*]`.

**Input source and stereo link.** Source is `Patch/Select` above, wire `[0,1,0]`
(StIn `[1,1,0]`). Stereo link is probably `InputChannel:C/Signal/Relation` u8 (wire `[0,2,0]`;
all 0 on this desk) [INFERRED]. `Mix`/`Matrix` have the same `Signal/Relation` for paired buses.

On the real V4.55 tree `Patch` has a single u8 `Select` (the simulator and the coverage table
now use it; an earlier fake modelled a string `Port`). The USB output patch (which input feeds USB out n, `tf5.usb_patch`) is not in any of
the 10 dumped functions; only `Setup/Usb_1_2_Output/Select` (u8 0–7) is. Where the rest lives
is [PENDING] (probably one of the functions TF Editor didn't subscribe to).

**Direct out.** `InputChannel:C/DirectOut/{On,Level,Point}` wire `[0,6,0..2]`; Point u8 (20 on
every channel here).

**Output patch.** `Processing/BuiltIn/OutputPort[16]/Patch/Source` u32, an opaque source ID
(3.11).

### 3.4 EQ screen

**Input PEQ and HPF.** Already written by tf-daemon (guide 5, 6.2). The rest of the PEQ block:

| Parameter | Path | Input wire | Values |
|---|---|---|---|
| HPF slope | `PEQ/Bank/HPF/Slope` | `[0,7,1,2,2]` | u8, schema min = max = 12 dB/oct, so not adjustable on the TF5 |
| 1-knob on / mode / level | `PEQ/Bank/OneKnob/{On,Mode,Depth}` | `[0,7,1,1,0..2]` | On u8; Mode string (`Vocal`, `Intensity` seen; `Loudness` default on buses); Depth u8 0–100 % |
| EQ type | `PEQ/Bank/Type` | `[0,7,1,0]` | string 8, `BOLD` everywhere here. Other values (e.g. `PRECISE` [INFERRED from the TF manual]) PENDING |
| Pre-1-knob copy | `PEQ/OneKnobBackup/...` | | the bands as they were before 1-knob; the console keeps it, don't write |

`OneKnob/Mode` and `Depth` are in the dump but not read by tf-daemon (coverage note). They are
plain set-params like the bands.

**Stereo / AUX / matrix / mono PEQ.** Same `PEQ` block shape on every strip, with two differences:
no separate HPF block (instead `BandL/HPFOn` and `BandL/Type` 2 = HPF), and the number of mid
bands. `Mono` additionally has `PEQ/Bank/LPF/{On,Frequency,Slope}` (slope 6–24 dB/oct).

| Strip | PEQ at | Mid bands |
|---|---|---|
| `StInChannel` | `[1,4,...]` | 0 (BandL, BandH) |
| `FxRtnChannel` | `[2,1,...]` | 0 |
| `Group` | `[3,1,...]` | 2 |
| `Mix` | `[4,3,...]` | 2 |
| `StMix` | `[5,3,...]` | 2 |
| `Matrix` | `[6,2,...]` | 2 |
| `Stereo` | `[8,1,...]` | 2 |
| `Mono` | `[9,1,...]` | 0, plus `LPF` |

The band-type enum and the curve maths are in guide 5 and 6.1 (they cover these bands too).

**GEQ (AUX 1–8, stereo).** Only `Mix` and `Stereo` have one [VERIFIED-DUMP]:

| Parameter | Path | Wire (Mix) | Values |
|---|---|---|---|
| GEQ on | `Mix:N/GEQ/On` | `[4,4,0]` | u8 |
| L/R link | `Mix:N/GEQ/LRLink` | `[4,4,1]` | u8 (Stereo default 1) |
| Band gain | `Mix:N/GEQ/Band:b/Gain` | `[4,4,2,0]` idx `[N,0,b,0]` | s16 −1500…1500 = ±15 dB; b 0–30 = 20 Hz…20 kHz in ⅓ octaves |

Stereo is `[8,2,...]`. The TF limit of 12 adjustable bands is enforced by the desk UI. Whether
the console refuses a 13th non-zero band from a client is [PENDING]; a client should enforce the
limit itself. AUX 9–20 (`StMix`) have no GEQ in the tree. The desk's "GEQ on fader" view is
`ConsoleSetup/Common/GEQOnFader/*` (local view state).

### 3.5 Dynamics

**Gate** (CH 1–40 only; `InputChannel:C/Gate`, wire `[0,8,...]`):

| Parameter | Path | Wire | Values |
|---|---|---|---|
| On | `Gate/On` | `[0,8,0]` | u8 |
| Type | `Gate/Bank/Type` | `[0,8,1,0]` | string 16; `GATE` here (`DUCKING` [INFERRED from the TF manual] PENDING) |
| Threshold | `Gate/Bank/Gate/Threshold` | `[0,8,1,1,0]` | s16 −7200…0 dBFS×100 |
| Attack | `.../Attack` | `[0,8,1,1,1]` | s8 0–120 ms |
| Range | `.../Range` | `[0,8,1,1,2]` | s16 −32768 (−∞)…0 dB×100 |
| Hold | `.../Hold` | `[0,8,1,1,3]` | u32 20…1960000 = 0.02 ms…1.96 s |
| Decay | `.../Decay` | `[0,8,1,1,4]` | u32 3340…42700000 = 3.34 ms…42.7 s |

**Compressor** (`<Ch>/Comp`, input wire `[0,9,...]`; also on `Group [3,2]`, `Mix [4,5]`,
`StMix [5,4]`, `Stereo [8,3]`, `Mono [9,2]`):

| Parameter | Path | Input wire | Values |
|---|---|---|---|
| On | `Comp/On` | `[0,9,0]` | u8 |
| Type | `Comp/Bank/Type` | `[0,9,1,0]` | string 16; `COMP` here (de-esser type name PENDING) |
| 1-knob on / amount | `Comp/Bank/OneKnob/{On,Depth}` | `[0,9,1,1,0..1]` | u8, Depth 0–100 % |
| Threshold | `Comp/Bank/Compressor/Threshold` | `[0,9,1,2,0]` | s16 −5400…0 dBFS×100 |
| Attack | `.../Attack` | `[0,9,1,2,1]` | s8 0–120 ms |
| Release | `.../Release` | `[0,9,1,2,2]` | u32 ms×1000, 3.34 ms…42.7 s |
| Ratio | `.../Ratio` | `[0,9,1,2,3]` | u8 ×10, 10…255 = 1.0…25.5 (255 likely ∞ [INFERRED]) |
| Knee | `.../Knee` | `[0,9,1,2,4]` | string 8: `SOFT-2` (inputs), `HARD` (buses); others PENDING |
| Make-up gain | `.../Gain` | `[0,9,1,2,5]` | s16 0…1800 = 0…18 dB |
| De-esser | `Comp/Bank/DeEsser/{Threshold,Frequency,Q,Type}` | `[0,9,1,3,0..3]` | inputs only; Freq u32 800…16000 Hz ×10, Q ×1000, Type `BPF` |

desk-coverage.md's note "ranges in console/strip.py SPECS: Yamaha's shared ones, to confirm on the
TF5" can now be answered from these tables; they are the TF5's own schema.

**Stereo-input extras** (`StInChannel`): `Leveler/{On,Threshold,Gain}` `[1,6,*]`,
`Ducker/{On,Threshold,Range,Release}` `[1,7,0..3]` and `Ducker/KeyIn_TF/Source` u32 (source ID,
default `0x00050000`). **Auto mixer:** `InputChannel:C/AutoMixer/{On,Group,Weight}` `[0,10,*]`,
Weight s16 −10000…1500; `Setup/Automixer/Master[2]/Bypass`.

### 3.6 Scenes

| What | Where | Notes |
|---|---|---|
| Scene list | `SceneStatus/Bank[2]/Info[100]/{Title,Comment,Protect,DataType,TimeStamp/*,Fading/{FadeTime,On}}` | bank 0 = A, 1 = B; FadeTime u16 s×10 [VERIFIED-DUMP] |
| Scene files | `SceneList/Bank[2]/Info[100]/FileName` | the `.tfs` file name per slot |
| Current scene | `Setup/Scene/LastScene` (string, `A13`), `Setup/Scene/EditFlag` (edited since recall), `ConsoleSetup/Scene/SelectedScene` | [VERIFIED-DUMP] |
| Recall | Editor: `EEVT RecallScene "NUMBER:A13"` [VERIFIED-CODE]; RCP: `ssrecall_ex scene_a 12` [EXTERNAL] | the console answers `onRecalledScene "STATUS:.. NUMBER:A13"`; RCP clients get `NOTIFY sscurrent_ex` |
| Store | no event in the editor's sync thread | TF Editor stores via its scene memory and file transfer (`d000` SceneA / `d010` SceneB `02 01` announce + `02 03` 2048-byte chunks, guide 10); RCP `ssupdate(_ex)` is a likely candidate [PENDING] |
| Recall safe | `Setup/RecallSafe/TF/<Ch>[N]/{SafeOn,Target,WithSend}` | Target is a bit mask [INFERRED] |
| Fading | `SceneInfo/Fading/*`, `Fading` function | per-strip fade enables and progress |

The scene NUMBER parser (`FUN_004ff130`) takes the bank from the first character (`A`/`a` = 0,
`B`/`b` = 1) and the number from the rest. tf-daemon already treats a recall as "re-read
everything" (RCP `sscurrent`); a recall over the Editor protocol pushes the changed parameters as
`01 11` and sends `onRecalledScene`. The RCP scene index base (`ssrecall_ex scene_a 12` vs scene
A13) is [PENDING]: read `ssnum_ex` / `sscurrent_ex` once and compare with `Setup/Scene/LastScene`.
`guard.py` allows `RecallScene` only with `tf5.scene_recall` on, only during a person's request
(`POST /api/tf5/event/recall_scene`); the simulator answers `STATUS:OK`, the real desk's STATUS
token is [PENDING].

### 3.7 FX, insert FX, delay

| What | Path (FX function) | Wire | Values |
|---|---|---|---|
| FX 1/2 bypass | `FX:n/Bypass` | `[0,0]` idx `[n,0]` | u8 |
| FX mix balance | `FX:n/MixBalance` | `[0,1]` | u8 0–100 % |
| Effect type | `FX:n/Param/{PluginName,LibraryName}` | `[0,2,0..1]` | string 17: `Echo`, `MonoDelay` here |
| Effect parameters | `FX:n/Param/Bank/Value[64]` | `[0,2,2,0]` idx `[n,0,0,i]` | s32 each; meaning depends on the plugin [PENDING] |
| Insert FX | `InsFX:0..5/...` same shape | `[1,*]` | `ReverbStage`, `StereoDelay` seen; Bypass default 1 |

FX returns are channels (`FxRtnChannel`, section 2). Changing an effect's type is probably
not a `PluginName` write but a library recall [INFERRED]. Parameter indices per plugin can be
mapped by changing one knob on the desk and watching which `Value:i` is pushed. Tap tempo:
`ConsoleSetup/Tap/Interval` u16 ms.

**Delay.** Only `StInChannel:n/Delay` (`[1,5,*]`) and `Matrix:n/Delay` (`[6,3,*]`):
`{On, Time u32 ms×1000 (1…1000 ms), TimeOffset s32, LRLink}`. Input channels have no delay in
the V4.55 tree. The display unit is `ConsoleSetup/Preference/Delay/{Scale,Frame}`.

### 3.8 USB recorder

| What | Where | Values |
|---|---|---|
| Transport state | `Status/Recorder/Transport/{State,RecState,PlayContinue}` | State u8 0–3, RecState u8 0–4 (enums PENDING) |
| Now playing | `Status/Recorder/PlayList/Current/{SongNo,FileName,PlayTime,PlayPosition}` | seconds |
| Playlist | `Status/Recorder/PlayList/{DeviceKind,DirPath,TotalSongs,State,Sort/*,Song[150]/*}` | |
| Levels / format / mode | `Setup/Recorder/Record/{Level,Format,Source/Select}`, `Setup/Recorder/Playback/{Level,Mode,AutoScroll}` | Format `WAV`; Mode 0–3 |
| Transport control | `EEVT Transport "OPERATE:<op> FILENAME:<f> NUMBER:<n>"` | event format [VERIFIED-CODE]; the `<op>` strings are [PENDING] |
| Recorder cue | `Volatile/Cue/Recorder/{Record,Playback}/On` | |

The `<op>` values come from the recorder view's `reqTransportCommand(int, QString, int)`; the
user-defined-key list next to them names `Play/Pause, Stop, Next, Previous, Rec, Auto Rec,
Rec & Start`. Capture TF Editor pressing each button once to get the exact strings.

### 3.9 Other desk state a remote can use

| What | Where |
|---|---|
| Oscillator | `Setup/Oscillator/{Level,Mode,HPF/*,LPF/*,SineWave/Frequency,BurstNoise/*,SendAssign/*}`, on/off `Volatile/Oscillator/On` |
| Monitor | `Setup/Monitor/{Out/On,Level/Level,Mode/Mono,Source/Select,Delay/*}` |
| Selected channel | `ConsoleSetup/Common/SelectedCH` (id, 3.11), `EEVT ChangeSelectedCH` |
| RTA source | `Volatile/Meter/RTA/Target` (id, 3.11; TF Editor wrote it during the capture and the console echoed it [VERIFIED-CAP]) |
| Console lock | `Volatile/ConsoleLock/Lock` |
| Custom fader bank | `ConsoleSetup/FaderBank/CustomBankTF5/ChStrip[32]/Source` (ids), `FaderBank/SelectedBank` |
| User keys, knobs, footswitch | `ConsoleSetup/{UserDefinedKeys,UserDefinedKnobs,FootSwitch}/...` (strings like `MUTE MASTER` / `GROUP 3`) |
| Model, firmware, sessions | `Property/Specification/Unit/*`, `Property/Status/CommSession[3]/{ClientType,SyncDirection}` |
| Recorder / USB / CPU status | `Status/{USBToHost,USBStorage,CPULoad,WordClock}/*` |

`Setup/CommSession/MobileMix/Password` (StageMix's password; RCP `MIXER:Setup/MonitorMix/Password`)
and `Property/UserAccount/Unit/Password` can be read by any client. Treat them as secrets:
never log or show them (`guard.py` already drops the Property dump unparsed).

### 3.10 Copy / paste / compare / default

These are client-side features. The protocol has no copy command: read the block (e.g. every
`PEQ/Bank/...` of a channel), keep it, and write it back parameter by parameter. Defaults are the
schema's `def=` values (e.g. PEQ band Q 700, BandL freq 125 Hz, HPF 80 Hz). The desk's library
recall (`Library/LastRecallPreset/Title` shows the last one) goes through the `d020` library
file transfer and is [PENDING].

### 3.11 Channel / source IDs

Several u32 parameters name a channel by an opaque ID instead of an index:
`ConsoleSetup/Common/SelectedCH`, `FaderBank/Bank[5]/SelectedCH`,
`FaderBank/CustomBankTF5/ChStrip[32]/Source`, `Volatile/Meter/RTA/Target`,
`StInChannel/Ducker/KeyIn_TF/Source`, `Processing/BuiltIn/OutputPort/Patch/Source`, and the
`CHINFO:` argument of `ChangeSelectedCH` (`%d`, decimal). On this desk the custom bank reads
`0x0C0316C9, 0x0C1316C9, ... 0x0DF316C9` for consecutive strips, i.e. CH k is
`0x0C0316C9 + (k − 1) × 0x00100000`, so the RTA target `0x0C0316C9` is CH 1 and the selected channel
`0x0CA316C9` is CH 11 [INFERRED from one desk].
Other strip types use other high bits (`0x140F7609`, `0x180B110C` appear in the bank). Until the
encoding is decoded, copy IDs the console reports instead
of constructing them.

---

## 4. Coverage table → how to complete each row

| desk-coverage row | Protocol to use | Address (section) | Blocking unknown |
|---|---|---|---|
| Channel name and colour | Editor or RCP | `Label/*` (3.1) | none |
| Input fader / ON | done | | |
| Input pan | Editor or RCP | `ToStereo/Pan` (3.1) | none |
| Stereo fader, ON, balance | Editor or RCP | `Stereo/Fader`, `Output/Balance` (3.1) | none |
| AUX / mix fader and ON | Editor or RCP | `Mix`, `StMix` `Fader` (3.1) | none |
| CUE | Editor | `Volatile/Cue/*` (3.1) | write acceptance in Volatile |
| DCA faders and assignment | Editor (RCP for masters) | `DCA/Fader`, `DCAGroup/Assign` (3.1) | none |
| Mute groups and master mutes | Editor or RCP | `Setup/MuteMaster`, `MuteGroup/Assign` (3.1) | group-index mapping, write acceptance in Setup |
| Level meters | RCP `mtrstart` or YMDP | 3.1 | meter mapping and scale |
| Sends level / ON | Editor or RCP | `ToMix`, `ToStMix` (3.2) | none |
| Send pre / post | Editor or RCP | `Pre` / `PrePost` (3.2) | none |
| Send pan | Editor or RCP | `ToStMix/Pan` (3.2) | none |
| Sends to FX / mono | Editor or RCP | `ToFX`, `ToMono` (3.2) | none |
| Head-amp gain, +48V, phase, digital gain | Editor | `Processing/.../HeadAmp`, `Input/*` (3.3) | write acceptance in Processing |
| Input source, stereo link | Editor | `Patch/Select`, `Signal/Relation` (3.3) | link encoding; USB output patch location |
| Input PEQ / HPF | done | | |
| HPF slope | read only | fixed 12 dB/oct (3.4) | none (not adjustable) |
| 1-knob EQ | Editor | `PEQ/Bank/OneKnob/*` (3.4) | Mode strings beyond the ones seen |
| EQ type | Editor | `PEQ/Bank/Type` (3.4) | other type strings |
| Stereo / AUX / matrix PEQ | Editor | per-strip `PEQ` (3.4) | none |
| GEQ | Editor | `Mix`/`Stereo` `GEQ` (3.4) | console behaviour past 12 bands |
| Gate | Editor | `Gate/*` (3.5) | Type strings |
| Compressor | Editor | `Comp/*` (3.5) | Knee / Type strings |
| Scene list, recall, store | Editor event or RCP | 3.6 | store path; RCP index base |
| FX, insert FX, delay | Editor | FX function, `Delay` (3.7) | plugin parameter maps |
| USB recorder transport | Editor event | 3.8 | `OPERATE` strings |
| Copy / paste / compare | client side | 3.10 | none |

### What each new write needs in tf-daemon

The foundation exists (2026-11-10): every function is subscribed and readable
(`TFEditorClient.get(path, func)`, `GET /api/tf5/param`), the simulator serves the real V4.55 tree
(`console/sim_tree.py`), and `guard.FAMILIES` lists what may be written, each family behind its own
`tf5.*_write` switch, limits from the schema (`console/editor/tree.py` `spec()`), written with
`TFEditorClient.write(changes, func=...)` / `POST /api/tf5/param`. So a feature only needs:

1. A page or route that reads with `get` and writes with `write` on a person's confirmed edit.
2. If its parameter isn't in a family yet: add it to `guard.FAMILIES` with tests in
   `tests/editor/test_guard.py` (a deliberate review, never a loosened assertion).
3. Confirm it on the desk: one write, watch the echo, and note it here as [VERIFIED-CAP].

---

## 5. Remaining unknowns, cheapest first

1. RCP meters: `mtrnum`, `mtrinfo`, `mtrstart` against the TF5 (one `nc` session).
2. Set-param acceptance outside Mixing for `TYPE:StageMix`: one HA-gain write (Processing) and one
   mute-master write (Setup) on a quiet desk.
3. One TF Editor capture pressing each recorder button, storing a scene, changing gate/comp type,
   EQ type and knee, and toggling one mute-group assignment of a channel and of an FX. That
   fills `OPERATE` values, the store path, the type strings and the mute-group mapping.
4. YMDP meter mapping (oscillator sweep, guide 8).
5. Where the USB output patch lives: subscribe the remaining functions (`StaticSetup`,
   `ConsoleStatus`, ...; tags PENDING) or watch TF Editor's patch screen.
6. FX plugin parameter maps (`Value[64]` per plugin).

### Ghidra functions used for this page (`tf_editor.exe`, Ghidra VA)

| Function | Role |
|---|---|
| `0x472680` | incoming event dispatcher (`KeepAlive`, `ChangeSelectedCH`, `NotifySyncStatus`, `onSetClientType`, `onRecalledScene`, `onSetWarningMessage`) |
| `0x4730c0` | device-control commands → events (`Scene`/Recall, `Transport`, `PlayList`) |
| `0x500820` | builds `RecallScene "NUMBER:..."` |
| `0x500a70` | builds `Transport "OPERATE:.. FILENAME:.. NUMBER:.."` |
| `0x5006b0` / `0x5004e0` / `0x500370` | build `PlayList` Sort / Mark / ChangeDirectory |
| `0x4ffb20` / `0x4fef40` | send / receive `ChangeSelectedCH "CHINFO:%d"` |
| `0x4ff520` / `0x4ff130` | `onRecalledScene` handler / scene-number parser (A/B bank) |
| `0x66bb50` / `0x66ba90` / `0x66bbe0` | `KEY:value` writer / key writer / tokenizer |
| `0x54c040`, `0x5a07b0` | channel → head-amp path from `Patch/Select` (0 BuiltIn, 2 Slot) |
