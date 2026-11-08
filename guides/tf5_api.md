# Yamaha TF Editor — EQ & Control Protocol Guide (for agents)

Goal: read, write and **live-monitor** TF-series console parameters (especially EQ) the way TF Editor does.

- Target: TF5 "Hartland HS TF" at **10.99.40.6** (MAC 00:a0:de:5b:c4:8a). Editor PC 10.99.40.20.
- Editor binary: `C:\Program Files (x86)\Yamaha\TF Editor\tf_editor.exe` (32-bit, Qt5, 2021-02-24, Jenkins `Citrus-git-dev_v4.0`). TF codename = **Citrus**.
- Capture: `C:\Users\soren.zaiser\OneDrive - Essium LLC\Desktop\tf5-editor.pcapng` (76 s). It contains:
  - discovery
  - 2 failed password attempts (TCP streams 59, 63)
  - 1 successful session (stream 65, t=58.3–76.9 s)
  - console-side mute changes

Confidence tags:
- **[VERIFIED-CAP]** = seen on the wire in the capture.
- **[VERIFIED-CODE]** = read in the Ghidra decompile (function addresses given).
- **[INFERRED]** = strong evidence, not proven.
- **[PENDING]** = unknown.

Address note: Ghidra VA = file offset + `0x401a00` for `.rdata` strings (`tf_editor_strings.txt` lists file offsets).

---

## 1. TL;DR

| Need | How | Status |
|---|---|---|
| Find console | UDP broadcast **54330**, "YSDP" | VERIFIED-CAP |
| Control session | TCP **50368**, tagged TLV frames (`MMIX` = Mixing, …) | VERIFIED-CAP + CODE |
| **Read all EQ** | Subscribe `Mixing` → console sends **MMSXLIT** dump (descriptor tree + raw values) → `tools/mmsxlit.py` | VERIFIED-CAP (ch1 EQ decoded) |
| **Live EQ updates from other controllers** | Console pushes `01 11` set-param messages; origin ≠ 0xA0 means "someone else changed it" | VERIFIED-CAP (Fader/On) + CODE (all types) |
| Write a param | Send `01 11` with origin 0xA0; console echoes it | VERIFIED-CAP + CODE |
| Value encoding | signed → `0x24` int32 BE, unsigned → `0x14` u32 BE, string → `0x31`; count = 1 | VERIFIED-CODE |
| Sync direction | `SYNCDIR:1` = ClientToServer (**overwrites console**), `SYNCDIR:2` = ServerToClient | VERIFIED-CODE |
| Client types | `TYPE:Editor` (only one allowed) or `TYPE:StageMix` | VERIFIED-CODE |
| Keepalive | `EEVT` "KeepAlive" request/echo | VERIFIED-CAP |
| Meters | UDP multicast **239.192.0.164:50240**, "YMDP" | VERIFIED-CAP + CODE (framing); block meaning PENDING |
| Text protocol (SCP) | Present in the binary, **no EQ addresses** | VERIFIED |
| Password | **Never sent.** Editor reads it from the console's Property data and compares locally | VERIFIED-CODE |

---

## 2. Files in this folder

| Path | What |
|---|---|
| `tools/mpro.py` | Decode the TCP 50368 stream from a tshark CSV: `python -I tools/mpro.py capture/tcp50368.csv 65 [--full]` |
| `tools/mmsxlit.py` | Parse an MMSXLIT dump: every param with its **wire path** + current value; `resolve(root, path, idx)`. `python -I tools/mmsxlit.py capture/dump_MMIX.bin --grep PEQ --values` |
| `tools/ymdp.py` | Decode meter multicast: `python -I tools/ymdp.py capture/mcast50240.csv --values 12` |
| `tools/DumpBy.java` | Ghidra headless decompile helper. Arg forms: `s:` string, `b:` raw string bytes, `x:` scalar, `f:` addr, `r:` callers of addr, `g:start:end` range, `m:addr:len` hexdump |
| `tools/DumpNet.java`, `flatten.py`, `strs.py` | Earlier helpers (winsock decompile, XML flatten, string extract) |
| `capture/` | `tcp50368.csv`, `s65.txt` (decoded session), `dump_<TAG>.bin` (MMSXLIT per function), `ysdp.csv`, `mcast50240.csv` |
| `decomp/*.c` | Decompile output: `sync*.c` (client sync thread), `cnp*.c`/`all_cnp.c` (comctrl protocol), `eqtype.c`, `ymdp*.c` |
| `gproj/` | Analyzed Ghidra project |
| `params/mms_*.txt` | Flattened editor XML schema (ranges, units, scaling) |

Re-export from a new capture (adjust `CLIENT_IP` in `mpro.py` if the PC differs):
```bash
tshark -r X.pcapng -Y "tcp.port==50368 && tcp.len>0" -T fields -E separator=, \
  -e frame.number -e frame.time_relative -e tcp.stream -e ip.src -e tcp.len -e tcp.payload > tcp50368.csv
tshark -r X.pcapng -Y "udp.srcport==50240" -T fields -E separator=, \
  -e frame.number -e frame.time_relative -e udp.length -e udp.payload > mcast50240.csv
```

Run Ghidra scripts against the saved project from **PowerShell**. cmd-from-Git-Bash breaks the quoting.
```powershell
& "$env:USERPROFILE\Downloads\ghidra_12.0.4_PUBLIC\support\analyzeHeadless.bat" "C:\Users\soren.zaiser\yamaha-tf-re\gproj" tf `
  -process tf_editor.exe -noanalysis -readOnly -scriptPath "C:\Users\soren.zaiser\yamaha-tf-re\tools" `
  -postScript DumpBy.java out.c 0 f:0066fb50
```

---

## 3. Discovery — YSDP, UDP 54330 [VERIFIED-CAP]

The client broadcasts to `x.x.x.255:54330` every ~5 s. The console replies **unicast** to the sender's port 54330. Service name `_ypax-tf`.
```
"YSDP" | u16 BE len (bytes after first 6) | u8 flags (00=query, 80=reply) | u8 04
| IPv4 of sender (4) | 12 x 00 | MAC (6)
| u8 len "_ypax-tf\0" | u16 BE remaining len
| u8 len "Yamaha Corporation" | u8 len model "TF5" | u8 len device name
```
Optional: you can connect straight to TCP 50368.

---

## 4. TCP 50368 protocol (comctrl "cnp" = ConsoleNetworkProtocol)

Code lives at `0x667740–0x6740xx` (`comctrl_ConsoleNetworkProtocol.cpp`).

### 4.1 Frame [VERIFIED-CAP]
```
tag[4]  u32 BE length  payload[length]
```
- **tag** = 4 ASCII chars, NUL-padded. It names the channel:

| Tag | Meaning |
|---|---|
| `MPRO` | Property |
| `MMIX` | **Mixing (EQ)** |
| `MPRC` | Processing |
| `MFX` | FX |
| `MSUP` | Setup |
| `MVOL` | Volatile |
| `MSTS` | Status |
| `MSCL` | SceneList |
| `MSCS` | SceneStatus |
| `MCST` | ConsoleSetup |
| `EEVT` | events |
| `d000` / `d010` / `d020` | scene A / scene B / library file transfer |
| `DS_A` | seen once, PENDING |

- Several frames can share a segment, and frames can span segments. Reassemble per direction.

### 4.2 Payload [VERIFIED-CODE `cnpGetCommand` 0x667740, `cnpGetDataPtr` 0x6677f0, `cnpGetValue` 0x667d20]
```
payload = 0x11 | u32 BE n | cmd[3] | elementCount[1] | elements...      (n = 4 + elements length)
cmd     = [class][command][dir]   compared as a 24-bit BE int, e.g. 0x011101
          class: 01 MMS param channel, 02 file transfer, 03 event
          dir:   01 request/notify, 10 reply/ack
element = type[1] | u32 BE count | count * width bytes (BIG-endian)
type    = 0x11 u8, 0x12 u16, 0x14 u32           (unsigned)
          0x21 s8, 0x22 s16, 0x24 s32           (signed)
          0x31 char string, count includes the NUL (must be NUL-terminated)
```
Header writers: `FUN_00668220` / `FUN_00668280`. Element appender: `FUN_00668050(buf, cap, &pos, type, data, count)`. Big-endian writer: `FUN_006713a0`.

### 4.3 MMS channel commands [VERIFIED-CAP; dispatcher `FUN_0066f260`]

| cmd | Elements | Meaning |
|---|---|---|
| `01 01 01` | `31:func`, `11:[0x80]` | **Subscribe** to function |
| `01 10 01` | `11:[00]`, `31:func`, `11:descMD5[16]`, `11:dataHash[16]` | **Hash compare**. `FUN_00672a80` checks descriptor and data hashes separately. **A data-hash mismatch (e.g. all zeros) triggers a full dump.** A descriptor mismatch means a different firmware schema |
| `01 04 01` / `01 04 10` | — | Sync bracket open / close (both sides send, both ack) |
| `01 14 01` | `11:00`, `31:func`, `31:func`, `12:[0]`, `14:[]`, `14:[]`, `14:[240]`, `11:MMSXLIT`, `11:` | **Full dump** (same builder as set-param, `FUN_006738c0`, with an empty address) |
| `01 11 01` | see 4.4 | **Set/notify parameter** |
| `01 05 01` | ? | Handled by `FUN_00672e20` and resets channel state. Not in the capture, PENDING |

### 4.4 Set-param message `01 11 01` — the read/write API
Field order is identical in the builder (`FUN_006738c0`) and the parser (`FUN_006733a0`) [VERIFIED-CODE]:

| # | Type | Field | Notes |
|---|---|---|---|
| e0 | `0x11` ×1 | flags | 0 in all traffic; receiver ignores it |
| e1 | `0x31` | function name | Must equal the channel's function, max 24 chars (`"Mixing"`) |
| e2 | `0x31` | base path | `"Mixing"` in all traffic; empty → none. Passed to the resolver with e4 |
| e3 | `0x12` ×1 | depth N | ≤ 5 |
| e4 | `0x14` ×N **or** `0x31` | path | Numeric: child-collection index per level, last = param index. **Or a string path that the receiver resolves itself** (resolver call at `FUN_0066fb50`, max depth 5) [VERIFIED-CODE; string form untested on the wire] |
| e5 | `0x14` ×N | array index per level | |
| e6 | `0x14` ×1 | origin | `FUN_00665270`: **0xA0** = treat as the sender's own echo (not delivered as a change). **Anything else** (console uses 0xA1) = delivered as a change (source 0xF0) |
| e7 | value | | Encoder `FUN_00673c30`: signed → **`0x24` s32**, unsigned → **`0x14` u32**, string → `0x31`. Decoder `FUN_00667a90` also accepts `0x11/0x12/0x21/0x22` and requires count = 1 |

Example: set ch1 BandL gain to −5.20 dB (raw −520):
```
MMIX | len | 11 <n> 01 11 01 08
  11 00000001 00
  31 00000007 "Mixing\0"
  31 00000007 "Mixing\0"
  12 00000001 0005
  14 00000005 00000000 00000007 00000000 00000002 00000002    path InputChannel/PEQ/Bank/BandL/Gain
  14 00000005 00000000 00000000 00000000 00000000 00000000    ch index 0 at level 0
  14 00000001 000000A0
  24 00000001 FFFFFDF8
```
(e3 is a u16 element: `12 00000001 0005`.)

Observed traffic, resolved with `mmsxlit.resolve`:

| Wire path / idx | Resolved | Value |
|---|---|---|
| `MMIX [0,19,1]` / `[14,0,0]` | `Mixing/InputChannel:14/Fader/On` | 0 (ch15 off at the console, origin 0xA1) |
| `MVOL [5,0,0]` | `Volatile/Meter/RTA/Target` | editor write 0xA0, echoed |
| `MPRO [2,0,0]` / `[0,1,0]` | `Property/Status/CommSession:1/ClientType` | `"Editor"` |

### 4.5 Events (`EEVT`, class 03) [VERIFIED-CAP; builder `FUN_00671760`]
Elements: `14:[0]`, `14:[0]`, `31:name`, `31:args`. The receiver echoes with dir=10.
- **`KeepAlive`** / `""`: either side sends it; it is echoed.
- **`SetClientType`** / `"TYPE:<Editor|StageMix> SYNCDIR:<n>"` (`FUN_005008e0`):
  - TYPE 0 = `Editor`, 1 = `StageMix`.
  - **SYNCDIR = the editor's direction setting: 1 = ClientToServer (editor overwrites console), 2 = ServerToClient** (`FUN_00474030`, `FUN_00471e90`).
  - Sent in sync state 4 ("Preparing to Synchronize Memories").
- `onSetClientType`: console's acknowledgement.
- `NotifySyncStatus` / `"SCN0INI:23 SCN0REQ:0 … LIB0INI:142 …"`: scene/library sync progress.
- Other names in the binary: `RecallScene`, `onRecalledScene`, `ChangeSelectedCH`, `onSetWarningMessage`, `SendWarningMessage`.

### 4.6 Editor sync state machine [VERIFIED-CODE `FUN_00471e90`]
States: 3 "Synchronizing Temporarily" → 4 "Preparing" (sends SetClientType) → 5 "Synchronizing Memories" → 6 "Synchronized" (then scenes/libraries).

Each of the 18 MMS functions has a policy (`FUN_00507800(i)+0xc`):
- 1 = sync console→editor during the temporary phase
- 2 = follow the user's SYNCDIR
- 3 = always console→editor
- 4 = **always editor→console**

This is why the editor pushed 545 `SceneStatus` sets. A custom client must not blindly replay editor traffic.

---

## 5. MMSXLIT dump → reading EQ [VERIFIED-CAP]

Layout (little-endian) is in `tools/mmsxlit.py`:
- header `0x58`, then the descriptor tree, then the data image
- `COL0` 48 B = name, paramBytes, childBytes, offset, datasize, arraysize
- `PR ` 32 B = type, elemsize, arraysize, name; type 0 str, 1 int, 2 uint
- Mixing = 28 816 B descriptor + 38 285 B data (= `datasize` in `mms_Mixing.xml`)

The **wire path** is a parameter's position in this tree. **Derive paths from the console's dump at runtime.** They are firmware-specific (descriptor MD5 `8d0d0bdd394a9abb80ab12ed1500eddc` for this console).

Input-channel PEQ paths on this firmware (idx = `[ch-1,0,0,band,0]`):
```
PEQ/On                  [0,7,0]           uint8
PEQ/Bank/Type           [0,7,0,0]         str8   "BOLD"
PEQ/Bank/OneKnob/{On,Mode,Depth} [0,7,0,0,0..2]
PEQ/Bank/HPF/{On,Frequency,Slope} [0,7,0,1,0..2]
PEQ/Bank/BandL/{Type,Frequency,Gain,Q}       [0,7,0,2,0..3]
PEQ/Bank/Band[2]/{Type,Frequency,Gain,Q}     [0,7,0,3,0..3]  (idx level 3 = band 0/1)
PEQ/Bank/BandH/{Type,LPFOn,Frequency,Gain,Q} [0,7,0,4,0..4]
```

Scaling (from `params/mms_Mixing.txt`):
- Frequency raw/10 = Hz
- Gain raw/100 = dB (signed)
- Q raw/1000

**Band `Type` enum** [VERIFIED-CODE, name table at `0x8ac280`; per-band rows of 3 names selected in `FUN_00596910`]:
- **0 = Bell**, **1 = Shelf**, **2 = HPF** (low band of buses / stereo inputs) **or LPF** (high band)
- Mid bands: Bell only
- Input ch: BandL Bell/Shelf; BandH Bell/Shelf/LPF; HPF is a separate block

Ch1 as captured:
- HPF on, 200 Hz
- BandL Bell 160 Hz −5.20 dB Q 0.14
- Band0 800 Hz −14.10 dB Q 0.25
- BandH Shelf 10 kHz +4.60 dB

---

## 6. Recipe: read EQ + receive live updates

1. TCP connect to `<console>:50368`.
2. Property: send Subscribe(`Property`), HashCompare(`Property`, descMD5, 16×00), `01 04 01`.
   - The console replies with its own HashCompare, then `01 04 01`, the dump `01 14`, and `01 04 10`. Ack with `01 04 10`.
   - Use the descriptor MD5 the console returns (Property: `3a7c8d4c85f89f1eaa834f96630cec3d`).
3. Send `EEVT SetClientType "TYPE:StageMix SYNCDIR:2"` to coexist with a running TF Editor. Use `TYPE:Editor` only if no editor is connected.
   - Only one Editor is allowed: the editor itself refuses if any `Property/Status/CommSession:n/ClientType == "Editor"`.
   - **Never use SYNCDIR:1.**
   - [INFERRED] Whether the console treats StageMix differently (e.g. restricted functions) is untested.
4. Subscribe and hash-compare `Mixing` (zero data hash → dump). Parse the dump (`mmsxlit.parse`). You now have every EQ value.
5. Loop on incoming frames:
   - `MMIX 01 11` with origin ≠ 0xA0 → a live change from the console, another app or a scene recall.
   - Decode it with `resolve(root, path, idx)` and apply e7 (int32 signed / u32 / string).
6. Answer `EEVT KeepAlive` with a dir=10 echo, and send your own every ≤15 s.
7. Write: send `01 11` with origin 0xA0 (see the 4.4 example). The console echo confirms it.

### 6.1 Drawing the EQ curve like TF Editor [VERIFIED-CODE]

PEQ graph painters (`FUN_00545970`, `FUN_00555300`, `FUN_00592770`) run:
1. `FUN_0051a080` builds 6 sections `{freq, gain-or-on, Q, type}`;
2. `FUN_00519f50` designs them with `FUN_005196e0`;
3. `FUN_00546ae0` sums `10*log10|H|^2` per section.

Sample rate: 48000 (`0xbb80` in the widget ctors).

Section table (Ghidra shows these integer constants as tiny floats, e.g. 1.4e-45 = 1):
- slot 0, the HPF block: slope 6 → type 7, 12 → type 5; Q 0.707106; on → 1.0;
- slots 2.., the bands, `{freq, gain dB, Q}`:
  - low band: Type 0 → 0 (bell), 1 → 2 (low shelf), 2 → 5 (HPF, Q 0.707106; on only if `HPFOn` = 1);
  - high band: Type 0 → 0, 1 → 3 (high shelf), 2 → 4 (LPF; on only if `LPFOn` = 1).
- A section whose gain / on value is 0 contributes 0 dB.

Designer `FUN_005196e0(coef, fs, Q, gain, freq, type)`:
- Clamps: fs ≥ 44100, freq ≥ 16, Q ≥ 0.1.
- K = tan(π·f/fs), V = 10^(|gain|/20).
- Output layout: `coef = [-a2, -a1, b2, b1, b0]`, with a0 = 1.

| type | filter |
|---|---|
| 0 bell (Zölzer) | boost: num 1+K²±VK/Q, den 1+K²±K/Q. Cut: VK/Q moves to the denominator (exact mirror of the boost). |
| 2 / 3 shelves | **Q is ignored.** Two cascaded first-order shelves. Low: (1+K√V)+(K√V−1)z⁻¹ over (1+K)+(K−1)z⁻¹. High: (K+√V)+(K−√V)z⁻¹ over the same. Swap num/den for a cut. |
| 4 / 5 | 2nd-order LPF / HPF with Q |
| 6 / 7 | 1st-order LPF / HPF |
| 8–11 | first-order shelf variants (unused by the PEQ table) |

`tf_daemon/tfeditor.py` (`biquad`, `eq_curve`) implements this. `tests/tf_editor_reference.py` is a literal transcription of the decompiled code that the tests compare against.

---

## 7. Password [VERIFIED-CODE `FUN_00474030`]

After the Property sync, the editor:
- reads `Property/Specification/Unit/*`, `Status/CommSession/ClientType` and **`UserAccount/Unit/Password`** from the console's Property data;
- compares the password against what the user typed **locally**;
- if any CommSession already has `ClientType == "Editor"` → `Disconnected / OtherEditorConnected`;
- if the password doesn't match → `Disconnected / WrongPassword`; the **editor** closes the socket (matches the FIN from the PC in streams 59 and 63);
- otherwise → `PrepareToSync` with `SyncDirection` = ClientToServer/ServerToClient.

The console never checks a password on this channel, and the password value is readable by any client that syncs Property.
Treat port 50368 as unauthenticated: keep it on a trusted VLAN. This guide intentionally does not extract the stored password.

---

## 8. Meters — YMDP multicast [VERIFIED-CODE `ymdp_Client.cpp`: `FUN_0075c3f0`, `FUN_0075c750`, `FUN_0075c6b0`; group/port in `FUN_00471390`]

- `YMDPClient("239.192.0.164", 50240)`. The console sends it continuously (it ran before the editor connected), ~19.5 pkt/s.
- Packet:
  ```
  "YMDP" | src[8] = console IPv4 + 4x00 (checked) | model[4] "TF\0\0" (checked) | u32 BE seq
  blocks: hdr[8] + data, repeated while hdr[0] & 0x80
     hdr[0] & 0x7f : element type 0, 1, 2, 0x10 → 1 byte each; 0x20 → 2 bytes each
     hdr[1]        : block id
     hdr[2]        : 0 = L/mono, 1 = R (stereo pairs)   [INFERRED]
     hdr[3..5]     : 0
     hdr[6..7]     : u16 BE element count
  ```
- Block ids group by channel type (counts on TF5):

| Block ids | Count | Probable group |
|---|---|---|
| 0x00–0x07 | 40 | inputs |
| 0x08–0x13 | 4 | StIn / FxRtn |
| 0x14–0x19 | 16 | Group |
| 0x1a–0x1f | 8 | Mix |
| 0x20–0x25 | 12 | StMix |
| 0x26–0x2b | 2 | Stereo / Matrix |
| 0x2c–0x30 | 1 | Mono |
| 0x31–0x3c | — | misc |

- Type 0x02 appears once per group. It is probably the gain-reduction meter (schema `*GR` max = 0).
- [PENDING] Exact block-id → meter-point mapping and the 1-byte value scale. The meter callback is `IClientMeterDataAccessor` (vtable at `param_1[0x3f]` in `FUN_00471390`), fed into `METERPOOL::MeterPool`.

---

## 9. SCP (text) protocol [VERIFIED; not useful for EQ]
- Commands: `SET GET SETN GETN SETR SETT GETT EVENT PRMINFO PRMNUM MTR* DEVINFO DEVMODE DEVSTATUS SCPMODE SS*(_EX)`.
- 130 `MIXER:Current/...` / `IO:Current/...` addresses: faders, on, sends, labels, HA gain. **No PEQ/GEQ.**
- Used by the editor mainly to talk to I/O devices found via YSDP (`scp_sever_list`).

---

## 10. Remaining work [PENDING]
- [ ] Confirm on the wire: an EQ gain push arrives as `0x24` (code says the encoder always uses s32 for signed). One capture with a console-side EQ change settles it.
- [ ] Test the string-path form of e4 (e.g. `31:"InputChannel/PEQ/Bank/BandL/Gain"`?). The exact string syntax the resolver expects is unverified. Check vtable+0x14 of the MMS facade called from `FUN_0066fb50`.
- [ ] Behaviour of `TYPE:StageMix` (allowed functions, coexistence with an Editor).
- [ ] `01 05 01` command, `DS_A` frame, d0x0 file-transfer details (`02 01` announce, `02 03` 2048-B chunks + ack).
- [ ] YMDP block-id → meter-point mapping and value scale.

### Key function map (Ghidra VA)

| Function | Role |
|---|---|
| `0x474030` | Connect: password / OtherEditor check, PrepareToSync |
| `0x471e90` | Sync state machine; SetClientType |
| `0x5008e0` | Builds `"TYPE:… SYNCDIR:…"` |
| `0x507800` | Per-function sync policy table |
| `0x66f260` | MMS command dispatcher |
| `0x66fb50` | Incoming set-param handler |
| `0x6733a0` / `0x6738c0` | Set-param parse / build |
| `0x673c30` | Value encoder |
| `0x667a90` / `0x667d20` | Value decoders |
| `0x665270` | Origin filter |
| `0x672a80` | Hash compare |
| `0x671760` | EEVT builder |
| `0x596910` / `0x593970` | EQ type → name/icon |
| `0x75c3f0` | YMDP packet parse |
| `0x471390` | Sync thread ctor (YMDP group/port) |
