# tf-daemon: notes for Claude

## Where things are

Each module's docstring says what it does (and lists its routes); read that before the code.
Full tree: `docs/architecture.md`.

- `tf_daemon/app.py`: builds and wires every module. `config.py`: every config key and default.
- `audio/`: capture (`device.py` PortAudio, `sources.py` synth / WAV, `stage.py` simulated
  stage) → `engine.py` analysis → `/ws/audio`. `dsp/`: FFT (`spectrum.py`), ring detection
  (`ring.py`, events in `ringlog.py`), the desk's EQ curve (`eq.py`), dB helpers (`db.py`).
- `console/`: the TF5. `rcp.py` names / faders, `editor/` EQ over the TF Editor protocol
  (`guard.py` decides what may be written), `module.py` routes, `sim*.py` the simulated desk.
- `vocal/`: the vocal EQ assist: `voice.py` → `tone.py` suggestion, `session.py` mic checks,
  `auto.py` auto EQ, `ringout.py` ring-out (+ `ringwatch.py`), `module.py` routes.
- `rf/` tinySA, `qlxd/` Shure receivers: `engine.py` + `module.py` each.
- `static/`: pages as ES modules sharing `common.js`; the audio page is `app.js` + `audio/*.js`.
- Tests: `tests/test_<area>.py`; shared helpers in `tests/helpers.py` (`make_config`,
  `wait_until`), offline stage rigs in `tests/offline.py`, fixtures in `tests/conftest.py`.

## Testing: use the dev container, don't improvise

All hardware (TF5, QLX-D, tinySA, audio) is simulated in the dev container. Don't
write ad-hoc fakes or plan how to test against real devices; this is the recipe:

1. `docker compose ps`. If `app` isn't `healthy`: `docker compose up -d --build`.
2. `./dev quick` while working: fix + format + mypy + every test but the slow ones (~5 s);
   only problems print. `./dev check [pytest args]` is the same with the slow tests
   (~20 s, includes `tests/test_live.py` and `tests/test_browser.py`, the pages in headless
   Chromium); run it once before committing. Target with a path or `-k`; `-n 0` for serial.
   For a change to the pages (`tf_daemon/static/`), add or extend a browser check rather
   than eyeballing it.
3. The server reloads on save. Check behaviour with
   `curl -s localhost:8080/api/status` (and `/api/tf5/status`, `/api/qlxd/state`,
   `/api/rf/state`, `/api/audio/meta`, `/api/tf5/eq/<n>`).
   `docker compose logs --tail 50 app` shows the server log.
4. Don't hand-fix lint or line length: `./dev check` runs the fixer, the formatter owns layout.
5. For a new feature, add a unit test next to the existing ones, and a check in
   `tests/test_live.py` if it shows up in the running app.

Test doubles live in `tf_daemon/console/sim.py` (the TF5: `sim_rcp.py` + `sim_editor.py`),
`tf_daemon/qlxd/sim.py`, the RF demo device in `tf_daemon/rf/devices.py` and the synth
source in `tf_daemon/audio/sources.py`. Reuse and extend those, don't make new ones. Tests
use ephemeral ports and `tmp_path`, so they are safe in parallel. Keep it that way.
The TF Editor protocol tests are in `tests/editor/` (the `console` / `client_for` fixtures in
`tests/conftest.py`, shared doubles and `assert_read_only` in `tests/editor_fakes.py`).
`tests/editor/test_guard.py` pins the write path: a change there needs a deliberate review,
never a loosened assertion.

## Standards

`tests/test_standards.py` and the slow-test limit (`tests/conftest.py`) check file size,
docstrings, the architecture map, test imports and sleeps; when one fails, fix the code,
don't loosen the check. Mark a test of a second or more `@pytest.mark.slow`. Reuse the
shared helpers (`files.py`, `dsp/db.py`, `console/patch.py`, `modules.py`,
`static/common.js`, `tests/helpers.py`, `tests/offline.py`) rather than writing another
copy; delete what nothing uses; a fact lives in one place and the others point to it.

The host `.venv` may be stale; don't rely on it.
