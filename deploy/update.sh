#!/usr/bin/env bash
# Pull the latest code from GitHub, reinstall the package and restart the service,
# rolling back to the previous revision if any step fails.
# Runs ON THE SERVER as the service user:
#   bash deploy/update.sh [remote-url] [branch]
# Remote and branch default to $UPDATE_REMOTE / $UPDATE_BRANCH (the /config page
# writes them to .update.env), then to this repo's main.
#
# The /config page's Update button starts this through the tf-daemon-update
# unit (TF_UPDATE_DETACHED=1), so it survives restarting tf-daemon and can check
# that the new version comes up healthy before declaring success. Without that
# unit it runs inside tf-daemon, and the restart ends it: it then records
# "restarting" and the new process marks the update done when it starts.
#
# Progress is written to .update.json ({"state", "from", "to", "message", "pid"});
# state is running | restarting | done | up_to_date | failed | rolled_back.
# A copy deployed with deploy.sh (no .git) becomes a git checkout on the first
# run. The server's config.toml is kept. Restarts use the sudoers rules from
# install-service.sh.
set -Eeuo pipefail

# Everything is inside main() so bash has read the whole script before
# `git reset` replaces this file on disk.
main() {
  local remote=${1:-${UPDATE_REMOTE:-https://github.com/hcs-auditoriums/tf-daemon.git}}
  local branch=${2:-${UPDATE_BRANCH:-main}}
  HEALTH_URL=${UPDATE_HEALTH_URL:-http://127.0.0.1:8080/api/system/info}
  cd "$(dirname "$0")/.."
  APP_DIR=$PWD
  OLD=none NEW=none
  trap 'state failed "update script stopped unexpectedly at line $LINENO"' ERR

  state running "fetching $branch"
  echo "==> fetching $branch from $remote"
  if [ ! -d .git ]; then
    git init -q
    git symbolic-ref HEAD "refs/heads/$branch"
  fi
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$remote"
  else
    git remote add origin "$remote"
  fi
  if ! git fetch -q origin "$branch"; then
    fail failed "could not fetch $branch from $remote"
  fi

  OLD=$(cat REVISION 2>/dev/null || git rev-parse -q --verify HEAD || echo none)
  NEW=$(git rev-parse FETCH_HEAD)
  if [ "$OLD" = "$NEW" ] && git rev-parse -q --verify HEAD >/dev/null; then
    echo "==> already up to date (${NEW:0:7})"
    state up_to_date "already at ${NEW:0:7}"
    return 0
  fi

  echo "==> ${OLD:0:7} -> ${NEW:0:7}"
  git --no-pager log --oneline -n 20 "$NEW" --not "$OLD" 2>/dev/null || true

  state running "installing ${NEW:0:7}"
  if ! checkout "$NEW" || ! install_pkg; then
    rollback "installing ${NEW:0:7} failed"
  fi

  unit_note

  if [ "${TF_UPDATE_DETACHED:-0}" != 1 ]; then
    # Inside tf-daemon: the restart stops this script too, so hand over now.
    state restarting "restarting into ${NEW:0:7}"
    echo "==> restarting service"
    sudo -n systemctl restart --no-block tf-daemon
    return 0
  fi

  echo "==> restarting service"
  state running "restarting into ${NEW:0:7}"
  if ! sudo -n systemctl restart tf-daemon || ! healthy "$NEW"; then
    rollback "${NEW:0:7} did not start"
  fi
  echo "==> done: running ${NEW:0:7}"
  state done "updated to ${NEW:0:7}"
}

# state STATE MESSAGE: record progress for the /config page (atomically).
state() {
  printf '{"state": "%s", "from": "%s", "to": "%s", "message": "%s", "pid": %d, "time": %d}\n' \
    "$1" "$OLD" "$NEW" "${2//\"/\'}" "$$" "$(date +%s)" > .update.json.tmp
  mv .update.json.tmp .update.json
}

# fail STATE MESSAGE
fail() {
  echo "==> FAILED: $2"
  trap - ERR
  state "$1" "$2"
  exit 1
}

# checkout REV: move the checkout to REV, keeping the server's config.toml
# (it is tracked but edited on the server).
checkout() {
  local rc=0
  if [ -f config.toml ]; then cp -p config.toml config.toml.keep; fi
  git reset -q --hard "$1" || rc=$?   # also overwrites the untracked files of a deploy.sh copy
  if [ -f config.toml.keep ]; then mv config.toml.keep config.toml; fi
  if [ $rc -eq 0 ]; then echo "$1" > REVISION; fi
  return $rc
}

install_pkg() {
  echo "==> installing python package"
  # ignore::UserWarning drops setuptools' "Editable installation" notice (the
  # checkout *is* the package, so its shadowing caveat doesn't apply); errors
  # and deprecation warnings still show.
  PYTHONWARNINGS=ignore::UserWarning .venv/bin/pip install -q -e '.[rf]'
}

# healthy REV: wait for the service to answer and report REV.
healthy() {
  echo "==> waiting for the service to come up"
  local i tries=${UPDATE_HEALTH_TRIES:-45}
  for i in $(seq 1 "$tries"); do
    sleep 2
    if .venv/bin/python - "$HEALTH_URL" "$1" 2>/dev/null <<'EOF'
import json, sys, urllib.request
info = json.load(urllib.request.urlopen(sys.argv[1], timeout=3))
sys.exit(0 if info.get("revision") == sys.argv[2] else 1)
EOF
    then
      return 0
    fi
  done
  echo "==> the service did not report ${1:0:7} within $((tries * 2)) s; recent log:"
  journalctl -u tf-daemon -n 30 --no-pager -o cat 2>/dev/null || true
  return 1
}

# rollback REASON: put the previous revision back and restart it.
rollback() {
  echo "==> $1"
  if [ "$OLD" = none ] || ! git cat-file -e "$OLD^{commit}" 2>/dev/null; then
    fail failed "$1; no previous revision to roll back to"
  fi
  echo "==> rolling back to ${OLD:0:7}"
  state running "$1; rolling back to ${OLD:0:7}"
  { checkout "$OLD" && install_pkg; } || echo "==> rollback install failed too"
  if [ "${TF_UPDATE_DETACHED:-0}" = 1 ]; then
    sudo -n systemctl restart tf-daemon || echo "==> restarting ${OLD:0:7} failed"
  fi
  fail rolled_back "$1; rolled back to ${OLD:0:7}"
}

unit_note() {
  # Installing units needs root, which this script deliberately doesn't have.
  local f
  for f in tf-daemon tf-daemon-update; do
    if ! sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$APP_DIR|g" "deploy/$f.service" \
        | cmp -s - "/etc/systemd/system/$f.service" 2>/dev/null; then
      echo "==> NOTE: deploy/$f.service is not installed as shipped; run  sudo bash deploy/install-service.sh  to apply it"
    fi
  done
}

main "$@"
exit
