#!/usr/bin/env bash
# Pull the latest code from GitHub, reinstall the package and restart the service.
# Runs ON THE SERVER as the service user, from anywhere:
#   bash deploy/update.sh [remote-url] [branch]
# The /config page's Update button runs this. A copy deployed with deploy.sh
# (no .git) is turned into a git checkout on the first run. The server's
# config.toml is kept. The restart needs the sudoers rule from install-service.sh.
set -euo pipefail

# Everything is inside main() so bash has read the whole script before
# `git reset` replaces this file on disk.
main() {
  local remote=${1:-https://github.com/hcs-auditoriums/tf-daemon.git}
  local branch=${2:-main}
  cd "$(dirname "$0")/.."

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
  git fetch -q origin "$branch"

  local old new
  old=$(cat REVISION 2>/dev/null || git rev-parse -q --verify HEAD || echo none)
  new=$(git rev-parse FETCH_HEAD)
  if [ "$old" = "$new" ] && git rev-parse -q --verify HEAD >/dev/null; then
    echo "==> already up to date (${new:0:7})"
    return 0
  fi

  echo "==> ${old:0:7} -> ${new:0:7}"
  git --no-pager log --oneline -n 20 "$new" --not "$old" 2>/dev/null || true

  # config.toml is tracked but edited on the server: keep the local copy.
  [ -f config.toml ] && cp -p config.toml config.toml.keep
  git reset -q --hard FETCH_HEAD   # also overwrites the untracked files of a deploy.sh copy
  [ -f config.toml.keep ] && mv config.toml.keep config.toml
  echo "$new" > REVISION

  echo "==> installing python package"
  .venv/bin/pip install -q -e '.[rf]'

  # Installing the unit needs root, which this script deliberately doesn't have.
  local unit=/etc/systemd/system/tf-daemon.service
  if [ -f "$unit" ] && ! sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$PWD|g" deploy/tf-daemon.service | cmp -s - "$unit"; then
    echo "==> WARNING: deploy/tf-daemon.service changed; run  sudo bash deploy/install-service.sh  to apply it"
  fi

  echo "==> restarting service"
  sudo -n systemctl restart --no-block tf-daemon
  echo "==> done"
}

main "$@"
exit
