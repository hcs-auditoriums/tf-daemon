#!/usr/bin/env bash
# Push the committed code (HEAD) to the server and restart the service.
#   deploy/deploy.sh [user@host]          (default: audman@10.99.40.22)
#
# Copies via `git archive`, so the server needs no GitHub access. The server's
# config.toml is kept if it already exists. Uncommitted changes are NOT sent.
# The restart uses sudo, so you may be asked for the server user's password.
# First-time setup: run deploy/install-service.sh on the server instead of the restart.
set -euo pipefail

HOST=${1:-audman@10.99.40.22}
DIR=${TF_DAEMON_DIR:-tf-daemon}   # relative to the remote user's home
cd "$(git rev-parse --show-toplevel)"

if ! git diff --quiet HEAD; then
  echo "warning: uncommitted changes are not deployed" >&2
fi

echo "==> copying $(git rev-parse --short HEAD) to $HOST:~/$DIR"
git archive --format=tar HEAD | ssh "$HOST" "
  set -e
  mkdir -p '$DIR'
  if [ -f '$DIR/config.toml' ]; then tar -xm -C '$DIR' --exclude=config.toml; else tar -xm -C '$DIR'; fi
  echo $(git rev-parse HEAD) > '$DIR/REVISION'
"

if [[ ${NO_RESTART:-0} == 1 ]]; then exit 0; fi

echo "==> updating venv and restarting"
ssh -t "$HOST" "
  set -e
  cd '$DIR'
  if [ -x .venv/bin/pip ]; then .venv/bin/pip install -q -e .; fi
  if systemctl cat tf-daemon >/dev/null 2>&1; then
    sudo systemctl restart tf-daemon && sleep 2 && systemctl --no-pager --lines=8 status tf-daemon
  else
    echo 'service not installed yet: run  sudo bash deploy/install-service.sh  in ~/$DIR'
  fi
"
