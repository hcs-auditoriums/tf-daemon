# Configuration

Everything lives in `config.toml`; any missing key falls back to the defaults in
`tf_daemon/config.py`. Highlights:

- Every device has a `source`: `audio.source` is `device`, `synth` or `wav`
  (with `audio.wav = "path"`); `rf.source`, `qlxd.source` and `tf5.source` are
  `device`, `demo` (simulated) or `off`. The command-line flags (`--source`,
  `--rf-source`, ..., `--demo`) and the `TF_DAEMON_*_SOURCE` variables override
  them. `enabled = false` under `[rf]` / `[qlxd]` (from earlier versions) still
  means `off`. Unknown keys are logged at startup and ignored.
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
- `[rf]`: `source` (`device` = tinySA, `demo`, `off`), `auto_connect`
  (default `true`) and the default sweep (`start_hz`, `stop_hz`, `points` ≤ 450,
  `rbw` `"auto"` or kHz, `interval_s`). With `auto_connect` the tinySA is opened
  and swept from startup, nobody needs to press Connect, and the range is the
  span of the known QLX-D receivers (`start_hz`/`stop_hz` only apply when none
  are known). With `auto_connect = false` it opens only on **Connect**.
- `[qlxd]`: `source` (`device`, `demo`, `off`), `interface` (NIC the
  receivers are on; `""` = first physical, up interface with a 169.254.x.x
  address), `hosts` (receivers to always monitor, `"ip"` or `"ip:port"`),
  `state_file`, `meter_poll_s`, `full_poll_s`. Interface and hosts are normally
  set on the `/qlxd` page (**Network…**); that choice is saved to `state_file`
  (default `qlxd_state.json` in the working directory) and overrides config.toml.
- `[tf5]`: `source` (`device`, `demo`, `off`), `host` (console IP; `""` = off), `port` (49280), `username` /
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
  EQ over the TF Editor protocol (`tf_daemon/console/editor/`, protocol notes in
  `guides/tf5_api.md`) and offer an **EQ** toggle that overlays the console's EQ
  curve on the spectrum (at the spectrum's dB scale from the View range, 0 dB at
  mid-height; band handles sit on the curve). The **Console** sidebar also gets an
  **EQ** section: on/off, a small curve graph (same scale as on the spectrum),
  and the HPF / low / high band shapes, live with every change on the desk. The curve
  is computed with TF Editor's own filter maths (decompiled; see the guide's EQ
  curve section). The client is **read-only** by default: it joins as a
  StageMix client with `SYNCDIR:2` (console → client), and a guard refuses to
  send anything but the subscribe / hash / sync / keepalive handshake.
  `tf5.eq_write` (default `false`) allows one exception, the vocal assist's
  **Apply to desk** / **Undo**: on that click the guard lets through exactly the
  confirmed set-param messages, only for input-channel PEQ parameters (switch,
  HPF, bands) with values in range. `GET /api/tf5/eq/<usb>` returns the settings and
  curve. One-off read-only probe from a shell:
  `python -m tf_daemon.console.editor <console-ip> --channels 1-8 [--watch]`.

Levels are dBFS with AES17 convention: a full-scale sine reads 0 dBFS.
