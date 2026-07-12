"""Poll Open-Meteo for outdoor weather + air quality and store readings.

Run as: python -m awair.outdoor
Config via environment:
  AWAIR_LAT, AWAIR_LON              — required, parcel coords (Ansible-templated)
  AWAIR_DB                          — shared with the indoor poller
  AWAIR_OUTDOOR_POLL_SECONDS        — default 900 (15 min, the native cadence)
  AWAIR_OUTDOOR_WEATHER_URL         — override for test/staging (see DEFAULT_WEATHER_URL)
  AWAIR_OUTDOOR_AIR_QUALITY_URL     — override for test/staging (see DEFAULT_AIR_QUALITY_URL)

Weather refreshes every 15 min at the source; air quality (CAMS-backed) is
hourly. Both are fetched every 15 min and merged into one row keyed on the
weather endpoint's `current.time` — inserts are idempotent via INSERT OR
IGNORE, so a re-poll before Open-Meteo refreshes writes nothing.
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from awair import db

log = logging.getLogger("awair.outdoor")

FETCH_TIMEOUT_SECONDS = 10
DEFAULT_POLL_SECONDS = 900
DEFAULT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WEATHER_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "pressure_msl",
    "precipitation",
)
AIR_QUALITY_FIELDS = (
    "pm2_5",
    "pm10",
    "us_aqi",
    "carbon_monoxide",
    "ozone",
)

# Map Open-Meteo `current.*` keys onto our column names. Kept explicit so a
# rename on either side is a one-line change and reviewers can see the mapping.
WEATHER_TO_COLUMN = {
    "temperature_2m": "temp",
    "relative_humidity_2m": "humid",
    "wind_speed_10m": "wind_speed",
    "pressure_msl": "pressure",
    "precipitation": "precipitation",
}
AIR_QUALITY_TO_COLUMN = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "us_aqi": "us_aqi",
    "carbon_monoxide": "co",
    "ozone": "o3",
}


def _build_url(base: str, lat: float, lon: float, fields: tuple) -> str:
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": ",".join(fields),
            "timezone": "UTC",
        }
    )
    return f"{base}?{params}"


def make_fetch(url: str):
    def fetch() -> str:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def parse_reading(
    weather_payload: dict, air_quality_payload: dict, received_at: str
) -> dict:
    """Merge one weather + one air-quality payload into an outdoor_readings row.

    The weather endpoint's `current.time` is the row's dedup key (`ts`); the
    air-quality endpoint has its own `current.time` (hourly) and is treated
    as auxiliary — its values are stitched in but its timestamp is not the
    primary key. Missing fields (either endpoint) degrade to NULL rather
    than halting; upstream schema drift is a warning, not an outage.
    """
    weather_current = weather_payload["current"]
    reading = {col: None for col in db.OUTDOOR_COLUMNS}
    reading["ts"] = weather_current["time"]
    reading["received_at"] = received_at
    for source_field, column in WEATHER_TO_COLUMN.items():
        reading[column] = weather_current.get(source_field)
    if air_quality_payload is not None:
        aq_current = air_quality_payload.get("current", {})
        for source_field, column in AIR_QUALITY_TO_COLUMN.items():
            reading[column] = aq_current.get(source_field)
    return reading


def poll_once(conn, fetch_weather, fetch_air_quality) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'error', 'partial'.
    'partial' = weather succeeded but air quality failed; the row is
    still inserted with AQ columns NULL because trend data on the
    weather side is more valuable than "all or nothing" here.
    """
    try:
        weather_payload = json.loads(fetch_weather())
    except (OSError, ValueError, KeyError) as exc:
        log.warning("weather fetch failed: %s", exc)
        return "error"
    try:
        air_quality_payload = json.loads(fetch_air_quality())
        status = "ok"
    except (OSError, ValueError, KeyError) as exc:
        log.warning("air-quality fetch failed: %s", exc)
        air_quality_payload = None
        status = "partial"
    try:
        reading = parse_reading(
            weather_payload,
            air_quality_payload,
            received_at=datetime.now(timezone.utc).isoformat(),
        )
    except KeyError as exc:
        log.warning("weather payload missing required field: %s", exc)
        return "error"
    inserted = db.insert_outdoor_reading(conn, reading)
    if not inserted:
        return "duplicate"
    return "inserted" if status == "ok" else "partial"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"{name} is required (parcel coordinates are templated from Ansible)"
        )
    return value


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    lat = float(_require_env("AWAIR_LAT"))
    lon = float(_require_env("AWAIR_LON"))
    db_path = os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    interval = int(os.environ.get("AWAIR_OUTDOOR_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    weather_base = os.environ.get("AWAIR_OUTDOOR_WEATHER_URL", DEFAULT_WEATHER_URL)
    air_quality_base = os.environ.get(
        "AWAIR_OUTDOOR_AIR_QUALITY_URL", DEFAULT_AIR_QUALITY_URL
    )

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)

    fetch_weather = make_fetch(_build_url(weather_base, lat, lon, WEATHER_FIELDS))
    fetch_air_quality = make_fetch(
        _build_url(air_quality_base, lat, lon, AIR_QUALITY_FIELDS)
    )
    log.info(
        "polling Open-Meteo every %ss for (%s, %s) into %s", interval, lat, lon, db_path
    )

    while True:
        status = poll_once(conn, fetch_weather, fetch_air_quality)
        log.log(
            logging.INFO if status in ("inserted", "duplicate") else logging.WARNING,
            "outdoor poll: %s",
            status,
        )
        time.sleep(interval)


if __name__ == "__main__":
    main()
