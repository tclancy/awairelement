"""Schema bootstrap and reading insertion."""

import json
import sqlite3
from pathlib import Path

import pytest

from awair import db

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "air_data_latest.json").read_text()
)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def reading_from_fixture(**overrides):
    from awair.poller import parse_reading

    reading = parse_reading(FIXTURE, received_at="2026-07-11T01:24:20+00:00")
    reading.update(overrides)
    return reading


def test_connect_creates_schema(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"readings", "alert_events"} <= tables


def test_connect_is_idempotent(tmp_path):
    db.connect(tmp_path / "test.db").close()
    conn = db.connect(tmp_path / "test.db")  # second bootstrap must not raise
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_connect_enables_wal_and_busy_timeout(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_insert_reading_stores_all_fields(conn):
    assert db.insert_reading(conn, reading_from_fixture()) is True
    row = conn.execute(
        "SELECT ts, received_at, score, co2, voc, voc_ethanol_raw, pm25"
        " FROM readings"
    ).fetchone()
    assert row == (
        "2026-07-11T01:24:22.662Z",
        "2026-07-11T01:24:20+00:00",
        83,
        435,
        267,
        39,
        7,
    )


def test_insert_reading_dedupes_on_device_ts(conn):
    assert db.insert_reading(conn, reading_from_fixture()) is True
    assert db.insert_reading(conn, reading_from_fixture()) is False
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_alert_events_schema_ready_for_slice_2(conn):
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(alert_events)")
    }
    assert {
        "metric",
        "tier",
        "opened_at",
        "closed_at",
        "peak_value",
        "baseline",
        "threshold",
        "open_notified",
        "close_notified",
        "renotified_at",
    } <= cols
