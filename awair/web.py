"""Dashboard Flask app: one page, JSON series/events endpoints.

Run via: uv run --frozen gunicorn -b 0.0.0.0:8097 'awair.web:create_app()'
"""

import os
from datetime import UTC, datetime, timedelta

from flask import Flask, abort, jsonify, render_template, request

from awair import db, solar, spikes, units
from awair.series import bucket

METRIC_NAMES = ("co2", "voc", "pm25", "temp", "humid", "score")

# Alert ceilings surfaced to the dashboard as horizontal reference lines so a
# Y-axis autoscaled to a peak doesn't visually collapse "still elevated" into
# "cleared" (#25). Metrics without an entry in spikes.METRICS get no line.
CEILINGS = {name: cfg.ceiling for name, cfg in spikes.METRICS.items()}

# Metric fields on an alert_event whose value carries the same unit as the
# metric itself — converted for temp events at the API boundary.
_TEMP_EVENT_FIELDS = ("peak_value", "baseline", "threshold")

# The reading columns `/api/latest` publishes. Deliberately a short, explicit
# list rather than READING_COLUMNS: the derived and raw-sensor columns
# (`abs_humid`, `dew_point`, `co2_est*`, `voc_*_raw`, `pm10_est`) are internal,
# and a consumer that starts depending on them makes them a contract.
LATEST_METRICS = ("score", "temp", "humid", "co2", "voc", "pm25")

# The outdoor columns `/api/outdoor-latest` publishes, in the order #71 lists
# them. A whitelist for the same reason `LATEST_METRICS` is one -- but note
# that here it happens to be every non-`received_at` column in the table, so
# the list is doing less filtering than its indoor sibling and more
# *ordering-and-contract* work. A column added to `outdoor_readings` later is
# private until someone adds it here on purpose.
OUTDOOR_LATEST_FIELDS = (
    "weather_code",
    "temp",
    "humid",
    "wind_speed",
    "pressure",
    "precipitation",
    "pm25",
    "pm10",
    "us_aqi",
    "co",
    "o3",
)

# `metric` values on an alert_event that are not measurements, and so are not
# part of the `/api/latest` contract. `poller.handle_device_health` opens one
# with `metric="device"` and `tier` in ("unreachable", "stale") whose
# peak_value/baseline/threshold are all None — a transport fact wearing a
# measurement's shape. Excluded deliberately (#70): the hub already learns the
# same thing, earlier and more reliably, from `received_at` going stale, since
# an unreachable device writes no readings at all. See README.
_NON_MEASUREMENT_METRICS = frozenset({"device"})

# The open-event fields `/api/latest` publishes, in the order #70 lists them.
# Also a whitelist rather than a passthrough — `db.get_open_events` carries
# `id`, `fans_engaged`, `notified_value` and `renotified_at`, which are this
# app's own notification bookkeeping and mean nothing to a consumer.
_OPEN_EVENT_FIELDS = (
    "metric",
    "tier",
    "opened_at",
    "peak_value",
    "baseline",
    "threshold",
)


def _iso_utc(value):
    """Normalise a stored timestamp to one ISO-8601 UTC spelling, or None.

    Accepts either spelling this app stores *and* an already-parsed datetime,
    because the three fields `/api/latest` publishes arrive in three shapes:
    `ts` is the device's own string via `db.iso_z` (`...Z`), `received_at` is
    `datetime.now(UTC).isoformat()` (`...+00:00`), and `opened_at` has already
    been through `datetime.fromisoformat` inside `db.get_open_events`.
    Publishing them as stored would hand a consumer three formats for fields
    whose whole purpose is to be compared against each other.

    A value with no offset is read as UTC rather than rejected: every writer in
    this app is UTC, and refusing here would take the endpoint down over a row
    it can still describe correctly. Note the failure this prevents is silent
    rather than loud — a naive value handed straight to `astimezone` is read as
    *local* time, so a poll that landed at noon UTC would publish as 16:00Z and
    a consumer's staleness clock would run four hours fast.
    """
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _public_event(event):
    """One open event as `/api/latest` publishes it.

    A whitelist rather than a passthrough, and named rather than inlined so the
    contract with the hub has one place to be read and one place to be tested.
    `db.get_open_events` carries `id`, `fans_engaged`, `notified_value` and
    `renotified_at` as well — this app's own notification bookkeeping, which
    means nothing to a consumer and which publishing would make a contract.
    """
    return {field: event[field] for field in _OPEN_EVENT_FIELDS} | {
        "opened_at": _iso_utc(event["opened_at"])
    }


# "today" == since local midnight, not the last 24h — it's the single-day
# detail view (#46). Bucket is 60 s (indoor poll cadence is 30 s → 2 samples
# per bucket) so the finer granularity actually shows up.
RANGES = {
    "today": {"days": "today", "bucket_seconds": 60},
    "7d": {"days": 7, "bucket_seconds": 300},
    "30d": {"days": 30, "bucket_seconds": 900},
}

# Outdoor readings publish every 15 min at the source, so bucket sizes are
# scaled up — indoor's 5-min bucket over 7d would leave most outdoor buckets
# empty and paint a jittery gap-riddled line. For "today", bucket == source
# cadence (900 s); no point sub-bucketing below what the source produces.
OUTDOOR_RANGES = {
    "today": {"days": "today", "bucket_seconds": 900},
    "7d": {"days": 7, "bucket_seconds": 900},
    "30d": {"days": 30, "bucket_seconds": 3600},
}

# Open-Meteo returns precipitation in mm. The dashboard displays inches — Tom's
# expected scale on #31 was "tenths of an inch". Conversion happens at the API
# boundary so storage stays raw (same shape as temperature: DB in Celsius,
# display convert via TEMPERATURE_UNIT).
_MM_PER_INCH = 25.4
# Open-Meteo returns MSL pressure in hPa. The dashboard displays inHg — US
# weather convention, matches the imperial units used for temperature and
# precipitation. Conversion at the API boundary (same pattern as precip).
_HPA_PER_INHG = 33.8639


def _since_for(spec):
    """Resolve a RANGES/OUTDOOR_RANGES spec to a UTC `since` datetime.

    `days: "today"` == local midnight (system TZ), everything else == N days
    back from now. Local midnight is the intuitive "today" — the app is a
    home dashboard on Tom's homelab, and Tom reads it in local time.
    """
    if spec["days"] == "today":
        local_midnight = (
            datetime.now()
            .astimezone()
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        return local_midnight.astimezone(UTC)
    return datetime.now(UTC) - timedelta(days=spec["days"])


def _range_params():
    name = request.args.get("range", "7d")
    if name not in RANGES:
        abort(400, f"range must be one of {sorted(RANGES)}")
    spec = RANGES[name]
    return _since_for(spec), spec["bucket_seconds"]


def _outdoor_range_params():
    name = request.args.get("range", "7d")
    if name not in OUTDOOR_RANGES:
        abort(400, f"range must be one of {sorted(OUTDOOR_RANGES)}")
    spec = OUTDOOR_RANGES[name]
    return _since_for(spec), spec["bucket_seconds"]


def create_app(db_path=None):
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.config["AWAIR_DB"] = db_path or os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    app.config["TEMPERATURE_UNIT"] = units.get_temperature_unit()

    def connect():
        return db.connect(app.config["AWAIR_DB"])

    def temp_unit():
        return app.config["TEMPERATURE_UNIT"]

    @app.get("/")
    def dashboard():
        return render_template(
            "dashboard.html",
            metrics=METRIC_NAMES,
            ceilings=CEILINGS,
            temp_unit_symbol=units.symbol(temp_unit()),
        )

    @app.get("/api/series")
    def series():
        since, bucket_seconds = _range_params()
        conn = connect()
        try:
            rows = db.readings_since(conn, METRIC_NAMES, since)
        finally:
            conn.close()
        unit = temp_unit()
        metrics = {}
        for i, name in enumerate(METRIC_NAMES, start=1):
            points = [(row[0], row[i]) for row in rows if row[i] is not None]
            series_data = bucket(points, bucket_seconds)
            if name == "temp" and unit != "C":
                for key in ("avg", "min", "max"):
                    series_data[key] = [
                        units.from_celsius(v, unit) for v in series_data[key]
                    ]
            metrics[name] = series_data
        return jsonify(
            {
                "bucket_seconds": bucket_seconds,
                "metrics": metrics,
                "temp_unit_symbol": units.symbol(unit),
            }
        )

    @app.get("/api/events")
    def events():
        since, _ = _range_params()
        conn = connect()
        try:
            rows = db.events_since(conn, since)
        finally:
            conn.close()
        unit = temp_unit()
        if unit != "C":
            for event in rows:
                if event.get("metric") == "temp":
                    for field in _TEMP_EVENT_FIELDS:
                        if field in event:
                            event[field] = units.from_celsius(event[field], unit)
        return jsonify({"events": rows, "temp_unit_symbol": units.symbol(unit)})

    @app.get("/api/latest")
    def latest():
        """Latest indoor reading + open events, for the house hub (#70).

        **Always Celsius, whatever `TEMPERATURE_UNIT` says.** Every sibling
        endpoint converts here because it feeds a browser a human reads; this
        one feeds a machine that formats for itself. Inheriting the setting
        would hand that machine a silent 30-degree error the day the config
        flips, with nothing in the payload to reveal it — so the unit is a
        literal below, not a lookup, and it is named in the response.

        **An empty table is a 200 with a null reading, not an error.** The
        consumer distinguishes "awairelement answered, its poller is dead" from
        "awairelement is unreachable", and those are different colours on its
        card. A 5xx here would collapse the first case into the second.
        """
        conn = connect()
        try:
            reading = db.latest_reading(conn, ("ts", "received_at", *LATEST_METRICS))
            open_events = db.get_open_events(conn)
        finally:
            conn.close()

        payload = {
            # Celsius always — see the docstring. Not `temp_unit_symbol`: the
            # siblings publish "°C" for a template to print, and a consumer
            # parsing units wants the identifier, not the glyph.
            "temp_unit": "C",
            "reading": None,
            # Sorted on the *normalised* stamp rather than the parsed
            # datetime: `db.get_open_events` has no ORDER BY, so without a sort
            # the list arrives in the order rows happened to be written — and
            # sorting the datetimes directly would raise on a naive/aware mix
            # and 500 the endpoint, ahead of the branch in `_iso_utc` that
            # exists to survive exactly that row.
            "open_events": sorted(
                (
                    _public_event(event)
                    for metric, event in open_events.items()
                    if metric not in _NON_MEASUREMENT_METRICS
                ),
                key=lambda event: event["opened_at"],
            ),
        }
        if reading is not None:
            payload["reading"] = {
                # Both clocks, deliberately. `received_at` is this machine's,
                # and is the one a consumer runs staleness off; `ts` is the
                # Element's, and is what a human reads as "as of". Sending only
                # `ts` would make age a subtraction across two machines, so a
                # device clock running fast would render a dead poller green
                # forever instead of stale.
                "ts": _iso_utc(reading["ts"]),
                "received_at": _iso_utc(reading["received_at"]),
                **{name: reading[name] for name in LATEST_METRICS},
            }
        return jsonify(payload)

    @app.get("/api/outdoor-latest")
    def outdoor_latest():
        """Latest outdoor reading, for the house hub's weather card (#71).

        The outdoor sibling of `/api/latest`, and it inherits that endpoint's
        two machine-facing rules verbatim: **source units, always, whatever the
        display config says**, and **an empty table is a 200 with a null
        reading, not an error**.

        Two units are named in the payload rather than left implied, and only
        two, because those are the two this app actually converts somewhere
        else. `temp_unit()` turns temp into F for every browser-facing
        endpoint, and `/api/outdoor-series` divides pressure by `_HPA_PER_INHG`
        to hand the dashboard inHg. A consumer reading raw hPa here and inHg
        there, with neither labelled, would have no way to notice. The
        remaining fields have exactly one spelling in this codebase -- they are
        Open-Meteo's native km/h, mm and µg/m³ everywhere -- so naming them
        would be documentation, not disambiguation, and the README carries it
        instead.

        `aq_ts` is the field to read carefully. It is the air-quality block's
        own observation time, which is hourly while `ts` is quarter-hourly, so
        it is *expected* to lag `ts` by up to an hour on a perfectly healthy
        row. NULL means this row's AQI has no known observation time -- either
        the air-quality fetch failed for that poll (`poll_once` returns
        "partial" and writes the weather half anyway) or the row predates #71.
        Per #71 the hub treats both as "no current AQI" and renders yellow,
        rather than folding an undateable number into a green.
        """
        conn = connect()
        try:
            reading = db.latest_outdoor_reading(
                conn, ("ts", "received_at", "aq_ts", *OUTDOOR_LATEST_FIELDS)
            )
        finally:
            conn.close()

        payload = {
            # Literals, not lookups — see the docstring. `temp_unit()` exists
            # in this module and must not reach this endpoint.
            "temp_unit": "C",
            "pressure_unit": "hPa",
            "reading": None,
        }
        if reading is not None:
            payload["reading"] = {
                # Three clocks, and they answer three different questions.
                # `ts` is Open-Meteo's publish time for the weather block —
                # what a human reads as "as of". `received_at` is this
                # machine's poll time and is the one to measure staleness
                # against, for the same cross-clock reason `/api/latest`
                # publishes it. `aq_ts` dates the AQI specifically and is
                # routinely older than both.
                "ts": _iso_utc(reading["ts"]),
                "received_at": _iso_utc(reading["received_at"]),
                "aq_ts": _iso_utc(reading["aq_ts"]),
                **{name: reading[name] for name in OUTDOOR_LATEST_FIELDS},
            }
        return jsonify(payload)

    @app.get("/api/outdoor-series")
    def outdoor_series():
        since, bucket_seconds = _outdoor_range_params()
        conn = connect()
        try:
            rows = db.outdoor_readings_since(
                conn, ("temp", "precipitation", "pressure"), since
            )
        finally:
            conn.close()
        unit = temp_unit()
        temp_points = [(row[0], row[1]) for row in rows if row[1] is not None]
        precip_points = [(row[0], row[2]) for row in rows if row[2] is not None]
        pressure_points = [(row[0], row[3]) for row in rows if row[3] is not None]
        temp_series = bucket(temp_points, bucket_seconds)
        precip_series = bucket(precip_points, bucket_seconds)
        pressure_series = bucket(pressure_points, bucket_seconds)
        if unit != "C":
            for key in ("avg", "min", "max"):
                temp_series[key] = [
                    units.from_celsius(v, unit) if v is not None else None
                    for v in temp_series[key]
                ]
        for key in ("avg", "min", "max"):
            precip_series[key] = [
                round(v / _MM_PER_INCH, 3) if v is not None else None
                for v in precip_series[key]
            ]
        for key in ("avg", "min", "max"):
            pressure_series[key] = [
                round(v / _HPA_PER_INHG, 2) if v is not None else None
                for v in pressure_series[key]
            ]
        return jsonify(
            {
                "bucket_seconds": bucket_seconds,
                "metrics": {
                    "temp": temp_series,
                    "precipitation": precip_series,
                    "pressure": pressure_series,
                },
                "temp_unit_symbol": units.symbol(unit),
                "daily_events": solar.daily_events(since, datetime.now(UTC)),
            }
        )

    return app
