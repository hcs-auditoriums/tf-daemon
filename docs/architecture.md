# Architecture

```
tf_daemon/
  __main__.py      CLI + Flask dev server
  wsgi.py          gunicorn entry point (env-var configured)
  app.py           Flask app factory; the one place modules are built and wired
  config.py        typed, validated, immutable Config (one dataclass per section)
  modules.py       Module protocol: register(app, sock) / start / stop / status;
                   EngineModule, the base of the RF and QLX-D modules
  files.py         write_atomic: state and config files are replaced, never half-written
  hub.py           FrameHub: latest-value broadcast shared by the modules
  system.py        /config page: config.toml editing, restart, self-update
  vocal/           ── vocal EQ assist (everything it does lives here) ──
    voice.py       the voice on a mic: speech activity, pitch, long-term spectrum
    tone.py        EQ suggestions from that spectrum, against reference speech
    targets.py     mic types and EQ styles: what the suggestion aims at
    bands.py       which TF5 PEQ band each EQ section goes on
    transfer.py    dual-channel transfer function (mic -> mains / house mic)
    room.py        what the PA and the room do to the voice, from a house mic
    feedback.py    loop gain and feedback headroom per frequency
    plan.py        the desk changes that make a channel's EQ the suggestion
    session.py     mic checks: listen while the person talks, then analyze
    desk.py        apply / undo on the desk, only on a confirmed request
    auto.py        auto EQ: walks the EQ to the live suggestion, once a person turns it on
    profiles.py    named EQ profiles (per actor): save, recall, copy
    ports.py       what it needs from audio and the console (Protocols)
    ringout.py     ring-out: find each mic's feedback point, notch it, set safe faders
    ringwatch.py   ring-out's fast watch on the mains
    module.py      VocalModule: wiring, /api/vocal routes
  audio/           ── the audio analysis module ──
    sources.py     Source; SynthSource, WavSource
    device.py      DeviceSource (PortAudio), reopened when the device returns
    stage.py       simulated stage: talkers, desk, PA and room in a closed loop
    ringbuffer.py  lock-protected per-channel ring buffers
    engine.py      analysis thread, binary frame encoding, FrameHub
    module.py      HTTP + WebSocket routes for audio
  dsp/             ── signal processing, no I/O ──
    spectrum.py    windowed FFT, averaging, peak hold, log display bins
    ring.py        ring / feedback detection
    ringlog.py     rings as events for the pages; which inputs feed a ring on the mains
    eq.py          the console EQ's response curve (TF Editor's maths)
    db.py          dB conversions
  console/         ── the Yamaha TF5 over the network ──
    coverage.py    what can be read / written on the desk vs TF StageMix (docs/desk-coverage.md)
    strip.py       gain, gate, comp, sends per channel (/api/tf5/strip): RCP sends + a simulated store
    sends.py       DeskSends: the SEND tab's sends (AUX level / ON / pre / pan, FX, MONO) from and
                   to the desk over the editor protocol, in place of strip.py's once it is synced
    buses.py       Buses: the Mix page (/mix, /api/tf5/mix): AUX / ST / MONO / MATRIX / DCA masters,
                   DCA and mute group assignment, mute masters, cue; the one AUX 1-20 <-> Mix / StMix map
    busproc.py     BusProcessing: the buses' 31-band GEQ (12-band rule) and PEQ (/api/tf5/mix/geq|eq)
    patch.py       which console source each USB capture channel carries
    rcp.py         TF5Client: names, faders, sends over RCP (TCP 49280)
    meters.py      MeterClient: level meters over RCP (mtrstart, read-only); probe CLI
    module.py      TF5Module: /api/tf5/* routes, console data for the audio page
    sim.py         simulated TF5 (demo mode, tests): sim_rcp.py + sim_editor.py as one desk
    sim_rcp.py     its RCP side
    sim_editor.py  its TF Editor side: all 10 functions, set-param echo, scene recall
    sim_tree.py    its parameter data: dumps from the real V4.55 tree, demo values
    editor/        the TF Editor protocol (TCP 50368): EQ and the rest of the desk
      codec.py     wire format: elements, messages, frames
      guard.py     what may be sent: read-only unless a person asked for a write;
                   the write families and their config switches
      tree.py      the TF5's V4.55 parameter tree (tf5_v455.json): tags, MD5s, schema
      dump.py      a function's parameter tree and data image (MMSXLIT); a channel's EQ
      client.py    TFEditorClient: session, sync of every function, keepalive, confirmed writes
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
                   mix.html + mix.js (the desk's buses, DCAs, mute groups, cue) with
                   mix/geq.js (a bus's GEQ) and mix/peq.js (a bus's PEQ),
                   config.html + config.js, nav.js (all pages), style.css;
                   the page scripts are ES modules (no build step) sharing
                   common.js ($, el, storage, JSON POST / PUT, reconnecting websocket,
                   canvas sizing, the fader law)
    index.html     the audio page; app.js draws it and starts everything
    audio/         its ES modules (no build step): shared.js (canvas, state),
                   plot.js (geometry, grid, traces, hover), socket.js (/ws/audio),
                   controls.js, console.js (sidebar), eq.js (EQ overlay),
                   range.js, rings.js, vocal.js, waterfall.js, eqedit.js (EQ
                   editing on the spectrum), eqbar.js (EQ toolbar), strip.js +
                   dyngraph.js (gain / gate / comp / sends panel), profiles.js (EQ profiles),
                   ringout.js (ring-out dialog), meters.js (the desk's level meters)
```

Data flow:

1. **Capture thread** (PortAudio callback, or the paced synth/WAV thread) copies
   every column of each block into the ring buffers. Nothing else.
2. **Analysis thread** wakes every hop, pulls the newest `fft_size` samples for
   the active inputs (the `[[channels]]` ones plus any input a browser is
   viewing), and runs one analyzer per input: Blackman-Harris window →
   `rfft` → power → exponential average + peak hold + this hop's power as the
   real-time trace (full FFT resolution) →
   max-reduce into 1/48-octave display bins (20 Hz–20 kHz as on the TF5, 479 bins) → dBFS.
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
`DeskEQ` (`vocal/ports.py`), and the TF5 module reports channel names and
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

Each module's docstring lists its routes, request bodies and messages; that list is
the reference:

| Area | Routes | Where |
|---|---|---|
| Pages, status | `GET /`, `GET /api/status` (per-module counters) | `app.py` |
| Audio | `/api/audio/*`, `WS /ws/audio` | `audio/module.py`; binary frame format in `audio/engine.py` |
| TF5 console | `/api/tf5/*` | `console/module.py` |
| Vocal assist | `/api/vocal/*` | `vocal/module.py` |
| RF | `/rf`, `/api/rf/*`, `WS /ws/rf` | `rf/module.py`; state format in `rf/engine.py` |
| Wireless (QLX-D) | `/qlxd`, `/api/qlxd/*`, `WS /ws/qlxd` | `qlxd/module.py`; state format in `qlxd/engine.py` |
| Config, update | `/config`, `/api/system/*` | `system.py` |
