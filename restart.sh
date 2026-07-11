#!/usr/bin/env bash
# Update flow on homelab: itguy deploy awairelement (= git pull + this script)
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
systemctl --user restart awairelement
systemctl --user is-active awairelement
