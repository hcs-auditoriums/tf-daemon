# tf-daemon

A Raspberry Pi appliance that captures audio from a **Yamaha TF5** over USB and
serves a real-time **spectrum analyzer** (and, coming next, a feedback/ringing
detector) to any browser on the network.

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

## Configuration

Everything lives in `config.toml`; any missing key falls back to the defaults in
`tf_daemon/config.py`. Highlights:

- `audio.device`: case-insensitive substring of the capture device name (default `"TF"`).
  All channels the device offers are opened, and every one can be picked in the
  browser (the input menu). An input is analyzed only while someone views it,
  except the `[[channels]]` ones, which are always analyzed.
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
  `password`, `poll_s`, `usb_patch`. With a host set, the server reads the TF5's
  channel names over Yamaha's remote control protocol and uses them as input
  labels (an explicit `[[channels]]` label still wins). The audio page also shows
  a **Console** sidebar (toggle in the header; stacks under the spectrum on
  narrow screens) for the shown input: name and colour, on/off, a fader with its
  level, pan, and the channel's sends to the mixes (by mix name; stereo pairs
  shown once), FX 1/2 and mono. Only active sends are listed unless **all** is
  ticked. On connect the console's parameter list (`prminfo`) is read, and any
  input-channel gain / EQ / HPF / dynamics parameters it offers are shown too. A
  TF5 lists none of those (checked on the desk at tf5-daemon), so EQ can't be shown. Desk
  changes and scene recalls update live. The protocol has no login, so
  `username` / `password` are accepted but not sent. It also doesn't expose the
  USB output patch, so `usb_patch` says which console channel each USB capture
  channel carries (`"InCh N"`, `"St L"`, `"St R"`, `""`). The default is the TF
  recording patch: USB 1–32 = InCh 1–32, USB 33–34 = St L/R. Status:
  `GET /api/tf5/status`, values: `GET /api/tf5/channels`.

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
   `rfft` → power → exponential average + peak hold (full FFT resolution) →
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

Binary frame (little-endian): `"TFS2"`, `uint32 seq`, `uint16 records`,
`uint16 bins`, then per analyzed input: `uint16 input index`, `uint16 0`,
`int16[2][bins]` in centi-dBFS (`[0]` average, `[1]` peak hold). Clients send
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

- Feedback / ringing detector (narrow, persistent peaks rising above the
  averaged spectrum), shown as markers and a list in the UI.
- RF: port the rest of tinysa-visualizer (points of interest, noise-floor
  dropout events, scan recording and browsing).
