# GET /health: does every feed still record? (#158)

*2026-11-20T22:31:35Z by Showboat 0.6.1*
<!-- showboat-id: c450811a-7412-4277-b799-7a10bd989ff2 -->

The deploy check for this app was `GET /`. That proves the web process is up and the database opens, and nothing at all about the four pollers that fill it: every feed could have stopped recording a week ago and the check would still pass.

`GET /health` is the missing half. Everything below is run against this branch; the only prerequisite is `uv sync --frozen`.

## The feeds, and the window each is judged against

One missed-poll count (`AWAIR_HEALTH_STALE_POLLS`, default 6) times **each feed's own** cadence. The cadence is read from the poller that sets it, not restated here.

```bash
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
from awair import feeds

print(f"{'feed':<18} {'table':<20} {'head':<8} {'cadence':>8} {'stale after':>12}")
for feed in feeds.FEEDS:
    print(
        f"{feed.name:<18} {feed.table:<20} {feed.head or '-':<8}"
        f" {feed.poll_seconds():>7}s {feed.stale_after_seconds():>11}s"
    )
PY

```

```output
feed               table                head      cadence  stale after
awair              readings             -             30s         180s
basement           basement_readings    -             60s         360s
outdoor            outdoor_readings     -            900s        5400s
minisplit-dining   minisplit_readings   dining       300s        1800s
minisplit-bedroom  minisplit_readings   bedroom      300s        1800s
```

## A database to ask about

One throwaway helper, used by every block below. `build()` writes one row per feed at a chosen moment; `payload()` calls the same `feeds.report` the route calls, with the clock supplied rather than read, so the JSON printed here is the real thing and byte-for-byte reproducible; `serve()` goes through the actual Flask route, for the status code.

```bash
mkdir -p /tmp/awair-158-demo && cat > /tmp/awair-158-demo/demo.py <<'PY'
"""Scratch helper for the #158 walkthrough. Not shipped; see tests/test_web_health.py."""
import json
import logging
from datetime import UTC, datetime, timedelta

from awair import db, feeds, timing
from awair.web import create_app

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
ROW = {
    "readings": "INSERT INTO readings (ts, received_at, temp) VALUES (?, ?, 21.5)",
    "basement_readings": "INSERT INTO basement_readings (ts, received_at, temp)"
    " VALUES (?, ?, 18.0)",
    "outdoor_readings": "INSERT INTO outdoor_readings (ts, received_at, temp)"
    " VALUES (?, ?, 5.0)",
}


def build(path, ages):
    """A bootstrapped database with one row per feed in `ages`, aged from NOW."""
    conn = db.connect(path)
    for feed, age in ages.items():
        stamp = (NOW - age).isoformat()
        if feed.head is None:
            conn.execute(ROW[feed.table], (stamp, stamp))
        else:
            conn.execute(
                "INSERT INTO minisplit_readings (head, ts, received_at, temp)"
                " VALUES (?, ?, ?, 20.0)",
                (feed.head, stamp, stamp),
            )
    conn.commit()
    return conn


def payload(path, ages):
    """The report as `/health` builds it, at a fixed instant."""
    conn = build(path, ages)
    try:
        print(json.dumps(feeds.report(conn, NOW), indent=2))
    finally:
        conn.close()


def serve(path, ages):
    """The real route, against the real clock. Rows are re-aged to match it."""
    offset = datetime.now(UTC) - NOW
    build(path, {feed: age - offset for feed, age in ages.items()}).close()
    app = create_app(path)
    # The per-request log line (#141) carries a wall clock and a duration, so it
    # is the one thing in this walkthrough `showboat verify` could not re-run.
    logging.getLogger(timing.LOGGER_NAME).setLevel(logging.WARNING)
    response = app.test_client().get("/health")
    body = response.get_json()
    print(f"HTTP {response.status_code}  status={body['status']!r}")
    print(f"stale: {body['stale']}")
    return body


FRESH = {
    "awair": timedelta(seconds=12),
    "basement": timedelta(seconds=30),
    "outdoor": timedelta(seconds=780),
    "minisplit-dining": timedelta(seconds=300),
    "minisplit-bedroom": timedelta(seconds=300),
}


def ages(**overrides):
    """FRESH, with named feeds re-aged or dropped entirely (`None` = no rows)."""
    plan = FRESH | overrides
    return {
        feed: plan[feed.name]
        for feed in feeds.FEEDS
        if plan.get(feed.name) is not None
    }
PY
echo "wrote /tmp/awair-158-demo/demo.py"

```

```output
wrote /tmp/awair-158-demo/demo.py
```

## Every feed fresh: 200, and the whole body

The window and the cadence it came from ride beside each verdict, so a failure can be read without opening this repo.

```bash
rm -f /tmp/awair-158-demo/fresh.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from demo import ages, payload
payload("/tmp/awair-158-demo/fresh.db", ages())
PY

```

```output
{
  "status": "ok",
  "checked_at": "2026-11-20T18:00:00.000Z",
  "stale": [],
  "feeds": [
    {
      "name": "awair",
      "label": "Awair Element",
      "newest_reading_at": "2026-11-20T17:59:48.000Z",
      "age_seconds": 12.0,
      "poll_seconds": 30,
      "stale_after_seconds": 180,
      "fresh": true
    },
    {
      "name": "basement",
      "label": "Basement sensor",
      "newest_reading_at": "2026-11-20T17:59:30.000Z",
      "age_seconds": 30.0,
      "poll_seconds": 60,
      "stale_after_seconds": 360,
      "fresh": true
    },
    {
      "name": "outdoor",
      "label": "Outdoor weather",
      "newest_reading_at": "2026-11-20T17:47:00.000Z",
      "age_seconds": 780.0,
      "poll_seconds": 900,
      "stale_after_seconds": 5400,
      "fresh": true
    },
    {
      "name": "minisplit-dining",
      "label": "Mini split (dining)",
      "newest_reading_at": "2026-11-20T17:55:00.000Z",
      "age_seconds": 300.0,
      "poll_seconds": 300,
      "stale_after_seconds": 1800,
      "fresh": true
    },
    {
      "name": "minisplit-bedroom",
      "label": "Mini split (bedroom)",
      "newest_reading_at": "2026-11-20T17:55:00.000Z",
      "age_seconds": 300.0,
      "poll_seconds": 300,
      "stale_after_seconds": 1800,
      "fresh": true
    }
  ]
}
```

Through the real Flask route, against the real clock:

```bash
rm -f /tmp/awair-158-demo/http-fresh.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from demo import ages, serve
serve("/tmp/awair-158-demo/http-fresh.db", ages())
PY

```

```output
HTTP 200  status='ok'
stale: []
```

## One stale feed: 503, and it is named

The basement sensor stops publishing. Its own window is 6 minutes (6 polls x 60 s), so a reading 10 minutes old is past it — while the outdoor feed, 13 minutes old against a 90-minute window, is perfectly fine. That is the point of judging per feed: one wall-clock threshold would have to call both stale or neither.

```bash
rm -f /tmp/awair-158-demo/stale.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from datetime import timedelta
from demo import ages, payload
payload("/tmp/awair-158-demo/stale.db", ages(basement=timedelta(minutes=10)))
PY

```

```output
{
  "status": "stale",
  "checked_at": "2026-11-20T18:00:00.000Z",
  "stale": [
    "basement"
  ],
  "feeds": [
    {
      "name": "awair",
      "label": "Awair Element",
      "newest_reading_at": "2026-11-20T17:59:48.000Z",
      "age_seconds": 12.0,
      "poll_seconds": 30,
      "stale_after_seconds": 180,
      "fresh": true
    },
    {
      "name": "basement",
      "label": "Basement sensor",
      "newest_reading_at": "2026-11-20T17:50:00.000Z",
      "age_seconds": 600.0,
      "poll_seconds": 60,
      "stale_after_seconds": 360,
      "fresh": false
    },
    {
      "name": "outdoor",
      "label": "Outdoor weather",
      "newest_reading_at": "2026-11-20T17:47:00.000Z",
      "age_seconds": 780.0,
      "poll_seconds": 900,
      "stale_after_seconds": 5400,
      "fresh": true
    },
    {
      "name": "minisplit-dining",
      "label": "Mini split (dining)",
      "newest_reading_at": "2026-11-20T17:55:00.000Z",
      "age_seconds": 300.0,
      "poll_seconds": 300,
      "stale_after_seconds": 1800,
      "fresh": true
    },
    {
      "name": "minisplit-bedroom",
      "label": "Mini split (bedroom)",
      "newest_reading_at": "2026-11-20T17:55:00.000Z",
      "age_seconds": 300.0,
      "poll_seconds": 300,
      "stale_after_seconds": 1800,
      "fresh": true
    }
  ]
}
```

And the code the deploy sees:

```bash
rm -f /tmp/awair-158-demo/http-stale.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from datetime import timedelta
from demo import ages, serve
serve("/tmp/awair-158-demo/http-stale.db", ages(basement=timedelta(minutes=10)))
PY

```

```output
HTTP 503  status='stale'
stale: ['basement']
```

## A feed with no rows at all: 503, and it is named

The case `GET /` was least able to notice, and the one that matters most on a fresh box. Here the bedroom head has never written a row — the bridge was never reachable, or the poller has never run — and `newest_reading_at` is `null` rather than old.

```bash
rm -f /tmp/awair-158-demo/missing.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys, json; sys.path.insert(0, "/tmp/awair-158-demo")
from demo import ages, serve
body = serve("/tmp/awair-158-demo/missing.db", ages(**{"minisplit-bedroom": None}))
entry = next(f for f in body["feeds"] if f["name"] == "minisplit-bedroom")
print(json.dumps(entry, indent=2))
PY

```

```output
HTTP 503  status='stale'
stale: ['minisplit-bedroom']
{
  "age_seconds": null,
  "fresh": false,
  "label": "Mini split (bedroom)",
  "name": "minisplit-bedroom",
  "newest_reading_at": null,
  "poll_seconds": 300,
  "stale_after_seconds": 1800
}
```

## Nothing has ever polled: every feed named, not just the first

A brand new box, web up before any poller.

```bash
rm -f /tmp/awair-158-demo/empty.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from demo import serve
serve("/tmp/awair-158-demo/empty.db", {})
PY

```

```output
HTTP 503  status='stale'
stale: ['awair', 'basement', 'outdoor', 'minisplit-dining', 'minisplit-bedroom']
```

## The windows are derived, not restated

Each feed's window is its own poller's cadence times the missed-poll count. Move the outdoor poller's interval and only the outdoor window follows — which is the only way to tell a derivation from a coincidence while the number happens to be 900.

```bash
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import os

from awair import feeds

CASES = (
    ("defaults", {}),
    ("outdoor poller moved to 30 min", {"AWAIR_OUTDOOR_POLL_SECONDS": "1800"}),
    ("missed-poll count lowered to 2", {"AWAIR_HEALTH_STALE_POLLS": "2"}),
)
for label, env in CASES:
    for name in ("AWAIR_OUTDOOR_POLL_SECONDS", "AWAIR_HEALTH_STALE_POLLS"):
        os.environ.pop(name, None)
    os.environ.update(env)
    windows = " ".join(
        f"{f.name}={f.stale_after_seconds()}s" for f in feeds.FEEDS
    )
    print(f"{label:<32} {windows}")
PY

```

```output
defaults                         awair=180s basement=360s outdoor=5400s minisplit-dining=1800s minisplit-bedroom=1800s
outdoor poller moved to 30 min   awair=180s basement=360s outdoor=10800s minisplit-dining=1800s minisplit-bedroom=1800s
missed-poll count lowered to 2   awair=60s basement=120s outdoor=1800s minisplit-dining=600s minisplit-bedroom=600s
```

## It cannot reach the network, so it cannot fail because the network is down

Every poller reaches its source through `urllib.request.urlopen`. Poison it and ask again.

```bash
rm -f /tmp/awair-158-demo/nonet.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys, urllib.request; sys.path.insert(0, "/tmp/awair-158-demo")


def refuse(*args, **kwargs):
    raise AssertionError("/health reached the network")


urllib.request.urlopen = refuse

from demo import ages, serve
serve("/tmp/awair-158-demo/nonet.db", ages())
PY

```

```output
HTTP 200  status='ok'
stale: []
```

## Over real HTTP, unauthenticated, from the unit's own command line

gunicorn exactly as `awairelement-web.service` runs it, and plain `curl` with no credentials. gunicorn's stderr is discarded here only because the per-request log line (#141) carries a wall clock and a duration, which `showboat verify` could not re-run.

```bash
set -e
rm -f /tmp/awair-158-demo/served.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from datetime import UTC, datetime

import demo
from demo import ages, build

offset = datetime.now(UTC) - demo.NOW
build("/tmp/awair-158-demo/served.db", {f: a - offset for f, a in ages().items()}).close()
PY
AWAIR_DB=/tmp/awair-158-demo/served.db env -u VIRTUAL_ENV uv run --frozen \
  gunicorn -w 2 -b 127.0.0.1:8197 'awair.web:create_app()' \
  --pid /tmp/awair-158-demo/gunicorn.pid --log-level error 2>/dev/null &
for _ in $(seq 40); do curl -sf -o /dev/null http://127.0.0.1:8197/health && break; sleep 0.25; done
curl -s -o /dev/null -w 'status: %{http_code}\n' http://127.0.0.1:8197/health
curl -s http://127.0.0.1:8197/health | jq -c '{status, stale, feeds: [.feeds[] | {name, fresh}]}'
kill "$(cat /tmp/awair-158-demo/gunicorn.pid)"

```

```output
status: 200
{"status":"ok","stale":[],"feeds":[{"name":"awair","fresh":true},{"name":"basement","fresh":true},{"name":"outdoor","fresh":true},{"name":"minisplit-dining","fresh":true},{"name":"minisplit-bedroom","fresh":true}]}
```

The same server, against a database where the basement sensor stopped 10 minutes ago. This is what a deploy check sees when it fails:

```bash
set -e
rm -f /tmp/awair-158-demo/served-stale.db
env -u VIRTUAL_ENV uv run --frozen python - <<'PY'
import sys; sys.path.insert(0, "/tmp/awair-158-demo")
from datetime import UTC, datetime, timedelta

import demo
from demo import ages, build

plan = ages(basement=timedelta(minutes=10))
offset = datetime.now(UTC) - demo.NOW
build("/tmp/awair-158-demo/served-stale.db", {f: a - offset for f, a in plan.items()}).close()
PY
AWAIR_DB=/tmp/awair-158-demo/served-stale.db env -u VIRTUAL_ENV uv run --frozen \
  gunicorn -w 2 -b 127.0.0.1:8198 'awair.web:create_app()' \
  --pid /tmp/awair-158-demo/gunicorn-stale.pid --log-level error 2>/dev/null &
for _ in $(seq 40); do curl -s -o /dev/null http://127.0.0.1:8198/health && break; sleep 0.25; done
curl -s -o /dev/null -w 'status: %{http_code}\n' http://127.0.0.1:8198/health
curl -s http://127.0.0.1:8198/health | jq -c '{status, stale}'
# Age rounded to 10 s: the live clock moves while gunicorn boots, and this
# walkthrough is re-run by `showboat verify`.
curl -s http://127.0.0.1:8198/health |
  jq -r '.feeds[] | select(.fresh == false) |
         "  \(.label): ~\(((.age_seconds / 10) | round) * 10)s old, stale after \(.stale_after_seconds)s"'
kill "$(cat /tmp/awair-158-demo/gunicorn-stale.pid)"

```

```output
status: 503
{"status":"stale","stale":["basement"]}
  Basement sensor: ~600s old, stale after 360s
```

## The tests

Thirty-four of them. Both 503 cases are parametrized over every feed rather than asserted on one, because a route that checked three feeds out of five would pass a single-feed test and miss two pollers forever.

```bash
# The duration pytest prints is stripped: `showboat verify` re-runs this.
env -u VIRTUAL_ENV uv run --frozen pytest tests/test_web_health.py --no-cov -q 2>&1 |
  sed -E 's/ in [0-9.]+s$//'

```

```output
..................................                                       [100%]
34 passed
```

And the mutation round that says those tests bite. Each of these was applied to the source, the suite run, and the source restored:

| mutation | tests that went red |
|---|---|
| `stale_after_seconds` returns a hardcoded `180` | 7, incl. every `test_each_window_follows_its_own_pollers_cadence` |
| `is_fresh` drops its lower bound (`age <= limit`) | `test_a_reading_from_the_future_is_stale_not_fresh` |
| the route always returns 200 | 13 |
| `newest_received_at` uses `ORDER BY ts DESC LIMIT 1` | 9, incl. `..._prefers_arrival_to_the_source_clock` |
| the `WHERE head = ?` filter is dropped | 5, incl. both `minisplit-*` no-rows cases |
| an empty table reads as fresh | 7 |
| the bedroom head is dropped from `FEEDS` | `test_the_head_list_comes_from_the_poller` |

## What it costs

`/health` is now timed by `docs/verification/response_time_probe.py`, which refuses to report on a subset of the app's routes. It is the one route whose cost grows with the table — `MAX(received_at)` on four tables, and `received_at` carries no index on any of them.

```bash
set -e
SCALE=/tmp/awair-158-demo/scale.db
if [ ! -f "$SCALE" ]; then
  mkdir -p /tmp/awair-158-demo
  env -u VIRTUAL_ENV uv run --frozen python - "$SCALE" <<'PY'
"""A synthetic database at homelab scale: 580,000 rows, ~78 MB."""
import sys
from datetime import UTC, datetime, timedelta

from awair import db

now = datetime.now(UTC)
conn = db.connect(sys.argv[1])


def stamps(count, step):
    return [now - timedelta(seconds=i * step) for i in range(count, 0, -1)]


conn.executemany(
    "INSERT INTO readings (ts, received_at, score, temp, humid, co2, voc, pm25)"
    " VALUES (?, ?, 90, 21.5, 44.0, 600, 120, 3.0)",
    [(db.iso_z(t), t.isoformat()) for t in stamps(320_000, 30)],
)
conn.executemany(
    "INSERT INTO basement_readings (ts, received_at, temp, humid)"
    " VALUES (?, ?, 18.0, 55.0)",
    [(t.isoformat(), t.isoformat()) for t in stamps(150_000, 60)],
)
conn.executemany(
    "INSERT INTO outdoor_readings (ts, received_at, temp, humid, pressure, pm25)"
    " VALUES (?, ?, 5.0, 70.0, 1013.0, 8.0)",
    [(t.isoformat(), t.isoformat()) for t in stamps(30_000, 900)],
)
conn.executemany(
    "INSERT INTO minisplit_readings (head, ts, received_at, temp, watts_spot)"
    " VALUES (?, ?, ?, 20.0, 120.0)",
    [
        (head, t.isoformat(), t.isoformat())
        for head in ("dining", "bedroom")
        for t in stamps(40_000, 300)
    ],
)
conn.commit()
conn.close()
PY
fi
# `/health` only, rounded to 5 ms: `showboat verify` re-runs this, and the
# dashboard's own `/api/series?range=30d` on the same file is noisier than the
# nearest 5 ms. Its figure is in the prose below and in README.
env -u VIRTUAL_ENV uv run --frozen python docs/verification/response_time_probe.py \
  --db "$SCALE" --repeat 5 2>/dev/null |
  awk '/^db      |^rows    /{print} /^health /{printf "%-10s p50 %3.0f ms (to the nearest 5)\n", $1, int(($3+2.5)/5)*5}'

```

```output
db      : /tmp/awair-158-demo/scale.db (77.7 MB)
rows    : {'readings': 320000, 'outdoor_readings': 30000, 'minisplit_readings': 80000, 'basement_readings': 150000, 'alert_events': 0}
health     p50  30 ms (to the nearest 5)
```

About 30 ms for all four tables, against 580,000 rows in a 78 MB file — roughly an eighth of what the dashboard's own `/api/series?range=30d` costs on the same database (~230 ms, from the same probe run), and a cost the deploy pays rather than something a page load waits on. `db.newest_received_at` records why there is no index on `received_at`, and why `ORDER BY rowid DESC LIMIT 1` is the tempting wrong answer.

## What this does not do

- **It does not alert.** 503 is a code a caller reads; nothing here sends an ntfy message. The pollers' own trackers (`monitor.DeviceHealth`, `monitor.OutdoorHealth`) already do that, and alerting was out of scope on #158.
- **It does not change what the pollers do.** Each of the four gained a `poll_seconds()` reader over the `DEFAULT_POLL_SECONDS` constant three of them already had, so each cadence has exactly one definition and `/health` reads it from there. Nothing about what they fetch, store or alert on moved.
- **It does not change the health check URL in the homelab repo.** That is a separate change, after this deploys.
