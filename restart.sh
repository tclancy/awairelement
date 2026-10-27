#!/usr/bin/env bash
# Update flow on homelab: itguy deploy awairelement (= git pull + this script)
# Restarts each unit that's actually installed, so poller-only boxes and
# mid-migration states don't fail.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
# Derived from systemd/, not listed: a unit added to the repo and installed on
# the box but missing from a hand-kept list here is a process that never picks
# up a deploy -- it keeps running the code it started with, silently, for ever.
for unit in $(cd systemd && printf '%s\n' *.service | sed 's/\.service$//'); do
  if systemctl --user cat "$unit" >/dev/null 2>&1; then
    systemctl --user restart "$unit"
    echo "$unit: $(systemctl --user is-active "$unit")"
  fi
done
