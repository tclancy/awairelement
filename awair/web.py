"""Dashboard Flask app: one page, JSON series/events endpoints.

Run via: uv run --frozen gunicorn -b 0.0.0.0:8097 'awair.web:create_app()'
"""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

from flask import Flask, abort, jsonify, render_template, request

from awair import db, outdoor, solar, spikes, units, weather_alerts
from awair.series import bucket, carry_forward

METRIC_NAMES = ("co2", "voc", "pm25", "temp", "humid", "score")

# Alert ceilings surfaced to the dashboard as horizontal reference lines so a
# Y-axis autoscaled to a peak doesn't visually collapse "still elevated" into
# "cleared" (#25). Metrics without an entry in spikes.METRICS get no line.
CEILINGS = {name: cfg.ceiling for name, cfg in spikes.METRICS.items()}

# Indoor metric cards that carry a second, overlaid series (#109), and the name
# of the overlay. Rendered onto the card as `data-overlay`, which is the one
# marker the CSS legend reservation and `dashboard.js` both read -- so a card
# cannot end up drawing a fifth legend entry it has not reserved room for.
#
# Only temp, and only temp can be here: the overlay shares the card's Y axis,
# which is sound exactly when both series are the same quantity in the same
# unit. Outdoor humidity onto `humid` would be the other honest candidate; an
# outdoor temperature onto `co2` would not, and this constant is not the place
# that would stop it -- see `_outdoor_temp_on_grid`.
METRIC_OVERLAYS = {"temp": "outdoor-temp"}

# Metric fields on an alert_event whose value carries the same unit as the
# metric itself — converted for temp events at the API boundary.
_TEMP_EVENT_FIELDS = ("peak_value", "baseline", "threshold")

# The reading columns `/api/latest` publishes. Deliberately a short, explicit
# list rather than READING_COLUMNS: the derived and raw-sensor columns
# (`abs_humid`, `dew_point`, `co2_est*`, `voc_*_raw`, `pm10_est`) are internal,
# and a consumer that starts depending on them makes them a contract.
LATEST_METRICS = ("score", "temp", "humid", "co2", "voc", "pm25")

# The outdoor columns `/api/outdoor-latest` publishes, in the order #71 lists
# them, excluding the three clocks (`ts`, `received_at`, `aq_ts`) which the
# handler passes separately because each goes through `_iso_utc`. A whitelist
# for the same reason `LATEST_METRICS` is one -- but note that here it happens
# to cover every remaining column in the table, so the list is doing less
# filtering than its indoor sibling and more *ordering-and-contract* work. A
# column added to `outdoor_readings` later is private until someone adds it
# here on purpose.
OUTDOOR_LATEST_FIELDS = (
    "weather_code",
    "temp",
    "humid",
    "wind_speed",
    "pressure",
    "precipitation",
    # #79. Beside `precipitation` because that is where a reader looks for it,
    # even though `db.OUTDOOR_COLUMNS` has to keep it last to match the ALTER
    # order. NULL here means *unknown* -- the row predates the snowfall
    # migration -- and the hub must not read it as "it is not snowing".
    "snowfall",
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
_NON_MEASUREMENT_METRICS = frozenset({"device", "outdoor"})

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


# The `weather_alerts` fields `/api/weather-alerts` publishes, split by whether
# they are clocks. A whitelist for the same reason `_OPEN_EVENT_FIELDS` is one:
# a column added to the table later is private until someone publishes it on
# purpose. Between them these happen to cover every stored column today.
_ALERT_FIELDS = ("id", "event", "severity", "certainty", "urgency", "headline")
_ALERT_CLOCK_FIELDS = ("onset", "ends", "expires", "first_seen_at", "last_seen_at")


def _alert_clock(value):
    """`_iso_utc` for a third-party timestamp, falling back to the raw string.

    Every other clock this app publishes was written by a process in this repo,
    which is why `_iso_utc` is allowed to raise on those -- an unparseable one
    would be our own bug. These came from NWS. A stamp we cannot normalise must
    not 500 the whole feed, because the consumer would then see an error where
    the honest answer is "here is the alert, and here is its `ends` exactly as
    published". Losing a tornado warning over its end time is the wrong trade
    in the one direction this endpoint cares about.

    `OverflowError` is in the tuple because `astimezone` raises it, not
    `ValueError`, when the offset carries the result out of `datetime`'s range
    -- `"9999-12-31T23:59:59-12:00"` is the reproducer, and it 500s the whole
    feed for as long as NWS keeps publishing that alert. Two accessor styles,
    two exception classes; caught in review, not by the first bad-clock test,
    whose fixture was an unparseable string and so only ever exercised
    `ValueError`.
    """
    try:
        return _iso_utc(value)
    except (TypeError, ValueError, OverflowError):
        return value


def _public_alerts(alerts):
    """The stored alerts as `/api/weather-alerts` publishes them, or None.

    **None passes through as None.** It means "no poll has ever succeeded", and
    mapping it to `[]` here would undo the whole point of the endpoint -- see
    its docstring.
    """
    if alerts is None:
        return None
    return [
        {field: alert[field] for field in _ALERT_FIELDS}
        | {field: _alert_clock(alert[field]) for field in _ALERT_CLOCK_FIELDS}
        for alert in alerts
    ]


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

# The range control's buttons, in the order they are drawn -- narrowest first,
# so the row reads left-to-right from detail to context (#46). The template
# renders from this dict; nothing in `dashboard.html` names a range.
RANGE_LABELS = {"today": "Today", "7d": "7 days", "30d": "30 days"}

# The one place the page's opening range is written down (#108).
#
# It used to be written down four times across three surfaces -- twice as a
# `request.args.get(..., "7d")` default below, once as `aria-pressed="true"` on
# a button in the template, and once as `state.range = "7d"` in
# `dashboard.js` -- with nothing holding them in step. The template now derives
# `aria-pressed` from this constant and `dashboard.js` reads the pressed button
# back out of the DOM, so changing the default is this line and nothing else.
#
# `today` is local midnight, not the last 24 h (see `_since_for`), so shortly
# after midnight the page opens on a nearly empty chart -- and a dashboard left
# open overnight collapses to it at 00:00 on the five-minute refresh. That is
# the literal meaning of the button and is what Tom asked for on #108; the
# 7-day view is one click away.
#
# Whose local, specifically: the WEB PROCESS's, via `datetime.now().astimezone()`.
# Not `AWAIR_TZ`, which only `awair.solar` reads. The homelab box is
# `America/New_York` and sets no `AWAIR_TZ`, so the window follows Tom's wall
# clock and the solar markers fall back to UTC -- they can pick different days
# for a few hours each evening. That divergence predates this change and is
# unaffected by it (clicking Today always did exactly this); it is named here
# because #108 makes it the default rather than a click.
DEFAULT_RANGE = "today"

# Open-Meteo returns precipitation in mm. The dashboard displays inches — Tom's
# expected scale on #31 was "tenths of an inch". Conversion happens at the API
# boundary so storage stays raw (same shape as temperature: DB in Celsius,
# display convert via TEMPERATURE_UNIT).
_MM_PER_INCH = 25.4
# Open-Meteo returns MSL pressure in hPa. The dashboard displays inHg — US
# weather convention, matches the imperial units used for temperature and
# precipitation. Conversion at the API boundary (same pattern as precip).
_HPA_PER_INHG = 33.8639

# How many source publish intervals an outdoor observation stays on the
# combined temperature chart after the one that should have replaced it never
# arrived (#109).
#
# Two, not one: at exactly one, a single skipped poll punches a hole in the
# trace, and the poller skips for ordinary reasons -- a restart, a WAN blip --
# that are not an outage. At two, one missed publish is bridged.
#
# The bound exists at all because the alternative is worse than a gap. Held
# indefinitely, a dead outdoor poller renders as a perfectly flat outdoor line
# beside a moving indoor one -- on a chart whose question is "does indoor
# follow outdoor", that is not a missing answer but a wrong one.
_OUTDOOR_CARRY_INTERVALS = 2


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
    name = request.args.get("range", DEFAULT_RANGE)
    if name not in RANGES:
        abort(400, f"range must be one of {sorted(RANGES)}")
    spec = RANGES[name]
    return _since_for(spec), spec["bucket_seconds"]


def _outdoor_range_params():
    name = request.args.get("range", DEFAULT_RANGE)
    if name not in OUTDOOR_RANGES:
        abort(400, f"range must be one of {sorted(OUTDOOR_RANGES)}")
    spec = OUTDOOR_RANGES[name]
    return _since_for(spec), spec["bucket_seconds"]


def _outdoor_carry_max_age_seconds():
    """How long one outdoor observation may be held onto the chart grid (#109).

    A function rather than a module constant so the derivation from
    `outdoor.SOURCE_INTERVAL_SECONDS` is *observable*: bound at import time,
    `2 * 900` and a literal `1800` are indistinguishable to every test that
    can be written, and the whole claim being made here is that this number
    tracks the source cadence rather than restating it. Read per call, a test
    can move the cadence and watch the window follow -- which is the only way
    to tell a derivation from a coincidence while the cadence happens to be
    900 (#109 review).

    `outdoor.SOURCE_INTERVAL_SECONDS` already warns if Open-Meteo stops
    publishing quarter-hourly; this is what makes that warning actionable
    here rather than merely logged.
    """
    return _OUTDOOR_CARRY_INTERVALS * outdoor.SOURCE_INTERVAL_SECONDS


def _outdoor_temp_on_grid(outdoor_rows, grid, unit):
    """The outdoor temperature trace, in display units, on the indoor x-grid (#109).

    `outdoor_rows` is `db.outdoor_readings_since(conn, ("temp",), ...)` --
    `[(epoch_seconds, celsius)]` -- and `grid` is `metrics["temp"]["t"]`, the
    bucket stamps the indoor temperature chart is already drawn against.

    The two series are sampled an order of magnitude apart (a 30 s indoor poll
    bucketed to 60 s on `today`, against a quarter-hourly outdoor publish), and
    uPlot takes exactly one x array per chart, so the coarse side is held onto
    the fine side's stamps by `series.carry_forward`. Not resampled the other
    way: the indoor trace is the subject of this chart and its detail is the
    thing worth keeping.

    Converted *before* the carry rather than after, so each observation is
    converted once rather than once per grid stamp it is held across -- and so
    the conversion plainly applies to the readings rather than to the drawing.

    NULL temps are not filtered here on purpose: `units.from_celsius` passes
    None through and `carry_forward` drops it, and that rule -- an empty
    window is "nothing landed", never a held value and never evidence of
    freshness -- belongs in one place rather than two.
    """
    points = list(outdoor_rows)
    if unit != "C":
        points = [(t, units.from_celsius(value, unit)) for t, value in points]
    return carry_forward(points, grid, _outdoor_carry_max_age_seconds())


def _bootstrap_schema(db_path, logger) -> None:
    """Run the schema bootstrap once, at app construction rather than per request.

    `connect_readonly` cannot create the database, so something has to, and a
    fresh install may bring the web unit up before either poller has ever run.
    The parent directory is created for the same reason the pollers create it
    (`poller.main`, `outdoor.main`) -- on a new box `~/data/awairelement/` does
    not exist yet.

    Gunicorn runs two workers, so this happens twice at startup. That is the
    same duplicate-column race `db._add_column` already tolerates against the
    pollers, and twice at startup is not once per request (#73).

    **A failure here is logged and swallowed, deliberately.** Raising would
    abort the gunicorn worker, and `awairelement-web.service` is
    `Restart=always`, so an unopenable path -- an unmounted volume, a
    permissions change -- would become a crash loop. Before #73 that same path
    left `/` rendering and 500ed only the `/api/*` routes, and this keeps it
    that way. Nothing is concealed: each request still fails loudly on its own
    `connect_readonly`.
    """
    try:
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        db.connect(db_path).close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("schema bootstrap failed for %s: %s", db_path, exc)


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

    _bootstrap_schema(app.config["AWAIR_DB"], app.logger)

    def connect():
        """Read-only, per request. Every view below is query-only."""
        return db.connect_readonly(app.config["AWAIR_DB"])

    def temp_unit():
        return app.config["TEMPERATURE_UNIT"]

    @app.get("/")
    def dashboard():
        return render_template(
            "dashboard.html",
            metrics=METRIC_NAMES,
            ceilings=CEILINGS,
            temp_unit_symbol=units.symbol(temp_unit()),
            range_labels=RANGE_LABELS,
            default_range=DEFAULT_RANGE,
            overlays=METRIC_OVERLAYS,
        )

    @app.get("/api/series")
    def series():
        since, bucket_seconds = _range_params()
        conn = connect()
        try:
            rows = db.readings_since(conn, METRIC_NAMES, since)
            # One carry window BEFORE `since`, not `since` (#109 review). The
            # observation that was current at the left edge was published
            # before it, so reading from `since` leaves the first grid stamps
            # with nothing at-or-before them and draws up to one publish
            # interval of blank on a perfectly healthy poller -- which the
            # footer promises means an outage. `carry_forward` clips to the
            # grid, so the extra rows cost one bucket's worth of scan.
            outdoor_rows = db.outdoor_readings_since(
                conn,
                ("temp",),
                since - timedelta(seconds=_outdoor_carry_max_age_seconds()),
            )
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
                # #109. A flat list, not a `{t, avg, min, max}` block, because
                # its x values ARE `metrics["temp"]["t"]` -- shipping a second
                # `t` beside it would be the same value published twice with
                # nothing holding the copies in step, and the browser would
                # have to reconcile them before it could draw one chart.
                "outdoor_temp": _outdoor_temp_on_grid(
                    outdoor_rows, metrics["temp"]["t"], unit
                ),
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

        Five units are named in the payload rather than left implied: the
        five the consumer is known to convert. #71's own motivating card reads
        `62°F, 8 mph, 0.00 in`, so the hub turns Celsius into F, km/h into mph
        and mm into inches -- and this app separately turns hPa into inHg at
        the `/api/outdoor-series` boundary and Celsius into F at every
        browser-facing one. `snowfall_unit` joined them in #79, and it is `cm`
        rather than `mm`: Open-Meteo publishes snow depth and rain in different
        units, so one shared label would be a silent 10x. Every one of those is a silent multiply on a
        number the card exists to display, and an unlabelled payload gives a
        consumer no way to notice it guessed wrong.

        The particulate and gas fields are deliberately unlabelled. They have
        one spelling everywhere (Open-Meteo's µg/m³), `us_aqi` is a
        dimensionless index and `humid` a percent, and nothing on either side
        of this contract converts them -- so naming them would be
        documentation rather than disambiguation. The README carries that.

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
            "wind_speed_unit": "km/h",
            "precipitation_unit": "mm",
            # cm, not mm, and it is the fifth unit rather than a second use of
            # `precipitation_unit` because Open-Meteo genuinely publishes the
            # two in different units (#79). Folding snow into the mm label
            # would be a silent 10x on the one number whose threshold is "more
            # than 4 inches".
            "snowfall_unit": "cm",
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

    @app.get("/api/outdoor-today")
    def outdoor_today():
        """Today's outdoor totals and extremes, for the house hub (#79).

        The machine-facing sibling of `/api/outdoor-latest`, and it exists
        because `/api/outdoor-series?range=today` **cannot** answer "how much
        rain fell today", for two independent reasons. It converts to display
        units (`_MM_PER_INCH`, hPa to inHg, Celsius to F) -- the exact silent
        multiply `/api/outdoor-latest` was built to avoid -- and `series.bucket`
        emits avg/min/max with no sum at all.

        There is a trap here worth naming, because a consumer who falls into it
        finds that it works. At `range=today` the bucket is 900 s and so is the
        source cadence, so each bucket holds exactly one point and `avg`
        *equals* the raw value; summing `avg` therefore produces the right rain
        total today, by coincidence, and would start under-reporting silently
        the moment either number changed.

        Inherits the machine-facing rules: **source units regardless of
        `TEMPERATURE_UNIT`**, units named in the payload, and an empty window
        is a 200 rather than an error.

        `row_count` and `contributing_rows` are both published and they are not
        the same number. `row_count` separates "no rain today" from "the poller
        has been down since 03:00" -- a day total over three rows is not a day
        total, and without it the hub renders a confident zero.
        `contributing_rows` is per source column, because a column can be NULL
        on a row that exists: every row predating the `snowfall` migration is
        exactly that, so for one day after deploy `snowfall_total` is a real
        sum over a strict subset of the day. A field with no values at all is
        `null`, never `0`.
        """
        since = _since_for(OUTDOOR_RANGES["today"])
        conn = connect()
        try:
            aggregate = db.outdoor_day_aggregate(conn, since)
        finally:
            conn.close()
        return jsonify(
            {
                # Literals, not lookups, for the same reason the sibling
                # endpoints use literals -- see `/api/outdoor-latest`.
                "temp_unit": "C",
                "wind_speed_unit": "km/h",
                "precipitation_unit": "mm",
                "snowfall_unit": "cm",
                # The window this is a total over, so a consumer can tell which
                # local day it got and how much of it has happened yet.
                "start": _iso_utc(since),
                "end": _iso_utc(datetime.now(UTC)),
                "row_count": aggregate["row_count"],
                "contributing_rows": aggregate["contributing_rows"],
                **aggregate["values"],
            }
        )

    @app.get("/api/weather-alerts")
    def weather_alert_feed():
        """NWS active alerts for the parcel, for the house hub (#79).

        Tornado and hurricane warnings are two of the three conditions the
        hub's card goes red for and neither has an Open-Meteo equivalent, so
        the outdoor poller fetches them from api.weather.gov on its existing
        timer (`awair.weather_alerts`).

        **`alerts` is `null` until a poll has succeeded, and that is the
        contract.** An empty list means "we asked NWS and it said none"; `null`
        means "we have never had an answer". Collapsing the two into `[]` is
        the one failure that turns a missing number into a false all-clear
        about a tornado, so it is the shape rather than a caveat in the docs.
        Both clocks ship beside it: `last_attempt_at` says the poller is alive
        and trying, `last_success_at` is what an all-clear has to be measured
        against, and the two diverging is precisely a sustained NWS outage.

        The endpoint is `/api/weather-alerts` rather than `/api/alerts` as #79
        drafted it. `awair.alerts` is this app's ntfy notifier and
        `alert_events` is its own spike bookkeeping, both a year older than
        this; `/api/alerts` beside `/api/latest`'s `open_events` would read as
        those. One extra word, and the two senses stop colliding.
        """
        conn = connect()
        try:
            feed = weather_alerts.active_alerts(conn, datetime.now(UTC))
        finally:
            conn.close()
        return jsonify(
            {
                "last_attempt_at": _iso_utc(feed["last_attempt_at"]),
                "last_success_at": _iso_utc(feed["last_success_at"]),
                "alerts": _public_alerts(feed["alerts"]),
            }
        )

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
