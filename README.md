# tf-daemon

A Raspberry Pi appliance that captures audio from a **Yamaha TF5** over USB and
serves a real-time **spectrum analyzer** (and, coming next, a feedback/ringing
detector) to any browser on the network.

Scope is audio analysis only. RF / wireless-mic monitoring is planned as a
separate module (see [Architecture](#architecture)).

## Quick start (development: macOS / Windows / Linux, no TF5 needed)

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m tf_daemon --source synth
```

Open <http://localhost:8080>. The synthetic source plays pink noise, speech-like
gliding harmonics, and a faint steady 2.5 kHz sine (the feedback-detector test
case).

Other sources:

```bash
python -m tf_daemon --source wav recording.wav    # loops a 48 kHz PCM WAV in real time
python -m tf_daemon --list-devices                # show PortAudio devices
python -m tf_daemon --source device               # real capture (default)
```

Run the tests with `pytest`.

## Server install (Raspberry Pi OS or Ubuntu)

Targets: Raspberry Pi 4 on Raspberry Pi OS Lite (Trixie), or an x86 Ubuntu
server. The system Python is "externally managed", so everything goes in a venv
(`python3-venv`); sounddevice needs `libportaudio2`.

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
user, and installs/starts the `tf-daemon` systemd service (with the `audio`
group so it can open the ALSA device). After that, `deploy/deploy.sh user@host`
pushes new commits and restarts the service. The server's `config.toml` is
never overwritten by a deploy, so edit it there.

Useful on the server: `journalctl -u tf-daemon -f`,
`~/tf-daemon/.venv/bin/python -m tf_daemon --list-devices`.

To run gunicorn by hand instead of the service:

```bash
.venv/bin/gunicorn -c gunicorn.conf.py tf_daemon.wsgi:app
```

> **One worker only.** The audio device can be opened exactly once and all
> analysis state lives in one process. `gunicorn.conf.py` sets
> `-w 1 -k gthread --threads 8` and refuses to start with more workers. Each
> open browser tab holds one thread, so 8 threads ≈ 6–7 simultaneous viewers.

gunicorn is configured through environment variables:

| Variable | Default | |
|---|---|---|
| `TF_DAEMON_CONFIG` | `config.toml` | config path |
| `TF_DAEMON_SOURCE` | `device` | `device`, `synth` or `wav` |
| `TF_DAEMON_WAV` | | WAV path for `wav` |
| `TF_DAEMON_BIND` | `0.0.0.0:8080` | listen address |
| `TF_DAEMON_THREADS` | `8` | gunicorn threads |
| `TF_DAEMON_LOG` | `INFO` | log level |

## Configuration

Everything lives in `config.toml`; any missing key falls back to the defaults in
`tf_daemon/config.py`. Highlights:

- `audio.device`: case-insensitive substring of the capture device name (default `"TF"`).
  All channels the device offers are opened; only `[[channels]]` are analyzed.
- `audio.samplerate`: must be 48000 (the TF5 is fixed at 48 kHz).
- `dsp.fft_size` (32768, ~1.46 Hz bins), `dsp.hop` (2400, ~20 updates/s),
  `dsp.average_tau` (3 s).
- `[[channels]]`: `index` (0-based device input) and `label`.
- `[synth]`: test-signal levels and the ring tone frequency/level.

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
  static/          index.html, app.js, style.css (no build step)
```

Data flow:

1. **Capture thread** (PortAudio callback, or the paced synth/WAV thread) copies
   the analyzed columns of each block into the ring buffers. Nothing else.
2. **Analysis thread** wakes every hop, pulls the newest `fft_size` samples for
   all channels, and runs the vectorized analyzer: Blackman-Harris window →
   `rfft` → power → exponential average + peak hold (full FFT resolution) →
   max-reduce into 1/48-octave display bins (50 Hz–16 kHz, 400 bins) → dBFS.
   All buffers are preallocated.
3. The frame is encoded as int16 centi-dB and published to a `FrameHub` that
   keeps only the latest frame.
4. **WebSocket threads** (one per client) send each new frame. Slow clients
   simply skip frames.

Feature areas are **modules** (`tf_daemon/modules.py`). The audio module owns
its device, threads and routes (`/api/audio/*`, `/ws/audio`). A future RF module
would live in `tf_daemon/rf/` with its own routes (`/api/rf/*`, `/ws/rf`) and be
added in `app.build_modules`; nothing in the audio code needs to change.

### HTTP / WebSocket API

| | |
|---|---|
| `GET /` | web UI |
| `GET /api/status` | per-module status counters |
| `GET /api/audio/meta` | channel labels, display bin frequencies, DSP params |
| `POST /api/audio/reset-peak` | body `{"channel": <position>}`, or `{}` for all |
| `WS /ws/audio` | one JSON `meta` text message, then binary frames |

Binary frame (little-endian): `"TFS1"`, `uint32 seq`, `uint16 channels`,
`uint16 bins`, then `int16[channels][2][bins]` in centi-dBFS (`[c][0]` average,
`[c][1]` peak hold). Clients may send `{"cmd": "reset_peak", "channel": n|null}`.

## Roadmap

- Feedback / ringing detector (narrow, persistent peaks rising above the
  averaged spectrum), shown as markers and a list in the UI.
- RF / wireless mic module.
