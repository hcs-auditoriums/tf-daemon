# Server install (Raspberry Pi OS or Ubuntu)

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
checkout, and the server's `config.toml` is always kept.

To try something out without merging it, **Run another branch or commit** on the same
page switches to any branch of `update.remote` (picked from a list) or a commit id (full
or the short 7-character form), the same way: install, restart, roll back if it doesn't
come up. The page then says which one is running, and **Update now** goes back to
`update.branch`. From a shell: `bash deploy/update.sh <remote> <branch> <branch-or-commit>`.

The same page edits `config.toml`, one setting at a time or as text (validated before it is
saved), and restarts the service to apply it. The restart and update rely on the sudoers rules that `install-service.sh`
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
| `TF_DAEMON_SOURCE` | `audio.source` | `device`, `synth` or `wav` |
| `TF_DAEMON_WAV` | | WAV path for `wav` |
| `TF_DAEMON_RF_SOURCE` | `rf.source` | `device`, `demo` or `off` |
| `TF_DAEMON_QLXD_SOURCE` | `qlxd.source` | `device`, `demo` or `off` |
| `TF_DAEMON_TF5_SOURCE` | `tf5.source` | `device`, `demo` or `off` |
| `TF_DAEMON_BIND` | `0.0.0.0:8080` | listen address |
| `TF_DAEMON_THREADS` | `8` | gunicorn threads |
| `TF_DAEMON_LOG` | `INFO` | log level |
