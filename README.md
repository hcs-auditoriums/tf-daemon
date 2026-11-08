# tf-daemon

A Raspberry Pi appliance that captures audio from a **Yamaha TF5** over USB and
serves a real-time **spectrum analyzer** and **waterfall** with a **ring /
feedback detector** to any browser on the network.

The audio page's **View** menu picks the layout (**Spectrum**, **Waterfall** or
**Both**, stacked with aligned frequency columns), the traces (average,
real-time, peak hold), the dB range (**Auto**, the default, keeps a 70 dB window on the shown input's
level; fixed choices from −90…−10 to −130…0 dB; it also sets the waterfall's colours),
the waterfall's history (10 s to 5 min) and source (real-time or the 3 s
average), and the Rings / Console overlays. Choices are remembered per browser.
On the waterfall, speech shows as gliding harmonics and a ring as a straight
vertical line; hovering shows frequency, how long ago, and level.

A second page, **`/rf`**, is an RF spectrum analyzer for a **tinySA** on USB
serial (connect, sweep, live trace + peak hold, waterfall, and lines at the
QLX-D receivers' frequencies), ported from
tinysa-visualizer. A third page, **`/qlxd`**, is a read-only, low-effort
Wireless Workbench: battery runtime and RF/antenna status of **Shure QLXD4**
receivers found on a (link-local) network interface. Each is its own module; see
[Architecture](#architecture).

## Quick start (development: macOS / Windows / Linux, no TF5 needed)

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m tf_daemon --source synth --rf-source demo --qlxd-source demo
```

Open <http://localhost:8080> (audio), <http://localhost:8080/rf> (RF) or
<http://localhost:8080/qlxd> (wireless receivers). The synthetic source plays pink noise, speech-like
gliding harmonics, and a faint steady 2.5 kHz sine (the feedback-detector test
case).

Other sources:

```bash
python -m tf_daemon --source wav recording.wav    # loops a 48 kHz PCM WAV in real time
python -m tf_daemon --list-devices                # show PortAudio devices
python -m tf_daemon --source device               # real capture (default)
python -m tf_daemon --rf-source device            # real tinySA (default; pip install -e '.[rf]')
python -m tf_daemon --rf-source off               # no RF module
```

The RF demo device plays three synthetic carriers over a −80 dBm noise floor. The
QLX-D demo runs four simulated QLXD4 receivers on 127.0.0.1 (batteries drain at
60× speed; one has no transmitter). `--qlxd-source off` disables the module.

Run the tests with `pytest`.

## Server install (Raspberry Pi OS or Ubuntu)

Targets: Raspberry Pi 4 on Raspberry Pi OS Lite (Trixie), or an x86 Ubuntu
server. The system Python is "externally managed", so everything goes in a venv
(`python3-venv`); sounddevice needs `libportaudio2`. The deploy scripts install
the `[rf]` extra (tsapython) for the tinySA page, and the service gets the
`dialout` group so it can open the tinySA's `/dev/ttyACM*`.

From your workstation, copy the committed code to the server (no GitHub access
needed on the server):

```bash
NO_RESTART=1 deploy/deploy.sh user@host
```

Then on the server, once:

```bash
cd ~/tf-daemon && sudo bash deploy/install-service.sh
```

That installs `python3-venv libportaudio2 alsa-utils`, builds `.venv` as your
user, and installs/starts the `tf-daemon` systemd service (with the `audio` and
`dialout` groups so it can open the ALSA device and the tinySA). Re-run it after
a deploy that changes `deploy/tf-daemon.service`. After that, `deploy/deploy.sh user@host`
pushes new commits and restarts the service. The server's `config.toml` is
never overwritten by a deploy, so edit it there.

Or update from the server itself: the **Config** page (`/config`) has an
**Update now** button that runs `deploy/update.sh`, which pulls `update.branch`
from `update.remote` (default: this repo's `main`), reinstalls the package and
restarts the service. It runs in its own `tf-daemon-update` systemd unit, so it
outlives that restart and waits for the new version to answer. If the install
fails or the new version doesn't come up within 90 s, it puts the previous
revision back and restarts that, and the page says so. Progress is kept in
`.update.json` and `.update.log`, so the page shows the result even across the
restart. (Before `install-service.sh` has installed that unit, the button falls
back to running the script inside the service; updates still work but can't be
rolled back automatically.) The first run turns a `deploy.sh` copy into a git
checkout, and the server's `config.toml` is always kept. The same page edits
`config.toml` (validated before it is saved) and restarts the service to apply
it. The restart and update rely on the sudoers rules that `install-service.sh`
adds. There is no login on these pages, so keep the server on a trusted network.

Useful on the server: `journalctl -u tf-daemon -f`,
`~/tf-daemon/.venv/bin/python -m tf_daemon --list-devices`.

To run gunicorn by hand instead of the service:

```bash
.venv/bin/gunicorn -c gunicorn.conf.py tf_daemon.wsgi:app
```

> **One worker only.** The audio device can be opened exactly once and all
> analysis state lives in one process. `gunicorn.conf.py` sets
> `-w 1 -k gthread --threads 8` and refuses to start with more workers. Each
> open browser tab holds one thread (on any page), so 8 threads ≈ 6–7
> simultaneous viewers.

gunicorn is configured through environment variables:

| Variable | Default | |
|---|---|---|
| `TF_DAEMON_CONFIG` | `config.toml` | config path |
| `TF_DAEMON_SOURCE` | `device` | `device`, `synth` or `wav` |
| `TF_DAEMON_WAV` | | WAV path for `wav` |
| `TF_DAEMON_RF_SOURCE` | `rf.source` | `device`, `demo` or `off` |
| `TF_DAEMON_QLXD_SOURCE` | `qlxd.source` | `device`, `demo` or `off` |
| `TF_DAEMON_BIND` | `0.0.0.0:8080` | listen address |
| `TF_DAEMON_THREADS` | `8` | gunicorn threads |
| `TF_DAEMON_LOG` | `INFO` | log level |

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
**only suggests**: nothing is ever changed on the console.

During a mic check, show the vocal mic's input, press **Mic check** in the top
bar and **Start listening**, and have the person talk normally at their usual
distance for about 20 s (reading a passage works well). It counts only the
moments they are actually talking, then suggests, in the TF5's terms:

- an **HPF** just under the voice's lowest pitch (rumble, handling, proximity
  boom);
- up to four **PEQ bands** (L, 1, 2, H) with frequency, gain and Q, each named
  for what it addresses: boomy / muddy, boxy, woody / hollow, nasal / honky,
  tinny / thin, harsh, sibilant, or presence / air for a gentle boost;
- narrow **notches** for resonances in the mic or room, and for rings the ring
  detector has reported on that input.

With `tf5.eq` on, the channel's current desk EQ is shown next to each
suggestion. **Show on spectrum** draws the voice's difference from reference
speech (violet) and the suggested EQ (dashed green).

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

`[vocal]`: `enabled`, `inputs` (0-based inputs that are vocal mics; `[]` = any),
`listen_s` (20), `max_cut_db` (6), `max_boost_db` (3). API: `GET /api/vocal/status`,
`POST /api/vocal/start|stop|clear` `{"channel": n}`.

## Configuration

Everything lives in `config.toml`; any missing key falls back to the defaults in
`tf_daemon/config.py`. Highlights:

- `audio.device`: case-insensitive substring of the capture device name (default `"TF"`).
  `audio.channels` (34) channels are opened (a device with fewer is zero-padded),
  and every one can be picked in the browser (the input menu). An input is
  analyzed only while someone views it, except the `[[channels]]` ones, which
  are always analyzed.
- **Unplugged devices don't stop anything.** The server starts without the TF5
  connected and opens it when it appears (retrying every 3 s; the reason shows in
  `GET /api/audio/status` under `source_stats.error`). If the USB audio stops
  delivering for 2 s (unplugged, power-cycled) the stream is closed, PortAudio
  rescanned and the device reopened when it's back; the audio page shows
  "no audio" meanwhile. The tinySA, the TF5's network control and the QLX-D
  receivers reconnect on their own the same way, and a tinySA that stops
  answering times out after 20 s instead of tsapython's 120 s.
- `audio.samplerate`: must be 48000 (the TF5 is fixed at 48 kHz).
- `dsp.fft_size` (32768, ~1.46 Hz bins), `dsp.hop` (2400, ~20 updates/s),
  `dsp.average_tau` (3 s).
- `[[channels]]`: `index` (0-based device input) and `label`. These get their own
  buttons, a name in the input menu, and continuous analysis (their average and
  peak hold are already warm when you switch to them).
- `[synth]`: test-signal levels and the ring tone frequency/level.
- `[rf]`: `enabled`, `source` (`device` = tinySA, `demo`), `auto_connect`
  (default `true`) and the default sweep (`start_hz`, `stop_hz`, `points` ≤ 450,
  `rbw` `"auto"` or kHz, `interval_s`). With `auto_connect` the tinySA is opened
  and swept from startup, nobody needs to press Connect, and the range is the
  span of the known QLX-D receivers (`start_hz`/`stop_hz` only apply when none
  are known). With `auto_connect = false` it opens only on **Connect**.
- `[qlxd]`: `enabled`, `source` (`device`, `demo`), `interface` (NIC the
  receivers are on; `""` = first physical, up interface with a 169.254.x.x
  address), `hosts` (receivers to always monitor, `"ip"` or `"ip:port"`),
  `state_file`, `meter_poll_s`, `full_poll_s`. Interface and hosts are normally
  set on the `/qlxd` page (**Network…**); that choice is saved to `state_file`
  (default `qlxd_state.json` in the working directory) and overrides config.toml.
- `[tf5]`: `host` (console IP; `""` = off), `port` (49280), `username` /
  `password`, `poll_s`, `usb_patch`, `usb_post_fader` (default `false`: the USB
  Direct Outs are tapped before the fader; see Ring detection). With a host set, the server reads the TF5's
  channel names over Yamaha's remote control protocol and uses them as input
  labels (an explicit `[[channels]]` label still wins). The audio page also shows
  a **Console** sidebar (toggle in the **View** menu; stacks under the spectrum on
  narrow screens) for the shown input: name and colour, on/off, a fader with its
  level, pan, and the channel's sends to the mixes (by mix name; stereo pairs
  shown once), FX 1/2 and mono. Only active sends are listed unless **all** is
  ticked. On connect the console's parameter list (`prminfo`) is read, and any
  input-channel gain / EQ / HPF / dynamics parameters it offers are shown too. A
  TF5 lists none of those (checked on the desk at tf5-daemon); its EQ comes from
  `eq` below instead. Desk changes and scene recalls update live: the desk pushes
  each change, and the server forwards it to open pages over their audio
  websocket within ~50 ms (just the changed values; a fader moving on the desk
  moves on the page). The protocol has no login, so
  `username` / `password` are accepted but not sent. It also doesn't expose the
  USB output patch, so `usb_patch` says which console channel each USB capture
  channel carries (`"InCh N"`, `"St L"`, `"St R"`, `""`). The default is the TF
  recording patch: USB 1–32 = InCh 1–32, USB 33–34 = St L/R. Status:
  `GET /api/tf5/status`, values: `GET /api/tf5/channels`.
- `tf5.eq` (default `false`) / `tf5.eq_port` (50368): also read every channel's
  EQ over the TF Editor protocol (`tf_daemon/tfeditor.py`, protocol notes in
  `guides/tf5_api.md`) and offer an **EQ** toggle that overlays the console's EQ
  curve on the spectrum (at the spectrum's dB scale from the View range, 0 dB at
  mid-height; band handles sit on the curve). The **Console** sidebar also gets an
  **EQ** section: on/off, a small curve graph (±18 dB, widened to fit deep curves),
  and the HPF / low / high band shapes, live with every change on the desk. The curve
  is computed with TF Editor's own filter maths (decompiled; see the guide's EQ
  curve section). The client is **read-only**: it joins as a
  StageMix client with `SYNCDIR:2` (console → client), and a guard refuses to
  send anything but the subscribe / hash / sync / keepalive handshake, so it
  cannot change a parameter. `GET /api/tf5/eq/<usb>` returns the settings and
  curve. One-off read-only probe from a shell:
  `python -m tf_daemon.tfeditor <console-ip> --channels 1-8 [--watch]`.

Levels are dBFS with AES17 convention: a full-scale sine reads 0 dBFS.

## Architecture

```
tf_daemon/
  __main__.py      CLI + Flask dev server
  wsgi.py          gunicorn entry point (env-var configured)
  app.py           Flask app factory; builds sources and modules
  config.py        TOML loading, defaults, validation
  modules.py       Module protocol: register(app, sock) / start / stop / status
  audio/           ── the audio analysis module ──
    sources.py     DeviceSource (PortAudio), SynthSource, WavSource
    ringbuffer.py  lock-protected per-channel ring buffers
    engine.py      analysis thread, binary frame encoding, FrameHub
    module.py      HTTP + WebSocket routes for audio
  dsp/
    spectrum.py    windowed FFT, averaging, peak hold, log display bins
  rf/              ── the RF (tinySA) module ──
    devices.py     TinySADevice (tsapython, USB serial), DemoDevice
    engine.py      connect/disconnect, sweep thread, peak hold, JSON state hub
    module.py      /rf page, HTTP + WebSocket routes for RF
  qlxd/            ── the Shure QLX-D module (read-only) ──
    protocol.py    TCP 2202 ASCII protocol: message framing, parsing, value decoding
    network.py     interface listing, NIC-bound sockets, SLP announcement listener
    receiver.py    one thread per receiver: GET polling, reconnect with backoff
    engine.py      persisted settings, discovery, receiver set, JSON state hub
    simulator.py   fake QLXD4 TCP servers (demo mode, tests)
    module.py      /qlxd page, HTTP + WebSocket routes
  hub.py           FrameHub: latest-value broadcast shared by the modules
  static/          index.html + app.js (audio), rf.html + rf.js (RF),
                   qlxd.html + qlxd.js (wireless), style.css
```

Data flow:

1. **Capture thread** (PortAudio callback, or the paced synth/WAV thread) copies
   every column of each block into the ring buffers. Nothing else.
2. **Analysis thread** wakes every hop, pulls the newest `fft_size` samples for
   the active inputs (the `[[channels]]` ones plus any input a browser is
   viewing), and runs one analyzer per input: Blackman-Harris window →
   `rfft` → power → exponential average + peak hold + this hop's power as the
   real-time trace (full FFT resolution) →
   max-reduce into 1/48-octave display bins (50 Hz–16 kHz, 400 bins) → dBFS.
   All buffers are preallocated.
3. The frame is encoded as int16 centi-dB and published to a `FrameHub` that
   keeps only the latest frame.
4. **WebSocket threads** (one per client) send each new frame. Slow clients
   simply skip frames.

Feature areas are **modules** (`tf_daemon/modules.py`). The audio module owns
its device, threads and routes (`/api/audio/*`, `/ws/audio`); the RF module owns
the tinySA and `/rf`, `/api/rf/*`, `/ws/rf`. Both are assembled in
`app.build_modules`.

RF data flow: the **sweep thread** calls the tinySA's `scan` (blocking, one
sweep per call), updates peak hold, and publishes a full JSON state snapshot to
the RF hub, then waits `interval_s`. Connect/disconnect/start/stop also publish,
so every viewer sees the same state. The browser computes frequencies as
`linspace(start_hz, stop_hz, points)` and keeps its own 200-row waterfall.

RF auto mode: a **supervisor thread** keeps the tinySA connected and sweeping
whether or not anyone has the page open. It retries every 3 s while the device
is absent, and a failed sweep (unplugged) closes the device so it is reopened
when it comes back. The default range comes from an integration hook in
`app.py` (`receiver_range`): the known QLX-D receivers' frequencies ±2 MHz,
whole MHz, at least 10 MHz wide (**Fit to receivers** uses the same span), at
the device's maximum points. While on that range the sweep follows receivers
being added or retuned; a range set by hand on the page is kept. **Stop** or
**Disconnect** on the page pause auto mode (to free the device) until **Start**
or **Connect**.

Inputs: the original tinySA has two SMA inputs and a sweep only sees the selected
one, so every sweep start sends `mode low input` (range up to 350 MHz, **LOW**
connector) or `mode high input` (from 240 MHz, **HIGH** connector); the page shows
which, so the antenna goes on that port. A range spanning both (e.g. 100–500 MHz)
is refused. The Ultra has one input and is left alone.

QLX-D data flow: Shure devices periodically announce themselves with SLP
multicast (239.255.254.253:8427). The engine listens on the selected interface,
probes each announcing address on TCP 2202, and keeps the ones that answer like
a receiver (others are ignored for 10 minutes; **Rescan** forgets them). Manual
hosts are always monitored. Each receiver has a thread that sends only `GET`
commands, never `SET`: everything on connect and every `full_poll_s`, meters
(`RX_RF_LVL`, `RF_ANTENNA`, `AUDIO_LVL`) every `meter_poll_s`, and it applies the
`REP` messages receivers push on their own when a value changes. All sockets
are bound to the selected NIC (`SO_BINDTODEVICE` on Linux). A receiver that stops
answering is shown offline with the reason.

Known receivers: every receiver the module has reached is remembered in
`state_file` (`known_receivers`, keyed by MAC address) with its channel name,
device ID (e.g. `T4213-07`) and frequency, and updated when any of those change.
`GET /api/qlxd/frequencies` serves that list (live values win, offline ones
included). The `/rf` page polls it and, with **Receivers** ticked, draws a line
per receiver on the spectrum and waterfall (dashed and dimmed when offline);
**Fit to receivers** sets Start/Stop to span them. Entries for receivers that
are gone for good can be dropped with `POST /api/qlxd/known/forget`.

### HTTP / WebSocket API

| | |
|---|---|
| `GET /` | web UI |
| `GET /api/status` | per-module status counters |
| `GET /api/audio/meta` | channel labels, display bin frequencies, DSP params |
| `POST /api/audio/reset-peak` | body `{"channel": <input index>}`, or `{}` for all |
| `WS /ws/audio` | one JSON `meta` text message, then binary frames |

Binary frame (little-endian): `"TFS3"`, `uint32 seq`, `uint16 records`,
`uint16 bins`, then per analyzed input: `uint16 input index`, `uint16 0`,
`int16[3][bins]` in centi-dBFS (`[0]` average, `[1]` peak hold, `[2]` real-time:
this hop's spectrum, unaveraged). Clients send
`{"cmd": "view", "channel": n}` to have input `n` analyzed while they stay
connected, and `{"cmd": "reset_peak", "channel": n|null}`.

RF:

| | |
|---|---|
| `GET /rf` | RF web UI |
| `GET /api/rf/status` | connection / sweep counters |
| `GET /api/rf/state` | full state snapshot (same as a `/ws/rf` message) |
| `POST /api/rf/connect` | open the tinySA (503 with `error` if not found) |
| `POST /api/rf/disconnect` | stop sweeping and close it |
| `POST /api/rf/scan/start` | body: any of `start_hz`, `stop_hz`, `points`, `rbw`, `interval_s` |
| `POST /api/rf/scan/stop` | |
| `POST /api/rf/reset-peak` | |
| `WS /ws/rf` | JSON state snapshots (format in `rf/engine.py`) |

Wireless (QLX-D):

| | |
|---|---|
| `GET /qlxd` | wireless web UI |
| `GET /api/qlxd/status` | interface, receiver / online counts |
| `GET /api/qlxd/state` | full state snapshot (same as a `/ws/qlxd` message) |
| `GET /api/qlxd/interfaces` | network interfaces with their IPv4 addresses |
| `POST /api/qlxd/settings` | `{"interface": "eth1" \| "", "hosts": ["169.254.10.21", ...]}`; saved |
| `POST /api/qlxd/rescan` | forget found receivers and rediscover |
| `GET /api/qlxd/frequencies` | known receivers: `key` (MAC), `name`, `device_id`, `frequency_mhz`, `online` |
| `POST /api/qlxd/known/forget` | `{"key": "<mac>"}`, or `{}` for all: drop from the known list |
| `WS /ws/qlxd` | JSON state snapshots (format in `qlxd/engine.py`) |

## Roadmap

- Ring detection: remember ignored frequencies across restarts; per-input
  on/off for instrument channels that ring by nature.
- RF: port the rest of tinysa-visualizer (points of interest, noise-floor
  dropout events, scan recording and browsing).
