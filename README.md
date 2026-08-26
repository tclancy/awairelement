# awairelement

Continuous local logging, spike alerting, and a small trend dashboard for an
[Awair Element](https://uk.getawair.com/products/element) air quality monitor.

Two long-running processes:

- **Poller** — hits the Awair Element's Local API every 30 seconds, stores every
  reading in SQLite, runs spike detection, and fires [ntfy](https://ntfy.sh)
  notifications when CO2 / TVOC / PM2.5 climb above their baseline or a hard
  ceiling.
- **Dashboard** — a small Flask app that renders 7d / 30d small-multiple charts
  of every metric, with detected spikes overlaid.

Design and rationale live in [SCOPE.md](SCOPE.md); canonical vocabulary lives
in [GLOSSARY.md](GLOSSARY.md).

## Requirements

- An **Awair Element** with the **Local API enabled** — the "Test Mode" /
  Local API toggle in the Awair app. On current firmware this toggle is gone,
  so if it was never enabled on your device you may not be able to turn it on
  now; this project can't help you re-enable it. `curl http://<device-ip>/air-data/latest`
  should return JSON.
- **Python 3.13+** and [uv](https://github.com/astral-sh/uv). No other runtime
  dependencies — the poller is stdlib-only; the dashboard uses Flask + gunicorn.
- (Optional, for alerts) an **[ntfy](https://ntfy.sh) topic** — either the
  public server or your own. You'll need the topic name and, if the topic is
  protected, an access token.

## Quick start

```bash
git clone https://github.com/tclancy/awairelement.git
cd awairelement
uv sync

# Point at your device and pick a DB path.
export AWAIR_URL="http://192.168.1.42/air-data/latest"   # your Element's LAN IP
export AWAIR_DB="$HOME/data/awairelement/awair.db"

# Run the poller. It creates the DB (and parent directory) on first run.
# Rows start accumulating; spike detection is disabled until ~6h of readings
# exist (see SCOPE.md → "Spike Detection → Cold start").
uv run python -m awair.poller
```

In a second shell:

```bash
export AWAIR_DB="$HOME/data/awairelement/awair.db"
uv run gunicorn -w 2 -b 127.0.0.1:8097 'awair.web:create_app()'
# Dashboard at http://127.0.0.1:8097/
```

If you don't want gunicorn, `uv run flask --app 'awair.web:create_app()' run`
works for local poking too.

## Configuration

All configuration is via environment variables. Only `AWAIR_URL` is required
in practice — every other var has a working default, and the ntfy vars can be
omitted entirely to run without alerts.

| Variable | Default | Notes |
|----------|---------|-------|
| `AWAIR_URL` | `http://192.168.68.51/air-data/latest` | The Local API URL for your device. Change this. |
| `AWAIR_DB` | `~/data/awairelement/awair.db` | SQLite path. The parent directory is created on first poll. |
| `AWAIR_POLL_SECONDS` | `30` | Seconds between polls. The device refreshes internally every ~10s, so shorter intervals just add duplicate-timestamp skips. |
| `AWAIR_NTFY_URL` | `https://notifications.tomclancy.info` | ntfy server root. Use `https://ntfy.sh` for the public server. |
| `AWAIR_NTFY_TOPIC` | `awair` | ntfy topic name. Pick your own. |
| `AWAIR_NTFY_TOKEN` | *(unset)* | Access token if your topic is protected. Empty string = no auth header sent. |
| `AWAIR_TZ` | `UTC` | IANA zone (e.g. `America/New_York`) for the dashboard's sunrise/sunset markers. Ignored if `AWAIR_LAT` / `AWAIR_LON` are unset. |

To disable ntfy entirely, leave `AWAIR_NTFY_TOKEN` unset and pick a topic
nobody's listening on — the poller will still POST but no one will see the
messages. (There's no explicit off-switch; alerting failures never block
ingestion, so a wrong URL or 401 just gets logged.)

## What you get

- **Every reading, every 30 seconds**, in a single SQLite `readings` table:
  score, temp, humidity, absolute humidity, dew point, CO2 (measured and
  estimated), TVOC (measured and baseline), the two raw VOC channels
  (`voc_h2_raw`, `voc_ethanol_raw`), and PM2.5. Timestamps are UTC.
- **Spike detection** — one ntfy notification when a metric opens an event,
  one when it clears. Tier 1 is a relative spike (>6× rolling MAD above the
  24h median for 4 consecutive polls); tier 2 is a hard ceiling (CO2 > 1200,
  TVOC > 1000, PM2.5 > 35 for 2 polls). Hysteresis prevents flapping. See
  [SCOPE.md → "Spike Detection"](SCOPE.md) for the full math and tunables.
- **Device health alerts** — 10 consecutive fetch failures fire an
  "unreachable" event; 10 polls with an unchanged device timestamp fire a
  "stale" event (the wedged-but-HTTP-200 failure mode after some firmware
  updates). Recovery closes the event.
- **Dashboard** — small multiples for CO2, TVOC, PM2.5, temp, humidity, and
  score over 7d or 30d. Detected events overlay as shaded spans. LAN-only by
  default (no auth).
- **`GET /api/latest`** — the newest reading plus every open event, as
  read-only JSON, for the house hub to poll (#70). awairelement stays the
  system of record; the hub stores nothing and derives its own card colour
  from `score`. Two things about this endpoint differ from its siblings on
  purpose, because it feeds a machine rather than a browser: it is **always
  Celsius whatever `TEMPERATURE_UNIT` says** (and says so, via `temp_unit`),
  and it publishes **both** timestamps — `ts` is the Element's clock, for a
  human to read as "as of", while `received_at` is this machine's and is the
  one a consumer should measure staleness against. Measuring age off `ts`
  alone is a subtraction across two clocks: a device running fast yields a
  negative age, so a dead poller would render healthy forever. An empty
  `readings` table answers 200 with `"reading": null`, not an error, so a
  consumer can tell "up, but the poller is dead" from "unreachable".
  `open_events` carries **spike events only** — the device-health event
  `poller.handle_device_health` opens (`metric="device"`, no peak/baseline/
  threshold) is filtered out, because a consumer learns the same thing earlier
  from `received_at` going stale and publishing it would make three numeric
  fields nullable for everyone.

- **`GET /api/outdoor-latest`** — the newest outdoor reading, whole, as
  read-only JSON, for the house hub's weather card (#71). The outdoor sibling
  of `/api/latest` and it inherits both of that endpoint's machine-facing
  rules: **source units regardless of `TEMPERATURE_UNIT`**, and an empty table
  answers 200 with `"reading": null` rather than an error.

  It publishes **three** clocks. `ts` is Open-Meteo's publish time for the
  weather block (what a human reads as "as of"); `received_at` is this
  machine's poll time and is the one to measure staleness against; and `aq_ts`
  dates the *air-quality* half specifically. That third one is the
  non-obvious one: air quality is CAMS-backed and hourly while weather is
  quarter-hourly, so a row stamped 14:15 routinely carries a `us_aqi` measured
  at 13:00. Before #71 that timestamp was parsed and thrown away, so a card
  saying "as of 14:15" over an hour-old AQI had no way to know better. A NULL
  `aq_ts` means the AQI has no known observation time — the air-quality fetch
  failed for that poll, or the row predates #71 — and the hub is expected to
  read that as *"no current AQI"* (yellow) rather than folding an undateable
  number into a green.

  Two units are named in the payload, `temp_unit` and `pressure_unit`, and
  only two. Those are the two values this app converts somewhere else:
  `temp_unit()` turns temp into F for every browser-facing endpoint, and
  `/api/outdoor-series` divides pressure by `_HPA_PER_INHG` to hand the
  dashboard inHg. A consumer reading raw hPa here and inHg there, with neither
  labelled, would have no way to notice. Everything else has exactly one
  spelling in this codebase — Open-Meteo's native km/h (`wind_speed`), mm
  (`precipitation`) and µg/m³ (`pm25`, `pm10`) — so labelling them would be
  documentation rather than disambiguation.

  `weather_code` is the WMO interpretation code as the **integer** the source
  published. The code→word mapping is the hub's, for the same reason
  `/api/latest` ships open events rather than a card colour: awairelement is
  the system of record and publishes facts, the consumer renders. Rows written
  before #71 carry NULL for both new columns — the migration deliberately does
  not backfill either, since inventing an observation time is precisely what
  `aq_ts` exists to prevent. **Expect NULLs for the first quarter-hour after
  deploy**, until the outdoor poller writes its first post-migration row. That
  is correct behaviour and it will look like a bug to whoever is watching.

## Running as a systemd user service

The `systemd/` directory ships two unit files you can drop into
`~/.config/systemd/user/`. They assume the checkout lives at
`~/sources/awairelement` and read environment from
`~/.config/awairelement/environment`.

```bash
mkdir -p ~/.config/systemd/user ~/.config/awairelement
ln -sf "$PWD/systemd/awairelement.service"     ~/.config/systemd/user/
ln -sf "$PWD/systemd/awairelement-web.service" ~/.config/systemd/user/

cat > ~/.config/awairelement/environment <<'EOF'
AWAIR_URL=http://192.168.1.42/air-data/latest
AWAIR_DB=/home/YOU/data/awairelement/awair.db
AWAIR_NTFY_URL=https://ntfy.sh
AWAIR_NTFY_TOPIC=your-topic-name
AWAIR_NTFY_TOKEN=
EOF

systemctl --user daemon-reload
systemctl --user enable --now awairelement awairelement-web
journalctl --user -u awairelement -f       # follow poller logs
```

Config (environment): `AWAIR_URL` (default `http://192.168.68.51/air-data/latest`),
`AWAIR_DB` (default `~/data/awairelement/awair.db`), `AWAIR_POLL_SECONDS` (default 30),
`TEMPERATURE_UNIT` (default `C`, also accepts `F` and `K`; display-only — storage
stays Celsius).

If your checkout isn't at `~/sources/awairelement`, edit `WorkingDirectory=`
and `ExecStart=` in each unit before symlinking.

### Fan mitigation — retired

Automatic fan mitigation (issues #10 / #14) is **retired** as of
[#61](https://github.com/tclancy/awairelement/issues/61). The poller does not
drive the ceiling fans, and `AWAIR_FAN_MITIGATION_ENABLED=true` no longer
re-enables it — `awair.fans.MITIGATION_RETIRED` overrides the variable and logs
a warning if a deploy still sets it.

Why: measured over 303 hours, the fans ran 97 of them — a 32% duty cycle, 25
hours of it overnight, with one unbroken 34-hour span. Spike events in this
house last half a day, so "mitigate the spike" meant "run the fans a third of
the time". Full reasoning and the condition under which we'd bring it back:
[ADR-001](docs/decisions/001-retire-automatic-fan-mitigation.md).

A poller running with mitigation off **releases** the fans: any fan it still
believes it left running gets one `off` command on the next poll that stores a
reading, then it stops touching them. (No reading, no release — if the Awair is
unreachable the fans stay put until it recovers.) Turning a fan on at the wall
afterwards is safe: the poller will not fight you. If the NodeMCU can't be
reached it retries a handful of times, then sends one high-priority ntfy asking
you to use the wall switch rather than retrying forever.

The machinery is intact and fully tested. Re-enabling is three deliberate edits —
`MITIGATION_RETIRED = False`, the `test_fan_mitigation_ships_retired` test that
pins it, and the homelab `awair_fan_mitigation_enabled` variable — plus whatever
change made it worth having again; see the ADR. `AWAIR_FAN_HOST` (default
`192.168.68.68`, the NodeMCU on the LAN) still configures where commands go.

Smoke-test the fan + ntfy plumbing — `--test` deliberately ignores every gate
above, including the retirement:

```bash
python -m awair.poller --test   # fans to speed1, sends "Fan test", exits
```

A running poller releases them off again on the next poll or two that stores a
reading (after the 60s rate limit).

## Deploy (homelab)
`restart.sh` in the repo root runs `uv sync --frozen` and restarts both units
— use it as a one-shot after a `git pull`.

To have the units start at boot rather than only after login, run
`loginctl enable-linger $USER` once.

## Development

```bash
uv sync
uv run pytest              # runs the full test suite with coverage
uv run pre-commit install  # ruff + trailing-whitespace + radon complexity gate
```

`awair/spikes.py` is a pure-function module and carries most of the
interesting unit tests — the poller and web modules are thin glue tested with
an in-memory SQLite fixture. See `tests/`.

## Layout

```
awair/
├── db.py        # connection PRAGMAs + idempotent schema bootstrap
├── poller.py    # the 30s loop: fetch → store → detect → alert
├── spikes.py    # baseline math + hysteresis (pure functions)
├── monitor.py   # device health checks (unreachable, stale)
├── alerts.py    # ntfy client
├── series.py    # server-side bucketing for the dashboard
└── web.py       # Flask app + JSON endpoints
```

## License

MIT — see [LICENSE](LICENSE).
