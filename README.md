# awairelement

Local logging, spike alerting, and trend dashboard for an Awair Element air
quality monitor. See [SCOPE.md](SCOPE.md) for the full design.

## Status

Slices 1–2 shipped: poller → SQLite, spike detection + ntfy alerts.
(Dashboard is slice 3.)

## Run locally

```bash
uv sync
AWAIR_DB=/tmp/awair.db uv run python -m awair.poller
uv run pytest
```

Config (environment): `AWAIR_URL` (default `http://192.168.68.51/air-data/latest`),
`AWAIR_DB` (default `~/data/awairelement/awair.db`), `AWAIR_POLL_SECONDS` (default 30),
`TEMPERATURE_UNIT` (default `C`, also accepts `F` and `K`; display-only — storage
stays Celsius).

## Deploy (homelab)

Provisioned by the `native-apps` Ansible role in the homelab repo
(`--tags awair`): clone to `~/sources/awairelement`, env file from vault to
`~/.config/awairelement/environment`, systemd user unit `awairelement.service`.

Day-to-day management is itguy (systemd shape — the unit name matches the
app name by convention):

```bash
itguy deploy awairelement   # git pull + ./restart.sh
itguy status awairelement
itguy logs awairelement     # journalctl --user -u awairelement under the hood
```

Logs live in the systemd journal:
`journalctl --user -u awairelement -f` on the box.
