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
| `AWAIR_FAN_MITIGATION_ENABLED` | `false` | Drive the ceiling fans on high CO2. See [Fan mitigation](#fan-mitigation). |
| `AWAIR_FAN_HOST` | `192.168.68.68` | The NodeMCU that relays fan commands. |
| `AWAIR_CO2_FAN_ON` | `1000` | ppm at or above which the fans come on. |
| `AWAIR_CO2_FAN_OFF` | `900` | ppm below which they go off again. The gap between the two is deliberate — see [Fan mitigation](#fan-mitigation). |
| `AWAIR_FAN_MAX_RUN_MINUTES` | `90` | Longest single fan run, regardless of CO2. |
| `AWAIR_PM25_SUPPRESS` | `100` | pm25 (µg/m³) at or above which fans are blocked outright. |
| `AWAIR_PM25_NEAR_MISS` | `50` | pm25 at or above which a watchpoint line is logged. Behaviour-neutral. |

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

### Fan mitigation

With `AWAIR_FAN_MITIGATION_ENABLED=true` the poller runs the ceiling fans while
CO2 is high. **On at 1000 ppm, off below 900**, one speed, and never for more
than 90 minutes at a stretch.

Three rules, in precedence order:

1. **PM2.5 suppresses.** At or above 100 µg/m³ the fans are blocked, and a
   running fan is forced off immediately — bypassing the rate limit, because
   fans re-suspend settled dust and that is the exact failure this prevents.
2. **The duration cap.** A run is bounded at `AWAIR_FAN_MAX_RUN_MINUTES`, after
   which the fans stay off until CO2 recovers below the off-threshold — not
   just until the 90 minutes elapse, or the fans would come straight back on.
3. **The CO2 band.** On at or above `AWAIR_CO2_FAN_ON`, off below
   `AWAIR_CO2_FAN_OFF`, hold in between. The gap is what stops the fans
   chattering as CO2 wanders across a single line at 30-second polls.

A missing or stale reading (older than 5 minutes) means **off** for both CO2
and PM2.5 — absent data is never treated as good news.

Why these numbers: fan mitigation was retired in
[#61](https://github.com/tclancy/awairelement/issues/61) after running the fans
32% of the time, and un-retired on this trigger only after measuring it against
eight weeks of real readings — **0.62% duty cycle, 7 runs, longest 1.5 hours,
no overnight running**. The reasoning, the measurements and what would reverse
it again: [ADR-002](docs/decisions/002-co2-only-fan-mitigation.md).

`tests/fixtures/co2_summer_2026.txt` holds that recording, and
`test_replay_summer_2026_stays_under_two_percent_duty` replays it through the
shipped rules on every test run — so lowering a threshold too far fails the
suite rather than surprising you in the kitchen. It is summer data; a winter
re-measurement is owed.

**Turning it off.** Unset the env var, or set
`awair.fans.MITIGATION_RETIRED = True` for a code-level kill switch that beats
the environment (it logs a warning if a deploy still asks for fans, and the
startup banner reads `disabled in code` rather than `off` — the two have
different fixes). Either way a poller with mitigation off **releases** the
fans: any fan it still believes it left running gets one `off` command on the
next poll that stores a reading, then it stops touching them. (No reading, no
release — if the Awair is unreachable the fans stay put until it recovers.)
Turning a fan on at the wall afterwards is safe: the poller will not fight you.
If the NodeMCU can't be reached it retries a handful of times, then sends one
high-priority ntfy asking you to use the wall switch rather than retrying
forever.

Smoke-test the fan + ntfy plumbing — `--test` deliberately ignores every gate
above, including the kill switch:

```bash
python -m awair.poller --test   # fans to speed1, sends "Fan test", exits
```

A running poller with mitigation off releases them again on the next poll or
two that stores a reading (after the 60s rate limit).

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
