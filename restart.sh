#!/usr/bin/env bash
# Update flow on homelab: git pull && ./restart.sh  (sandy pattern)
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
systemctl --user restart awair-poller
systemctl --user --no-pager status awair-poller | head -5
