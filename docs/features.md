# Features

## The pages

The audio page's **View** menu picks the layout (**Spectrum**, **Waterfall** or
**Both**, stacked with aligned frequency columns), the traces (average,
real-time, peak hold), the dB range (**Auto**, the default, keeps a 70 dB window on the shown input's
level; fixed choices from −90…−10 to −130…0 dB; it also sets the waterfall's colours),
the waterfall's history (10 s to 5 min) and source (real-time or the 3 s
average), and the Rings / Console overlays. Choices are remembered per browser.
On the waterfall, speech shows as gliding harmonics and a ring as a straight
vertical line; hovering shows frequency, how long ago, and level.

**Editing the console, as on TF StageMix.** With the EQ shown on the spectrum (and
`tf5.eq_write` on), drag a band's handle (frequency and gain), pinch for a bell's Q,
double-tap a handle to put its gain back to 0 dB, or click it for its settings (type:
bell / shelf, and LPF on the high band; values to type; locks). The toolbar at the top
left switches the EQ and HPF on and off, undoes and redoes, and compares (A/B) with a
copied EQ; its menu has Gain flat, Copy, Paste and Compare with before. Every value is
held to the TF5's ranges. Tap the channel's name at the foot of the console sidebar
(or its **Sends** heading) for its **Input**, **Gate**, **Comp** and **Sends**: gain,
+48V (asks first), phase; the gate's and compressor's curves with a draggable threshold
(and ratio); each AUX send's level, ON, pre and pan. Which of these the real desk
can be read and written for yet, and which are only simulated in demo mode, is in
[Desk coverage](desk-coverage.md).

**Shift+← / Shift+→** switches between the pages (Audio, RF, Wireless, Config),
except while typing or choosing in a field. The Config page checks for updates by
itself when it opens.

A second page, **`/rf`**, is an RF spectrum analyzer for a **tinySA** on USB
serial (connect, sweep, live trace + peak hold, waterfall, and lines at the
QLX-D receivers' frequencies), ported from
tinysa-visualizer. A third page, **`/qlxd`**, is a read-only, low-effort
Wireless Workbench: battery runtime and RF/antenna status of **Shure QLXD4**
receivers found on a (link-local) network interface. Each is its own module; see
[Architecture](architecture.md).

## Ring detection

Always on, nothing to set up. It watches every analyzed input: Main L/R all the
time, plus whatever input someone is viewing. A ring shows up as:

- a **header alert** (`RING 2.50 kHz · Main L`; pulsing red `FEEDBACK` when it
  is growing). Click it to show that input.
- a **marker** on the spectrum at the ring's frequency (dashed once it's gone,
  for five minutes).
- the **Rings list** (bottom left of the spectrum): frequency, input, the
  nearest 1/3-octave **GEQ band** to pull down, how often it came back (`×3`)
  and when. **Ignore** stops reporting a frequency (a steady tone that isn't
  feedback, until restart); **Clear** forgets past rings. **Rings** in the
  **View** menu hides the markers and the list (the alert stays).

**Which input is ringing.** For a ring on the mains (the TF's St L/R on USB
33/34, or the pinned inputs without a TF5), the list and the alert also name the
inputs feeding it, strongest first: `from ROEDER · KAT −4 dB` (click a name to
view it). A ring is the loop loudspeaker → room → mic → channel → mains; every
open mic hears the PA, but the loop gain at the ring frequency is set by how much
of it each channel sends back to the mains. So every input is measured at the
ring frequency (a single-frequency DFT over ~0.34 s, four times a second while a
ring is live) and the channel's fader from the TF5 is added; channels that are
off are left out. This relies on the USB feeds being **pre-fader**: on the TF,
USB 1–32 are the channel Direct Outs with a per-channel tap point (Pre DG, Pre
HPF, Pre Fader or Post ON; Reference Manual p. 67). If yours are Post ON, set
`tf5.usb_post_fader = true` so the fader isn't counted twice. Taps before the
EQ also mean a notch already applied on a channel's EQ isn't seen (EQ can't be
read over the network).

It works the same during a mic check and mid-show, and does not need the ring to
be louder than the voice. Just below feedback, the system acts as a very narrow
resonant boost (with loop gain g the boost is 1/(1−g): +26 dB and ~0.5 Hz wide at
g = 0.95), so a ring is the programme's own energy at one frequency, boosted and
smeared out in time. Three things give it away:

| | what the detector sees | typical situation |
|---|---|---|
| **feedback** | a steady frequency whose level keeps rising for a second or more | gain pushed too far |
| **ring** (sustained) | a narrow peak at a fixed frequency in a ~1 s average, where moving speech harmonics smear out; found even 25 dB under the voice | pushing gain at mic check; a ring under dialogue |
| **ring** (tail) | after a phrase ends the room dies away (~60 dB/s) but this frequency decays much slower (a ring near feedback: ~15–30 dB/s) | "it rings after every line" |

To keep speech and music from triggering it, a candidate must stand ≥ 10 dB above
the spectrum around it, hold its frequency to ~0.3% (voiced speech glides, singing
has vibrato) and have no harmonic series: nothing comparable at 2× and 3× its
frequency (the peak-to-harmonic power ratio, the strongest single criterion in
van Waterschoot & Moonen, *JAES* 58(11), 2010), and no louder steady peak at
1/2…1/10 of it (an upper partial of a note). A single tail is shown as
*possible* without an alert: a slowly decaying instrument note (piano, vibes)
can look like one. When it recurs at the same frequency, it's a ring.

`tests/test_ring.py` checks this against synthetic speech with rings 5–25 dB
under the voice, speech through a simulated loop at gain 0.95, runaway feedback
and tails (all must be found within 1–2.5 s), and speech, reverberant speech,
vibrato, organ chords, room-decaying notes and mains hum (must stay silent).

`[ring]` in `config.toml`: `enabled`, `sensitivity` (`low` | `normal` | `high`),
`fmin` / `fmax` (detection range, default 80 Hz–12 kHz), `mains_hz` (hum
harmonics to skip: 60, 50 or 0). API: `GET /api/audio/rings`,
`POST /api/audio/rings/clear`, `POST /api/audio/rings/ignore` `{"freq": hz}`,
`POST /api/audio/rings/unignore` `{"freq": hz}` or `{}`; live updates arrive on
`/ws/audio` as `{"type": "rings", ...}`.

## Vocal EQ assist (mic checks)

Optional and **off by default** (`[vocal] enabled = true` to turn it on). It
suggests; the console is changed only if you press **Apply to desk** and confirm
(and `tf5.eq_write` is on), never automatically.

During a mic check, show the vocal mic's input, press **Mic check** in the top
bar and **Start listening**, and have the person talk normally at their usual
distance for about 20 s (reading a passage works well). It counts only the
moments they are actually talking, then suggests, in the TF5's terms:

- an **HPF** just under the voice's lowest pitch (rumble, handling, proximity
  boom);
- up to four **PEQ bands** (L, LM, HM, H) with frequency, gain and Q, each named
  for what it addresses: boomy / muddy, boxy, woody / hollow, nasal / honky,
  tinny / thin, harsh, sibilant, or presence / air for a gentle boost;
- narrow **notches** for resonances in the mic or room, and for rings the ring
  detector has reported on that input.

With `tf5.eq` on, the channel's current desk EQ is shown next to each
suggestion. **Show suggested EQ on spectrum** draws the suggested EQ as a violet
dashed curve on the same scale as the desk's EQ overlay (green), so the two can
be compared directly; **Compare with desk EQ** turns that overlay on. A faint thin
line shows the voice's difference from reference speech.

A change to a value the desk already has isn't sent (the TF5 doesn't echo a set that
changes nothing) and counts as confirmed.

**Apply to desk** (needs `tf5.eq = true` and `tf5.eq_write = true`): sets the
input channel's EQ on the TF5 to the suggestion. A confirmation lists every
change first: the EQ switched on if it was off, the HPF on at the suggested
frequency, suggested bands as bells, and the remaining bands set flat (0 dB) so
the desk matches the suggested curve. A low or high band used as an HPF / LPF is
left alone, and so is a narrow cut (Q 5 or more: almost always a feedback notch
someone rang out) and every band a ring-out reserved. The panel then reports how many changes the desk confirmed (it echoes
each one back). **Undo apply** puts back exactly the values it replaced. Only an
input channel's PEQ can be written, not with 1-knob EQ on, and nothing in
tf-daemon writes on its own. The analyzer taps the mic before the EQ, so the
spectrum doesn't change after applying; the green desk EQ curve does.

How it decides: it builds the voice's long-term average spectrum and compares
its *shape* with the ANSI S3.5-1997 standard speech spectrum (normal vocal
effort). Build-ups of 3 dB or more spanning at least 1/3 octave become cuts
(80 % of the excess, at most `max_cut_db`); a deficit of 3 dB or more at
2–8 kHz becomes a boost (at most `max_boost_db`). Energy at the voice's own
fundamental is left alone. Cuts are preferred to boosts and kept few. The
labels are a vocabulary for where an issue sits, not a diagnosis (part of
nasality is how a person talks), and the reference is an average voice, so treat
the result as a starting point for your ears. It analyzes the mic before the EQ
and dynamics, so compression isn't included.

**Listen live** keeps listening while the person talks instead of stopping
after 20 s: the suggestion follows the last minute of speech and updates every
few seconds, and the panel says whether the voice reaches the **mains** (and at
what gain re the mic), measured by comparing the mic with the mains. Rings that
this mic causes (on the mic itself, or with it as the strongest source of a ring
on the mains) become narrow cuts in the suggestion.

**With a house mic** (`house_mic`: a measurement mic in the audience area on a
desk input, patched to USB), the suggestion aims at the voice *as the room hears
it*: the mic is compared with the house mic (like Smaart's transfer function,
using the voice itself as the test signal), the channel EQ already on the desk is
taken out, and what's left (the PA and the room) is added to the analysis. Only
frequencies where the house mic clearly hears this voice count; if too few do, it
says so and suggests from the mic alone.

**Feedback headroom** (live): how far each frequency is from feedback, from
150 Hz to 5 kHz. The loop is mic → channel → mains → PA → back into the mic:
the first half is measured while the person talks (mic vs mains), the second
while their mic is idle and the mains carry something else, like another talker
or music (mains vs mic: the PA leaking back). The panel shows the frequency
closest to feedback; suggested boosts are held to what keeps 6 dB of headroom.
Until both halves have been heard, the headroom is unknown and only the usual
boost limit applies.

**Auto EQ** (optional, off by default): with `auto_allowed = true` in the config,
the panel offers **Turn auto on…** for a channel (with a confirmation). From then
on, while the person talks and the live suggestion has settled, it moves the
channel's EQ towards the suggestion one small step at a time (at most 1 dB per
band every few seconds; a band that has to move is faded out, moved and faded back
in, so nothing jumps). Boosts only where the feedback headroom is measured (and
keep 6 dB of it). A ring this mic causes gets a narrow notch within a second,
deeper if it persists, and any boost near it is taken out. It **stops itself the
moment anyone changes that channel's EQ on the desk**, or if the desk stops being
writable or the EQ is turned off. **Undo auto changes** puts back the EQ from
before it was turned on, and the panel lists what it did and why. It needs
`tf5.eq_write` too.

**Mic type and EQ style** (pickers in the panel, remembered per input in the
browser; defaults from `mic_type` / `eq_style`). The reference speech spectrum is
measured 1 m in front of a mouth, which no mic on a person hears, so:

- *Mic*: **Generic** (aims straight at the reference), **Wig / hairline** (accepts a
  slightly softer top and boosts at most 1 dB: head-worn mics feed back first at
  2–8 kHz), **Lavalier (chest)** (cuts the chest resonance, puts back only part of the
  upper mids lost under the chin, boosts at most 2 dB), **Headset / boom**,
  **Handheld / podium** (proximity bass is cut like any build-up). The head-worn
  types also get a higher HPF floor and narrower ring-out notches (Q 10).
- *EQ for*: **Speech** (clarity first), **Projected speech** (keeps the upper mids a
  projected stage voice has), **Speech & singing** (a little more warmth and ring,
  cuts at most 4 dB), **Singing** (keeps the body and the 2.5–3.5 kHz singer's ring,
  cuts at most 3 dB, lower HPF).

Each EQ type also adds a little **character** on top of the correction, on a band
that's free, away from rings and the other moves: *Speech* a touch of presence
(3 kHz), *Projected speech* less box (400 Hz cut), *Speech & singing* presence and
air (a 10 kHz shelf), *Singing* the singer's ring (3 kHz), air and body (220 Hz).
Boosts stay within the mic type's limits (a wig mic: 1 dB in the 2–8 kHz feedback
range, 2 dB of air) and the measured feedback headroom.

All bands are kept **in frequency order** (L ≤ LM ≤ HM ≤ H), notches included, so
the desk and TF Editor read the way you'd expect.

These are starting points from common placement guidance, not measurements of a
particular microphone.

**Ring-out** (needs `tf5.fader_write`; offered as **Ring out…** in the panel). The
software takes over the chosen mics' faders and EQ and does what an engineer does
by hand. A popup stays on top the whole time with **Mute & cancel** (or Esc): it
mutes every ring-out channel at once, then puts their faders and EQ back as they
were; they stay muted until you press **Unmute**.

1. One mic at a time, the others in the ring-out muted. The high-pass goes on at the
   mic type's floor (100 Hz for head-worn mics; a higher one stays). The fader starts
   10 dB under its level and rises 1 dB a second, never above `ringout_ceiling_db`. When it
   starts to ring, the fader drops 6 dB at once. If the ring then falls clearly
   further than the fader did (and further than everything else), it was feedback:
   a narrow notch (−3 dB, the mic type's Q) goes on a free PEQ band, and the ramp
   carries on from just under where it rang. A ring right beside an existing notch is
   the same resonance (the loop's delay makes it ring at one of several frequencies)
   and widens and deepens that notch instead, to at most −9 dB. A steady tone (hum,
   a held note) falls only as far as the fader and is ignored. A ring under 170 Hz is
   taken out with the high-pass instead (raised to 1.3 × the ring, never past the
   voice's lowest pitch or 200 Hz). Placing a notch keeps every band in frequency
   order: the channel's other bands move along (same settings), and when there's no
   room the one doing least gives way. Each mic is done at the
   ceiling, after 3 notches (one band stays for tone), or when new frequencies ring
   as soon as the last was notched: the system's limit.
2. All of them together at their safe levels, raised together up to the margin:
   more open mics feed back sooner (about 3 dB per doubling). A ring here is notched
   on the mic the ring detector names as its source.
3. Each fader ends at least `ringout_margin_db` under where it rang (alone and
   together) and never above where you had it; a fader that was down stays down,
   and on / off is as before. The popup lists, per mic, where it first rang, where
   it rings now (the gain the notches bought) and the notches. A frequency that rang
   on two or more mics is named: the system rings there (PA, room, or the capsules'
   shared peak), and one narrow cut on the stereo bus PEQ would serve them all.
4. **The vocal curve**: from the voice heard during the ring-out (the analysis taps
   are before the fader, so the ramp doesn't skew it), the suggestion for the mic type
   and EQ type goes on in one step: the high-pass under the voice (never below what a
   ring needed), cuts where the voice builds up, and the EQ type's character, on the
   bands the notches left. Have each person talk and sing while their mic is rung out;
   without enough voice it says so and live tone EQ does it later.
5. Optionally (on by default), **live tone EQ**: auto EQ on each mic with its mic
   type and EQ style, the notches reserved (never moved), while each person talks or
   sings, until you press **Done**.

It stops on its own and hands back the desk: when anyone moves one of those faders
or changes their EQ on the desk (it leaves everything as it is), when no page has
been watching for 10 s while it is raising faders (faders back to where they were),
and when the desk can't be written or a fader move isn't confirmed (faders back).
Before it starts, the fader the TF Editor protocol reports must match the one RCP
reports, and after every move RCP must follow; if not, it doesn't move faders.
It listens to the mains for 2 s first and stops without changing anything if they
are silent (main bus muted, PA off): with no PA there is no loop to ring.

Ring the mics out where they are used: on the actor, including downstage near the
speakers. Mute other open mics first. Notches typically buy 3–6 dB; distance to the
speakers and the number of open mics matter more.

**EQ profiles** (the **Profiles** button in the console sidebar's EQ section): **Save as…** stores the
shown channel's whole EQ (EQ on, the high-pass, every band's type, frequency, gain
and Q) under a name, e.g. the actor's; pick a profile and **Recall** to put it on
the channel you're looking at, on any channel; **Copy from…** copies another
channel's EQ straight across. Recall and copy ask first and can be undone (Undo
puts back the EQ from before the first change). Profiles are saved in
`profiles_file` and survive restarts.

`[vocal]`: `enabled`, `inputs` (0-based inputs that are vocal mics; `[]` = any),
`listen_s` (20), `max_cut_db` (6), `max_boost_db` (3), `house_mic` (-1 = none),
`room_tilt_db_oct` (0: the in-room target is the reference speech spectrum; tilt
it for taste), `auto_allowed` (false), `profiles_file` (`eq_profiles.json`),
`mic_type` (`generic`), `eq_style` (`speech`), `ringout_ceiling_db` (6),
`ringout_margin_db` (6). API: `GET /api/vocal/status`,
`POST /api/vocal/start|stop|clear` `{"channel": n}` (start also takes `"live": true`,
`"mic"` and `"style"`),
`POST /api/vocal/apply|undo` `{"channel": n, "confirm": true}`,
`POST /api/vocal/auto` `{"channel": n, "on": true, "confirm": true}` (or `"on": false`),
`POST /api/vocal/auto/undo` `{"channel": n, "confirm": true}`,
`GET /api/vocal/ringout` (the page's poll is the "someone is watching" heartbeat),
`POST /api/vocal/ringout/start` `{"mics": [{"channel": n, "mic": id, "style": id}], "tone": true,
"confirm": true}`, `POST /api/vocal/ringout/abort` (Mute & cancel), `.../unmute`, `.../finish`.
