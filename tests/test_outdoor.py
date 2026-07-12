"""Open-Meteo outdoor poller: parse, dedup, error and partial paths."""

import json
from urllib.error import URLError

import pytest

from awair import db
from awair.outdoor import (
    AIR_QUALITY_FIELDS,
    WEATHER_FIELDS,
    _build_url,
    parse_reading,
    poll_once,
)

RECEIVED = "2026-07-12T04:30:00+00:00"

WEATHER = {
    "current": {
        "time": "2026-07-12T04:30",
        "interval": 900,
        "temperature_2m": 22.4,
        "relative_humidity_2m": 68,
        "wind_speed_10m": 3.2,
        "pressure_msl": 1013.2,
        "precipitation": 0.0,
    }
}
WEATHER_TEXT = json.dumps(WEATHER)

AIR_QUALITY = {
    "current": {
        "time": "2026-07-12T04:00",
        "pm2_5": 5.6,
        "pm10": 8.1,
        "us_aqi": 32,
        "carbon_monoxide": 200,
        "ozone": 55,
    }
}
AIR_QUALITY_TEXT = json.dumps(AIR_QUALITY)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_parse_reading_merges_weather_and_air_quality():
    reading = parse_reading(WEATHER, AIR_QUALITY, received_at=RECEIVED)
    # Open-Meteo's minute-precision naive `current.time` is normalized to a
    # full ISO UTC string so lexicographic `ts >= ?` filters work correctly.
    assert reading["ts"] == "2026-07-12T04:30:00+00:00"
    assert reading["received_at"] == RECEIVED
    assert reading["temp"] == 22.4
    assert reading["humid"] == 68
    assert reading["wind_speed"] == 3.2
    assert reading["pressure"] == 1013.2
    assert reading["precipitation"] == 0.0
    assert reading["pm25"] == 5.6
    assert reading["pm10"] == 8.1
    assert reading["us_aqi"] == 32
    assert reading["co"] == 200
    assert reading["o3"] == 55


def test_parse_reading_tolerates_missing_air_quality_field():
    aq = {"current": dict(AIR_QUALITY["current"])}
    del aq["current"]["ozone"]
    reading = parse_reading(WEATHER, aq, received_at=RECEIVED)
    assert reading["o3"] is None
    assert reading["pm25"] == 5.6


def test_parse_reading_tolerates_missing_weather_field():
    """A weather-endpoint schema drift dropping a field falls back to NULL."""
    payload = {"current": dict(WEATHER["current"])}
    del payload["current"]["precipitation"]
    reading = parse_reading(payload, AIR_QUALITY, received_at=RECEIVED)
    assert reading["precipitation"] is None
    assert reading["temp"] == 22.4


def test_parse_reading_requires_weather_time():
    payload = {"current": dict(WEATHER["current"])}
    del payload["current"]["time"]
    with pytest.raises(KeyError):
        parse_reading(payload, AIR_QUALITY, received_at=RECEIVED)


def test_parse_reading_normalizes_naive_open_meteo_time():
    """Prod payloads carry a naive `HH:MM` string; storage needs full ISO+tz."""
    reading = parse_reading(WEATHER, AIR_QUALITY, received_at=RECEIVED)
    # Full ISO with UTC offset — sorts lexicographically alongside
    # `since.isoformat()` values from callers.
    assert reading["ts"] == "2026-07-12T04:30:00+00:00"


def test_parse_reading_null_air_quality_gives_null_aq_columns():
    """Partial-fetch path: weather succeeded, AQ endpoint failed."""
    reading = parse_reading(WEATHER, None, received_at=RECEIVED)
    assert reading["temp"] == 22.4
    assert reading["pm25"] is None
    assert reading["us_aqi"] is None


def test_poll_once_inserts_fresh_row(conn):
    status = poll_once(
        conn,
        fetch_weather=lambda: WEATHER_TEXT,
        fetch_air_quality=lambda: AIR_QUALITY_TEXT,
    )
    assert status == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


def test_poll_once_reports_duplicate_source_time(conn):
    poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT)
    assert (
        poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT) == "duplicate"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


def test_poll_once_weather_error_returns_error_without_inserting(conn):
    def failing():
        raise URLError("weather down")

    assert poll_once(conn, failing, lambda: AIR_QUALITY_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_poll_once_air_quality_failure_still_inserts_partial(conn):
    """AQ endpoint outage must not wedge weather ingestion."""

    def failing():
        raise URLError("aq down")

    status = poll_once(conn, lambda: WEATHER_TEXT, failing)
    assert status == "partial"
    row = conn.execute("SELECT temp, pm25, us_aqi FROM outdoor_readings").fetchone()
    assert row == (22.4, None, None)


def test_poll_once_bad_json_reports_error(conn):
    assert poll_once(conn, lambda: "<html>", lambda: AIR_QUALITY_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_build_url_encodes_params():
    url = _build_url("https://example.test/x", 43.1, -70.9, WEATHER_FIELDS)
    assert url.startswith("https://example.test/x?")
    assert "latitude=43.1" in url
    assert "longitude=-70.9" in url
    assert "current=" in url
    for field in WEATHER_FIELDS:
        assert field in url


def test_build_url_carries_all_air_quality_fields():
    url = _build_url("https://example.test/aq", 43.1, -70.9, AIR_QUALITY_FIELDS)
    for field in AIR_QUALITY_FIELDS:
        assert field in url
