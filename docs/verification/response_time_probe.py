#!/usr/bin/env python3
"""Size every dashboard request against a real database (#141).

Answers the question #141 opens with -- *"sometimes it's the entire page load,
sometimes it's just the json"* -- by timing each request the page makes, per
range, and printing them side by side.

Run it on the homelab, against the live file, where the row counts are real::

    cd ~/sources/awairelement
    uv run --frozen python docs/verification/response_time_probe.py

Or against a snapshot anywhere::

    uv run --frozen python docs/verification/response_time_probe.py --db /tmp/awair-snap.db

**What this probe can and cannot see.** It drives the app through Flask's test
client, so each number is the time the *application* spends on one request,
start to finish, with nothing else in flight. That is very nearly the quantity
`awair.timing` logs in production -- near enough to compare, and not identical:
the probe's clock spans the whole test-client call while the log line's stops
at `after_request`. Measured, they agree to 0.1 ms on `/api/series` and
disagree by 0.1 ms on `/`, where the request costs almost nothing.

`create_app` bootstraps the schema, so pointing this at the live file opens it
**read-write** for one idempotent DDL pass -- the same thing the web unit does
on every boot.

It therefore cannot see the two costs that live outside the app, and neither
can the production log line:

- **Queue time.** `awairelement-web.service` runs gunicorn with ``-w 2`` *sync*
  workers, so two requests are served at a time. `dashboard.js` fires three
  `/api/*` fetches at once (`load()`), after the browser has already asked for
  the document and three static assets -- so a request can sit waiting for a
  free worker, and its in-app timer does not start until it gets one. The
  browser's TTFB minus the `Server-Timing: app` header is that wait.
- **Transfer.** Response bytes are reported for exactly this reason: over
  wifi, the 500 KB `/api/series?range=30d` body -- plus the client-side parse
  and the uPlot draw of it -- is a wait no server-side timer attributes to
  itself.
"""

import argparse
import logging
import os
import statistics
import sys
from pathlib import Path
from time import perf_counter

# Import `awair` from the checkout this file sits in, so a probe run by path
# measures its own tree rather than whatever happens to be importable.
_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from awair import timing, web  # noqa: E402

# Every request one page load makes, in the order the browser makes them.
# `dashboard.js` fires the three `/api/*` entries below concurrently; the rest
# are serial ahead of them.
DOCUMENT = ("document", "/")
STATIC = (
    ("static css", "/static/uplot.min.css"),
    ("static uplot", "/static/uplot.iife.min.js"),
    ("static dashboard", "/static/dashboard.js"),
)
# Ranged endpoints -- `?range=` is appended per range.
RANGED = (
    ("api/series", "/api/series"),
    ("api/events", "/api/events"),
    ("api/outdoor-series", "/api/outdoor-series"),
)
# Endpoints the page polls without a range.
UNRANGED = (
    ("api/latest", "/api/latest"),
    ("api/outdoor-latest", "/api/outdoor-latest"),
    ("api/outdoor-today", "/api/outdoor-today"),
    ("api/weather-alerts", "/api/weather-alerts"),
)


def assert_covers_every_route(app):
    """Refuse to report on a subset of the app's routes.

    The three tuples above are written out by hand, because which of them take
    a `?range=` and which the browser fires concurrently is not introspectable
    from the url map. A hand-written list is one route behind the day someone
    adds a route, and a probe reporting on four endpoints out of five reads
    exactly like a probe reporting on all five -- so this asserts set equality
    against `app.url_map` rather than a count or a floor. `static` is the one
    rule with a path parameter; the three files under it are timed explicitly.
    """
    routed = {
        str(rule)
        for rule in app.url_map.iter_rules()
        if "GET" in rule.methods and rule.endpoint != "static"
    }
    probed = {path for _, path in (DOCUMENT, *RANGED, *UNRANGED)}
    missing = routed - probed
    extra = probed - routed
    if missing or extra:
        raise SystemExit(
            f"probe does not cover the app's routes: missing={sorted(missing)} "
            f"unknown={sorted(extra)} -- update DOCUMENT/RANGED/UNRANGED"
        )


def time_get(client, path, repeat):
    """Return (p50_ms, max_ms, bytes, status) over `repeat` GETs of `path`."""
    samples = []
    length = status = None
    for _ in range(repeat):
        start = perf_counter()
        response = client.get(path)
        samples.append((perf_counter() - start) * 1000)
        length = len(response.get_data())
        status = response.status_code
    return statistics.median(samples), max(samples), length, status


def rows_in(db_path):
    """Row counts per table, so a duration can be read against its input."""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        counts = {}
        for table in ("readings", "outdoor_readings", "alert_events"):
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        return counts
    finally:
        conn.close()


def probe(db_path, repeat):
    app = web.create_app(db_path)
    # Quiet the per-request log this repo now installs (#141). 17 requests at
    # `--repeat 5` is ~85 INFO lines on stderr against a table on stdout, and
    # in a terminal they interleave and scroll the header away. The slow tier
    # is left at WARNING so a genuinely slow request still announces itself
    # while the probe runs.
    logging.getLogger(timing.LOGGER_NAME).setLevel(logging.WARNING)
    assert_covers_every_route(app)
    client = app.test_client()
    print(f"db      : {db_path} ({os.path.getsize(db_path) / 1e6:.1f} MB)")
    print(f"rows    : {rows_in(db_path)}")
    print(f"repeat  : {repeat} per request\n")
    header = (
        f"{'request':<22} {'range':<6} {'p50 ms':>8} {'max ms':>8} {'KB':>9} {'st':>4}"
    )
    print(header)
    print("-" * len(header))
    for label, path in (DOCUMENT, *STATIC):
        p50, mx, size, status = time_get(client, path, repeat)
        print(
            f"{label:<22} {'-':<6} {p50:>8.1f} {mx:>8.1f} {size / 1024:>9.1f} {status:>4}"
        )
    for name in web.RANGES:
        for label, path in RANGED:
            p50, mx, size, status = time_get(client, f"{path}?range={name}", repeat)
            print(
                f"{label:<22} {name:<6} {p50:>8.1f} {mx:>8.1f} {size / 1024:>9.1f} {status:>4}"
            )
    for label, path in UNRANGED:
        p50, mx, size, status = time_get(client, path, repeat)
        print(
            f"{label:<22} {'-':<6} {p50:>8.1f} {mx:>8.1f} {size / 1024:>9.1f} {status:>4}"
        )


def probe_render(db_path, repeat):
    """Size the Jinja render of `/` on its own -- the #141 caching proposal.

    `/` touches no database (see `web.dashboard`), so the only cost caching
    could remove is this render. Measured separately from the request above so
    the saving is a number rather than an argument.
    """
    from flask import render_template

    app = web.create_app(db_path)
    samples = []
    with app.test_request_context("/"):
        kwargs = {
            "metrics": web.METRIC_NAMES,
            "ceilings": web.CEILINGS,
            "temp_unit_symbol": "F",
            "range_labels": web.RANGE_LABELS,
            "default_range": web.DEFAULT_RANGE,
            "overlays": web.METRIC_OVERLAYS,
        }
        render_template("dashboard.html", **kwargs)  # warm Jinja's own cache
        for _ in range(repeat):
            start = perf_counter()
            render_template("dashboard.html", **kwargs)
            samples.append((perf_counter() - start) * 1000)
    print(
        f"\njinja render of dashboard.html alone: "
        f"p50 {statistics.median(samples):.2f} ms, max {max(samples):.2f} ms "
        f"over {repeat} renders (template already compiled -- Jinja caches that)"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
        ),
    )
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        parser.error(f"no such database: {args.db}")
    probe(args.db, args.repeat)
    probe_render(args.db, args.repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
