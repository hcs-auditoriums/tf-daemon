# Development

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
python -m tf_daemon --demo --config config.dev.toml
```

`--demo` simulates all the hardware: a stage, a demo tinySA, four QLX-D
receivers and a TF5 console (names, faders, EQ read and EQ write). It is short for
`--source stage --rf-source demo --qlxd-source demo --tf5-source demo`; each flag
also works on its own.

The simulated stage (`tf_daemon/audio/stage.py`) is a closed loop: the named
inputs are talkers taking turns, each channel goes through the simulated desk's
current EQ and fader into the mains (USB 33/34), the PA leaks back into the mics
after ~17 ms through a couple of resonances, and a house mic hears the mains
through a reverberant room. So an EQ change on the desk is heard in the mains and
the house mic, and too much gain really rings (at 820 Hz and 2.48 kHz first). The
plain `synth` source (same voice, no desk, no loop) is still there for tests. `config.dev.toml` turns on every feature and names a few
inputs. Or use the dev container (next section), which needs only Docker.

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

Run the tests with `pytest` (`pytest -n auto` runs them in parallel).

## Dev container (Docker, any platform)

The same environment on macOS, Windows and Linux, with only Docker installed:
the demo server runs in a container on the code on your disk and restarts
whenever you save a file under `tf_daemon/` or `config.dev.toml`.

```bash
docker compose up -d --build        # build (first time, or after pyproject.toml changes) and start
```

Open <http://localhost:8080>. In VS Code, **Dev Containers: Reopen in Container**
(or a Codespace) uses the same container, with the server already running.

Shortcuts. These start the container if it isn't running. Use `./dev` on macOS / Linux,
`dev` in Windows `cmd` and `.\dev` in PowerShell:

```bash
./dev shell                          # bash in the container
./dev test                           # all tests, in parallel
./dev test tests/test_tf5.py -k reconnects   # any pytest arguments
./dev logs                           # follow the server log
./dev lint                           # ruff + mypy
./dev open                           # open the running app in the browser (./dev open rf | qlxd | config)
./dev up                             # start, rebuilding if pyproject.toml changed
./dev down                           # stop
./dev help                           # list the commands
```

The full `docker compose` commands they run:

| What | Command |
| --- | --- |
| Server log, live (each reload shows up here) | `docker compose logs -f app` |
| Last 100 lines | `docker compose logs --tail 100 app` |
| Shell in the container | `docker compose exec app bash` |
| Run all tests (parallel, ~10 s) | `docker compose exec app pytest` |
| Lint and type-check | `docker compose exec app ruff check .` and `docker compose exec app mypy` |
| One file / one test | `docker compose exec app pytest tests/test_tf5.py -k reconnects` |
| Serial, e.g. to use `pdb` | `docker compose exec app pytest -n 0 --pdb` |
| Restart the server by hand | `docker compose restart app` |
| Is it healthy? | `docker compose ps` |
| Stop | `docker compose down` |
| Rebuild after a dependency change | `docker compose up -d --build` |

Inside the container, `pytest` runs on every core (`PYTEST_ADDOPTS` in
`compose.yaml`) and includes `tests/test_live.py`, which checks a running
server end to end: every simulated device up, the pages, the console's names
and EQ, and the WebSockets. `tests/test_browser.py` loads the pages in headless
Chromium (Playwright, in the dev image) and fails on any script error, a plot
that stops drawing, or a control that no longer works. Both run against a
private demo server that `tests/conftest.py` starts for the run (a free port,
throwaway state files), not the dev server on :8080: saving a file doesn't
restart it mid-test, and the tests can't change your demo. It only starts when
the run includes those tests, so `pytest tests/test_dsp.py` stays instant. Set
`TF_DAEMON_URL` to use a server of your own instead (outside the container the
browser tests also need `pip install playwright && playwright install chromium`).

Run the tests for what you changed first (`pytest tests/test_x.py` or `-k name`)
and the whole suite once at the end; failures print short tracebacks.

CI (`.github/workflows/ci.yml`) runs `ruff`, `mypy` and the tests in this same
container image on every pull request and push to `main` (changes to Markdown
files or `guides/` alone skip it). The image is built only when
`.devcontainer/Dockerfile` or the dependency lists in `pyproject.toml` change, and
kept in GHCR as `tf-daemon-dev:deps-<hash>`; other runs pull it and run inside it. `./dev lint` and `./dev test`
run the same checks locally. `ruff check --fix .` fixes the simple cases (import order,
unused imports). The settings for both tools are in `pyproject.toml`.

On Linux, if your user ID isn't 1000, build with `DEV_UID=$(id -u) docker compose up -d --build`
so files written in the container stay yours.

Notes:

- The container runs the Flask development server, not gunicorn, and has no
  systemd, so the Config page's **Restart** and **Update now** do nothing there.
  Saving on the Config page writes `config.dev.toml`.
- QLX-D settings edited on the Wireless page go to `.dev/qlxd_state.json`
  (ignored by git), not the real `qlxd_state.json`.
- The image sets `OPENBLAS_NUM_THREADS=1`. Without it numpy's BLAS keeps a
  thread per core spinning, and the idle demo server uses about 2.5 cores.
