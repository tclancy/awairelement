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


def test_fan_state_schema_present(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fan_state)")}
    assert cols == {"fan_id", "last_action", "last_changed_at", "last_command_at"}


def test_get_fan_state_returns_off_default_for_unknown_fan(conn):
    state = db.get_fan_state(conn, fan_id=7)
    assert state["last_action"] == "off"
    # Sentinel is UTC-aware so a `datetime.now(timezone.utc) - state[...]` won't TypeError.
    assert state["last_command_at"].tzinfo is not None


def test_upsert_fan_state_round_trips(conn):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    db.upsert_fan_state(conn, fan_id=1, action="speed2", changed_at=now, command_at=now)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["last_action"] == "speed2"
    assert state["last_changed_at"] == now


def test_upsert_fan_state_overwrites(conn):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    later = now + timedelta(minutes=5)
    db.upsert_fan_state(conn, fan_id=1, action="speed1", changed_at=now, command_at=now)
    db.upsert_fan_state(
        conn, fan_id=1, action="off", changed_at=later, command_at=later
    )
    state = db.get_fan_state(conn, fan_id=1)
    assert state["last_action"] == "off"
    assert state["last_changed_at"] == later
    # Only one row per fan.
    assert conn.execute("SELECT COUNT(*) FROM fan_state").fetchone()[0] == 1


def test_latest_pm25_empty_is_none(conn):
    assert db.latest_pm25(conn) is None


def test_latest_pm25_returns_most_recent(conn):
    # Two readings; the most recent pm25 wins regardless of insert order.
    conn.executemany(
        "INSERT INTO readings (ts, received_at, pm25) VALUES (?, ?, ?)",
        [
            ("2026-07-12T11:00:00.000Z", "2026-07-12T11:00:00+00:00", 12.0),
            ("2026-07-12T12:00:00.000Z", "2026-07-12T12:00:00+00:00", 30.0),
        ],
    )
    conn.commit()
    assert db.latest_pm25(conn) == 30.0


def test_latest_pm25_skips_nulls(conn):
    conn.executemany(
        "INSERT INTO readings (ts, received_at, pm25) VALUES (?, ?, ?)",
        [
            ("2026-07-12T11:00:00.000Z", "2026-07-12T11:00:00+00:00", 12.0),
            ("2026-07-12T12:00:00.000Z", "2026-07-12T12:00:00+00:00", None),
        ],
    )
    conn.commit()
    assert db.latest_pm25(conn) == 12.0
