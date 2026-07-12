#!/usr/bin/env bash
# Update flow on homelab: itguy deploy awairelement (= git pull + this script)
# Restarts each unit that's actually installed, so poller-only boxes and
# mid-migration states don't fail.
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
for unit in awairelement awairelement-web awairelement-outdoor; do
  if systemctl --user cat "$unit" >/dev/null 2>&1; then
    systemctl --user restart "$unit"
    echo "$unit: $(systemctl --user is-active "$unit")"
  fi
done
