"""Parsing device JSON and single poll iterations."""

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from awair.poller import parse_reading, poll_once

FIXTURE_TEXT = (Path(__file__).parent / "fixtures" / "air_data_latest.json").read_text()
FIXTURE = json.loads(FIXTURE_TEXT)

RECEIVED = "2026-07-11T01:24:20+00:00"


def test_parse_reading_maps_device_fields():
    reading = parse_reading(FIXTURE, received_at=RECEIVED)
    assert reading["ts"] == "2026-07-11T01:24:22.662Z"
    assert reading["received_at"] == RECEIVED
    assert reading["score"] == 83
    assert reading["temp"] == 24.45
    assert reading["humid"] == 64.67
    assert reading["abs_humid"] == 14.40
    assert reading["dew_point"] == 17.36
    assert reading["co2"] == 435
    assert reading["co2_est"] == 400
    assert reading["co2_est_baseline"] == 37731
    assert reading["voc"] == 267
    assert reading["voc_baseline"] == 40869
    assert reading["voc_h2_raw"] == 27
    assert reading["voc_ethanol_raw"] == 39
    assert reading["pm25"] == 7
    assert reading["pm10_est"] == 8


def test_parse_reading_tolerates_missing_sensor_field():
    payload = dict(FIXTURE)
    del payload["pm10_est"]
    reading = parse_reading(payload, received_at=RECEIVED)
    assert reading["pm10_est"] is None


def test_parse_reading_requires_device_timestamp():
    payload = dict(FIXTURE)
    del payload["timestamp"]
    with pytest.raises(KeyError):
        parse_reading(payload, received_at=RECEIVED)


def test_poll_once_inserts_fresh_reading(conn):
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_poll_once_reports_duplicate_device_ts(conn):
    poll_once(conn, fetch=lambda: FIXTURE_TEXT)
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_poll_once_reports_fetch_error_without_inserting(conn):
    def failing_fetch():
        raise URLError("device unreachable")

    assert poll_once(conn, fetch=failing_fetch) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_poll_once_reports_bad_json_as_error(conn):
    assert poll_once(conn, fetch=lambda: "<html>not json</html>") == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_main_with_test_flag_runs_fan_test_and_exits(monkeypatch, tmp_path):
    from awair import poller

    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "test.db"))
    ran = []
    monkeypatch.setattr(poller, "run_fan_test", lambda *a, **k: ran.append(a))
    poller.main(["--test"])  # must return instead of entering the poll loop
    assert len(ran) == 1
