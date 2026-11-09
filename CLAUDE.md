# tf-daemon: notes for Claude

## Testing: use the dev container, don't improvise

All hardware (TF5, QLX-D, tinySA, audio) is simulated in the dev container. Don't
write ad-hoc fakes or plan how to test against real devices; this is the recipe:

1. `docker compose ps`. If `app` isn't `healthy`: `docker compose up -d --build`.
   (`./dev shell | test | logs` are the human shortcuts; `dev.cmd` on Windows.)
2. `docker compose exec -T app pytest -q` runs the whole suite in parallel (~10 s),
   including `tests/test_live.py` against the running server and
   `tests/test_browser.py` (the pages in headless Chromium). Target with a path
   or `-k`; `-n 0` for serial. For a change to the pages (`tf_daemon/static/`),
   add or extend a browser check rather than eyeballing it.
3. The server reloads on save. Check behaviour with
   `curl -s localhost:8080/api/status` (and `/api/tf5/status`, `/api/qlxd/state`,
   `/api/rf/state`, `/api/audio/meta`, `/api/tf5/eq/<n>`).
   `docker compose logs --tail 50 app` shows the server log.
4. Before committing: `docker compose exec -T app ruff check .` and
   `docker compose exec -T app mypy` (CI runs both, plus the tests).
5. For a new feature, add a unit test next to the existing ones, and a check in
   `tests/test_live.py` if it shows up in the running app.

Test doubles live in `tf_daemon/console/sim.py` (TF5 RCP + TF Editor), `tf_daemon/qlxd/sim.py`,
the RF demo device in `tf_daemon/rf/devices.py` and the synth source in
`tf_daemon/audio/sources.py`. Reuse and extend those, don't make new ones. Tests use
ephemeral ports and `tmp_path`, so they are safe in parallel. Keep it that way.
The TF Editor protocol tests are in `tests/editor/` (fixtures in its `conftest.py`, shared
doubles and `assert_read_only` in `tests/editor_fakes.py`). `tests/editor/test_guard.py` pins
the write path: a change there needs a deliberate review, never a loosened assertion.
When a test waits on a simulated device, wait for the condition (`wait_until`,
`wait_settled` in `tests/test_tf5.py`), never a fixed sleep.

The host `.venv` may be stale; don't rely on it.
