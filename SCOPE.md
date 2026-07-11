# awairelement — Scope

Continuous local logging of the Awair Element air quality monitor, spike alerting
via ntfy, and a small dashboard for spotting trends in CO2, TVOC, and PM2.5.

**Status:** DRAFT v2 (post-review) — awaiting Tom's sign-off
**Last updated:** 2026-07-10

## Goals

1. Poll the Awair Element Local API every 30 seconds and store every reading in SQLite.
2. Detect spikes in CO2, TVOC, and PM2.5 against a rolling baseline (not just fixed
   thresholds) and send a single ntfy notification per event.
3. Serve a dashboard with graphs over the last 7 days and last 30 days.
4. Capture enough raw sensor detail (`voc_ethanol_raw`, `voc_h2_raw`, humidity) to
   later distinguish alcohol-based VOC sources and humidity-driven false positives.
5. Alert when the device goes unreachable **or stale** (also catches the known
   firmware-update-disables-Local-API failure mode).

## Non-Goals (for now)

- Awair cloud API integration or historical import from Awair's servers.
- Multiple devices. One Element, one table.
- Data downsampling/retention policy. At 30s intervals a year is ~1M rows; SQLite
  doesn't care. Revisit if the dashboard queries ever feel slow.
- Automated cause classification ("this spike was cooking"). The dashboard and raw
  fields give us the data to eyeball this first; automation is a later slice if
  patterns emerge.
- Quiet hours for alerts. Deferred: fix the ceilings against the room's real
  occupancy first (see Decisions), and only add quiet-hours logic if real alerts
  prove annoying in practice.
- Public exposure of the dashboard. LAN-only to start (see Decisions).

## Device Facts (established)

- Element at **192.168.68.51** (needs a DHCP reservation if it doesn't have one).
- Local API already enabled (legacy — the toggle is gone from the current app, so
  **never factory-reset the device casually**; we may not be able to re-enable it).
- `GET http://192.168.68.51/air-data/latest` — no auth, no rate limit, device
  refreshes internally every ~10s.
- Response fields: `timestamp`, `score`, `dew_point`, `temp`, `humid`, `abs_humid`,
  `co2`, `co2_est`, `co2_est_baseline`, `voc`, `voc_baseline`, `voc_h2_raw`,
  `voc_ethanol_raw`, `pm25`, `pm10_est`.

## Architecture

Django project, one app (`readings`). Django is Tom's default and earns its keep
here: the poller runs as a management command sharing the ORM with the dashboard,
and the Django admin gives free browsing/filtering of raw readings.

```
awairelement/  (this repo)
├── SCOPE.md
├── pyproject.toml            # uv-managed
├── manage.py
├── awairelement/             # Django project (settings, urls, wsgi)
└── readings/
    ├── models.py             # Reading, AlertEvent
    ├── management/commands/poll_awair.py   # the 30s poller loop
    ├── spikes.py             # baseline + detection logic (pure functions, unit-tested)
    ├── alerts.py             # ntfy client (pure-ish, unit-tested with mocked HTTP)
    ├── views.py              # dashboard + JSON series endpoints
    └── templates/readings/dashboard.html
```

Two long-running processes on homelab, both systemd **user** units
(`systemctl --user`, sandy pattern):

- `awair-poller.service` — `manage.py poll_awair`: fetch → store → detect → alert,
  loop with a 30s sleep. A persistent process rather than cron: consecutive-failure
  tracking stays trivial and we avoid 2,880 process spawns/day. All detection
  state is DB-backed (see Spike Detection), so restarts are safe by construction.
- `awair-web.service` — gunicorn serving the dashboard on a LAN port.

`Restart=always` on both units covers crashes. All timestamps stored in UTC.
SQLite runs in **WAL mode with a busy_timeout** (set via connection PRAGMAs in
settings) so the poller's writes and gunicorn's reads never produce
`database is locked` errors.

Every HTTP call has an explicit timeout: device fetch 5s, ntfy POST 10s. A hung
request must never stall the poll loop.

## Data Model

```sql
-- Reading: one row per poll, all fields the device gives us
CREATE TABLE readings_reading (
    id INTEGER PRIMARY KEY,
    ts TIMESTAMP NOT NULL,              -- device timestamp, UTC
    received_at TIMESTAMP NOT NULL,     -- server clock, UTC; exposes staleness & clock skew
    score INTEGER, temp REAL, humid REAL, abs_humid REAL, dew_point REAL,
    co2 INTEGER, co2_est INTEGER, co2_est_baseline INTEGER,
    voc INTEGER, voc_baseline INTEGER, voc_h2_raw INTEGER, voc_ethanol_raw INTEGER,
    pm25 REAL, pm10_est INTEGER
);
CREATE UNIQUE INDEX ON readings_reading (ts);   -- device ts; dedupes double-polls

-- AlertEvent: ONE ROW PER EVENT (not per notification) — restart-safe and
-- directly queryable for "currently open?" and dashboard overlays
CREATE TABLE readings_alertevent (
    id INTEGER PRIMARY KEY,
    metric TEXT NOT NULL,               -- co2 | voc | pm25 | device
    tier TEXT NOT NULL,                 -- relative | ceiling | unreachable | stale
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP,                -- NULL while open
    peak_value REAL, baseline REAL, threshold REAL,
    open_notified BOOLEAN NOT NULL DEFAULT 0,
    close_notified BOOLEAN NOT NULL DEFAULT 0,
    renotified_at TIMESTAMP             -- last "still elevated" reminder, if any
);
```

(Expressed as Django models; SQL shown for clarity.) The unique `ts` index is the
idempotency guard: the device only updates every ~10s, so a 30s poll can
occasionally see a repeated device timestamp — colliding inserts are skipped.
A dup-skip **pauses** (does not reset) the consecutive-poll counters below.

On startup the poller reloads open events (`closed_at IS NULL`) and resumes —
no duplicate "spike" notifications after a deploy or crash restart.

## Spike Detection

Runs in the poller after each successful insert. Baseline stats are computed by a
straight trailing-24h query per metric each poll (~2,880 rows; sub-millisecond —
no in-memory window to keep warm, restart-safe by construction).

Per metric (CO2, TVOC, PM2.5):

- **Baseline:** median of the trailing 24h. **Spread:** `max(MAD, floor)` — the
  floor is essential because MAD collapses to ~0 during stable periods (CO2
  overnight, PM2.5 for days at a time), which would otherwise turn 6×MAD into
  "alert on sensor noise." Starting floors: CO2 50 ppm, TVOC 50 ppb,
  PM2.5 4 µg/m³. K, M, and the floors are the first-class tunables.
- **Cold start:** tier-1 detection is disabled until ≥ 6h of readings exist;
  tier-2 ceilings are active from the first reading.
- **Tier 1 — relative spike:** value > baseline + `K` × spread for `M` consecutive
  polls (start K=6, M=4 ≈ two minutes sustained — tune with real data).
- **Tier 2 — absolute ceiling:** value over the ceiling for **2 consecutive polls**
  (~1 min; never a single sample — optical PM sensors blip). Ceilings:
  CO2 1200 ppm, TVOC 1000 ppb, PM2.5 35 µg/m³. The CO2 ceiling assumes the device
  is NOT in an occupied bedroom overnight — see Decisions.
- **Close condition (hysteresis):** an open event closes only when the value is
  **both** below baseline + (K/2) × spread **and** below the ceiling, sustained
  for 10 minutes. One "spike" notification at open, one "cleared" at close.
- **Re-arm for long events:** if an event is still open after 12h (e.g., VOC
  baseline drift parking readings high for days), send one "still elevated"
  reminder, update `renotified_at` and `peak_value`, and repeat at most every 12h.
  This prevents a stuck event from silently swallowing days of alerting.
- **Device health:** 10 consecutive fetch failures (~5 min) → "unreachable" event;
  10 consecutive polls with an **unchanged device timestamp** (expected refresh is
  ~10s) → "stale" event — this catches the wedged-but-HTTP-200 failure mode where
  dedup would otherwise silently skip every insert while looking healthy.
  First good, fresh reading closes either event ("recovered").

VOC caveat baked into the design: the TVOC sensor auto-recalibrates and drifts
with humidity, so `voc_baseline`, `voc_ethanol_raw`, `voc_h2_raw`, and `humid` are
all stored to let us discount drift and fingerprint alcohol sources when reviewing
events. Detection stays simple (relative-to-recent-median absorbs slow drift; the
12h re-arm handles fast drift episodes).

## Alerts

- ntfy POST with token from the environment (systemd `EnvironmentFile`, sourced
  from Ansible vault — never committed). Topic: see Decisions (dedicated `/awair`
  topic proposed so air-quality alerts don't share a phone channel/sound with
  Claude task notifications).
- Message includes metric, value, baseline, and a link to the dashboard.
- Priority: default for tier-1 spikes; high for tier-2 ceilings and
  unreachable/stale.
- Send failure: one retry, then record `*_notified = false`, log, and move on.
  Alerting must never block ingestion.

## Dashboard

Single page, no login (LAN-only), served by Django + one JSON endpoint per range.

- Range toggle: **7 days** / **30 days**. Charts render in **browser-local time**
  (storage stays UTC).
- One chart per metric (small multiples): CO2, TVOC, PM2.5, temp, humidity, score.
- Data is bucketed server-side (7d → 5-min averages ≈ 2k points/series; 30d →
  15-min averages) so the browser never chokes on raw 30s data. Buckets carry
  min/max so spikes aren't averaged away — render as a band or high/low ticks.
- Spike events overlaid as shaded spans straight from `AlertEvent`
  (`opened_at`/`closed_at`; open events extend to "now").
- Charting: a small embedded JS chart library (uPlot or Chart.js — pick at build
  time; uPlot favored for time-series density). No build step, no npm — vendored
  single file.

## Deployment

Native-daemon pattern (sandy/estimatedtaxes precedent), NOT an itguy docker app:

- Clone at `/home/tom/sources/awairelement` on homelab.
- `restart.sh` in-repo: `uv sync --frozen`, run migrations, restart both units.
- Systemd user units + env file templated by the `native-apps` Ansible role in the
  homelab repo (same as `sandy.service.j2`). Secrets (ntfy token,
  `DJANGO_SECRET_KEY`) via Ansible vault → env file.
- Update flow: `ssh tom@192.168.68.67 'cd ~/sources/awairelement && git pull && ./restart.sh'`.
- SQLite DB lives outside the repo checkout (e.g. `~/data/awairelement/awair.db`)
  so a re-clone never touches data.
- Backups: nightly `sqlite3 awair.db ".backup <dest>"` (or `VACUUM INTO`) into the
  existing homelab backup path — never a raw `cp` of a live WAL database.

## Testing

- `spikes.py` is pure functions over sequences of readings — unit-test the
  baseline math (including the MAD floor), hysteresis both-conditions close,
  cold start, re-arm, and counter-pause-on-dup with synthetic spike shapes
  (step, ramp, single-sample blip that must NOT alert, flatline that must NOT
  alert at MAD≈0).
- Poller parsing tested against a captured real JSON response (fixture recorded
  from 192.168.68.51 at build time).
- ntfy client tested with mocked HTTP (mock at the library boundary).
- Dashboard endpoints: Django test client against seeded readings.

## Project Plan

| Slice | Deliverable | Proves |
|-------|-------------|--------|
| 1 | Poller + models (incl. AlertEvent schema) + systemd unit, running on homelab, rows accumulating | Ingestion works unattended |
| 2 | Spike detection + ntfy alerts (unit-tested first) | Alerting works; tune K/M/floors with a real cooking event |
| 3 | Dashboard (7d/30d charts + event overlays) + web unit | Trends visible |
| 4 | Ansible role wiring + backups + docs | Reproducible deploy |

Slice 1 ships value immediately (data starts accumulating while we build 2–3, and
2–3 benefit from having real data to tune against). The full `AlertEvent` schema
lands in slice 1 so no migration-with-data is needed for slice 2.

## DECISION NEEDED

1. **Django vs. lighter (FastAPI/Flask):** Scope assumes Django per Tom's default;
   recommendation stands (management-command poller + free admin). Confirm.
2. **LAN-only vs. Cloudflare Tunnel + Authelia:** Scope assumes LAN-only for v1.
   Exposing later via the tunnel is a small, known lift. Confirm.
3. **Which room is the Element in?** The CO2 ceiling (1200 ppm) is calibrated for
   living space. An occupied bedroom overnight routinely hits 1200–2000 ppm and
   would alert every night — if it's a bedroom, the ceiling moves to ~1800 ppm.
4. **ntfy topic:** dedicated `/awair` topic (proposed — separate phone sound from
   Claude task notifications) vs. reusing `/claude`. Depends on whether the
   existing token is scoped to the `/claude` topic.
