# awairelement

Local logging, spike alerting, and trend dashboard for an Awair Element air
quality monitor. See [SCOPE.md](SCOPE.md) for the full design.

## Status

Slice 1: poller → SQLite. (Spike detection/ntfy and the dashboard are slices 2–3.)

## Run locally

```bash
uv sync
AWAIR_DB=/tmp/awair.db uv run python -m awair.poller
uv run pytest
```

Config (environment): `AWAIR_URL` (default `http://192.168.68.51/air-data/latest`),
`AWAIR_DB` (default `~/data/awairelement/awair.db`), `AWAIR_POLL_SECONDS` (default 30).

## Deploy (homelab, interim manual — Ansible wiring lands in slice 4)

```bash
ssh tom@192.168.68.67
git clone git@github.com:tclancy/awairelement.git ~/sources/awairelement
cd ~/sources/awairelement && uv sync --frozen
mkdir -p ~/.config/systemd/user ~/data/awairelement
ln -sf ~/sources/awairelement/systemd/awair-poller.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now awair-poller
```

Update: `cd ~/sources/awairelement && git pull && ./restart.sh`
