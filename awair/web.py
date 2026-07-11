"""Dashboard Flask app: one page, JSON series/events endpoints.

Run via: uv run --frozen gunicorn -b 0.0.0.0:8097 'awair.web:create_app()'
"""

import os
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, render_template, request

from awair import db
from awair.series import bucket

METRIC_NAMES = ("co2", "voc", "pm25", "temp", "humid", "score")

RANGES = {
    "7d": {"days": 7, "bucket_seconds": 300},
    "30d": {"days": 30, "bucket_seconds": 900},
}


def _range_params():
    name = request.args.get("range", "7d")
    if name not in RANGES:
        abort(400, f"range must be one of {sorted(RANGES)}")
    spec = RANGES[name]
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

    def connect():
        return db.connect(app.config["AWAIR_DB"])

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", metrics=METRIC_NAMES)

    @app.get("/api/series")
    def series():
        since, bucket_seconds = _range_params()
        conn = connect()
        try:
            rows = db.readings_since(conn, METRIC_NAMES, since)
        finally:
            conn.close()
        metrics = {}
        for i, name in enumerate(METRIC_NAMES, start=1):
            points = [(row[0], row[i]) for row in rows if row[i] is not None]
            metrics[name] = bucket(points, bucket_seconds)
        return jsonify({"bucket_seconds": bucket_seconds, "metrics": metrics})

    @app.get("/api/events")
    def events():
        since, _ = _range_params()
        conn = connect()
        try:
            return jsonify({"events": db.events_since(conn, since)})
        finally:
            conn.close()

    return app
