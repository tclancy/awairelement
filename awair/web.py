"""Dashboard Flask app: one page, JSON series/events endpoints.

Run via: uv run --frozen gunicorn -b 0.0.0.0:8097 'awair.web:create_app()'
"""

import os
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, render_template, request

from awair import db, units
from awair.series import bucket

METRIC_NAMES = ("co2", "voc", "pm25", "temp", "humid", "score")

# Metric fields on an alert_event whose value carries the same unit as the
# metric itself — converted for temp events at the API boundary.
_TEMP_EVENT_FIELDS = ("peak_value", "baseline", "threshold")

RANGES = {
    "7d": {"days": 7, "bucket_seconds": 300},
    "30d": {"days": 30, "bucket_seconds": 900},
}

# Outdoor readings publish every 15 min at the source, so bucket sizes are
# scaled up — indoor's 5-min bucket over 7d would leave most outdoor buckets
# empty and paint a jittery gap-riddled line.
OUTDOOR_RANGES = {
    "7d": {"days": 7, "bucket_seconds": 900},
    "30d": {"days": 30, "bucket_seconds": 3600},
}


def _range_params():
    name = request.args.get("range", "7d")
    if name not in RANGES:
        abort(400, f"range must be one of {sorted(RANGES)}")
    spec = RANGES[name]
    since = datetime.now(timezone.utc) - timedelta(days=spec["days"])
    return since, spec["bucket_seconds"]


def _outdoor_range_params():
    name = request.args.get("range", "7d")
    if name not in OUTDOOR_RANGES:
        abort(400, f"range must be one of {sorted(OUTDOOR_RANGES)}")
    spec = OUTDOOR_RANGES[name]
    since = datetime.now(timezone.utc) - timedelta(days=spec["days"])
    return since, spec["bucket_seconds"]


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

    @app.get("/api/outdoor-series")
    def outdoor_series():
        since, bucket_seconds = _outdoor_range_params()
        conn = connect()
        try:
            rows = db.outdoor_readings_since(conn, ("temp",), since)
        finally:
            conn.close()
        unit = temp_unit()
        points = [(row[0], row[1]) for row in rows if row[1] is not None]
        temp_series = bucket(points, bucket_seconds)
        if unit != "C":
            for key in ("avg", "min", "max"):
                temp_series[key] = [
                    units.from_celsius(v, unit) if v is not None else None
                    for v in temp_series[key]
                ]
        return jsonify(
            {
                "bucket_seconds": bucket_seconds,
                "metrics": {"temp": temp_series},
                "temp_unit_symbol": units.symbol(unit),
            }
        )

    return app
