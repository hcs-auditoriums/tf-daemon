# Architecture

```
tf_daemon/
  __main__.py      CLI + Flask dev server
  wsgi.py          gunicorn entry point (env-var configured)
  app.py           Flask app factory; the one place modules are built and wired
  config.py        typed, validated, immutable Config (one dataclass per section)
  modules.py       Module protocol: register(app, sock) / start / stop / status
  hub.py           FrameHub: latest-value broadcast shared by the modules
  system.py        /config page: config.toml editing, restart, self-update
  vocal/           ── vocal EQ assist ──
    module.py      mic-check sessions, /api/vocal routes, Apply to desk / Undo
    plan.py        the desk changes that make a channel's EQ the suggestion
  audio/           ── the audio analysis module ──
    sources.py     DeviceSource (PortAudio), SynthSource, WavSource
    ringbuffer.py  lock-protected per-channel ring buffers
    engine.py      analysis thread, binary frame encoding, FrameHub
    module.py      HTTP + WebSocket routes for audio
  dsp/             ── signal processing, no I/O ──
    spectrum.py    windowed FFT, averaging, peak hold, log display bins
    ring.py        ring / feedback detection and attribution
    vocal.py       vocal EQ suggestions from a mic check
    eq.py          the console EQ's response curve (TF Editor's maths)
  console/         ── the Yamaha TF5 over the network ──
    patch.py       which console source each USB capture channel carries
    rcp.py         TF5Client: names, faders, sends over RCP (TCP 49280)
    module.py      TF5Module: /api/tf5/* routes, console data for the audio page
    sim.py         simulated TF5, both protocols (demo mode, tests)
    editor/        the EQ over the TF Editor protocol (TCP 50368)
      codec.py     wire format: elements, messages, frames
      guard.py     what may be sent: read-only unless a person asked for a write
      dump.py      the console's parameter tree and data image; a channel's EQ
      client.py    TFEditorClient: session, sync, keepalive, confirmed writes
      __main__.py  read-only probe CLI
  rf/              ── the RF (tinySA) module ──
    devices.py     TinySADevice (tsapython, USB serial), SimTinySA
    engine.py      connect/disconnect, sweep thread, peak hold, JSON state hub
    settings.py    SweepSettings (validated sweep range; also used by config.py)
    module.py      /rf page, HTTP + WebSocket routes for RF
  qlxd/            ── the Shure QLX-D module (read-only) ──
    protocol.py    TCP 2202 ASCII protocol: message framing, parsing, value decoding
    network.py     interface listing, NIC-bound sockets, SLP announcement listener
    receiver.py    one thread per receiver: GET polling, reconnect with backoff
    engine.py      persisted settings, discovery, receiver set, JSON state hub
    sim.py         simulated QLXD4 receivers (demo mode, tests)
    module.py      /qlxd page, HTTP + WebSocket routes
  static/          rf.html + rf.js (RF), qlxd.html + qlxd.js (wireless),
                   config.html + config.js, nav.js (all pages), style.css
    index.html     the audio page; app.js draws it and starts everything
    audio/         its ES modules (no build step): shared.js (canvas, state),
                   plot.js (geometry, grid, traces, hover), socket.js (/ws/audio),
                   controls.js, console.js (sidebar), eq.js (EQ overlay),
                   range.js, rings.js, vocal.js, waterfall.js
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
the tinySA and `/rf`, `/api/rf/*`, `/ws/rf`; likewise QLX-D, the TF5 console and
the vocal assist. All of them are built and wired together in one place,
`app.build_modules`. A module that needs another declares what it needs as a
small `Protocol` next to its own code, and receives it there; nothing reaches
into another module's objects. The audio page gets the console as a
`ConsoleFeed` (`audio/module.py`), the vocal assist gets `AudioInputs` and a
`DeskEQ` (`vocal/module.py`), and the TF5 module reports channel names and
faders through callbacks. mypy checks that the real objects fit.

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

## HTTP / WebSocket API

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
