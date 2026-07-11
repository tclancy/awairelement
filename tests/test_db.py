"""Schema bootstrap and reading insertion."""

import json
from datetime import datetime, timedelta, timezone
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
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"readings", "alert_events"} <= tables


def test_connect_is_idempotent(tmp_path):
    db.connect(tmp_path / "test.db").close()
    conn = db.connect(tmp_path / "test.db")  # second bootstrap must not raise
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_connect_enables_wal_and_busy_timeout(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000


def test_connect_adds_notified_value_column_to_legacy_db(tmp_path):
    # DBs created before the escalation feature lack notified_value;
    # connect() must add it in place (CREATE IF NOT EXISTS won't).
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE alert_events ("
        " id INTEGER PRIMARY KEY, metric TEXT NOT NULL, tier TEXT NOT NULL,"
        " opened_at TEXT NOT NULL, closed_at TEXT,"
        " peak_value REAL, baseline REAL, threshold REAL,"
        " open_notified INTEGER NOT NULL DEFAULT 0,"
        " close_notified INTEGER NOT NULL DEFAULT 0, renotified_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    assert "notified_value" in columns


def test_insert_reading_stores_all_fields(conn):
    assert db.insert_reading(conn, reading_from_fixture()) is True
    row = conn.execute(
        "SELECT ts, received_at, score, co2, voc, voc_ethanol_raw, pm25 FROM readings"
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
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


# --- fan_state helpers ---


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def test_fan_state_schema_present(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fan_state)")}
    assert cols == {"fan_id", "last_action", "last_command_at"}


def test_fan_state_rejects_out_of_domain_action(conn):
    # CHECK constraint prevents a typo (e.g. 'Speed1') from writing state that
    # decide() would never match, causing infinite retries.
    with pytest.raises(Exception):
        db.upsert_fan_state(conn, fan_id=1, action="Speed1", command_at=NOW)


def test_get_fan_state_returns_off_default_for_unknown_fan(conn):
    state = db.get_fan_state(conn, fan_id=7)
    assert state["last_action"] == "off"
    # Sentinel is UTC-aware so a `datetime.now(timezone.utc) - state[...]` won't TypeError.
    assert state["last_command_at"].tzinfo is not None


def test_upsert_fan_state_round_trips(conn):
    db.upsert_fan_state(conn, fan_id=1, action="speed2", command_at=NOW)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["last_action"] == "speed2"
    assert state["last_command_at"] == NOW


def test_upsert_fan_state_overwrites(conn):
    later = NOW + timedelta(minutes=5)
    db.upsert_fan_state(conn, fan_id=1, action="speed1", command_at=NOW)
    db.upsert_fan_state(conn, fan_id=1, action="off", command_at=later)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["last_action"] == "off"
    assert state["last_command_at"] == later
    # Only one row per fan.
    assert conn.execute("SELECT COUNT(*) FROM fan_state").fetchone()[0] == 1


def test_latest_pm25_empty_is_none(conn):
    assert db.latest_pm25(conn, since=NOW - timedelta(minutes=5)) is None


def test_latest_pm25_returns_most_recent_within_window(conn):
    # Two fresh readings; the most recent pm25 wins regardless of insert order.
    conn.executemany(
        "INSERT INTO readings (ts, received_at, pm25) VALUES (?, ?, ?)",
        [
            (db.iso_z(NOW - timedelta(minutes=2)), "x", 12.0),
            (db.iso_z(NOW - timedelta(minutes=1)), "x", 30.0),
        ],
    )
    conn.commit()
    assert db.latest_pm25(conn, since=NOW - timedelta(minutes=5)) == 30.0


def test_latest_pm25_skips_nulls_but_stays_in_window(conn):
    conn.executemany(
        "INSERT INTO readings (ts, received_at, pm25) VALUES (?, ?, ?)",
        [
            (db.iso_z(NOW - timedelta(minutes=2)), "x", 12.0),
            (db.iso_z(NOW - timedelta(minutes=1)), "x", None),
        ],
    )
    conn.commit()
    assert db.latest_pm25(conn, since=NOW - timedelta(minutes=5)) == 12.0


def test_latest_pm25_returns_none_when_only_stale_readings(conn):
    # If the last non-null pm25 is older than the freshness window, don't
    # silently trust it — return None so the suppressor treats it as unknown.
    conn.execute(
        "INSERT INTO readings (ts, received_at, pm25) VALUES (?, ?, ?)",
        (db.iso_z(NOW - timedelta(hours=1)), "x", 40.0),
    )
    conn.commit()
    assert db.latest_pm25(conn, since=NOW - timedelta(minutes=5)) is None
