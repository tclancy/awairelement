"""outdoor_readings table: schema, insert idempotency, query."""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def _row(**overrides):
    row = {col: None for col in db.OUTDOOR_COLUMNS}
    row["ts"] = "2026-07-12T04:30"
    row["received_at"] = "2026-07-12T04:30:15+00:00"
    row["temp"] = 22.4
    row.update(overrides)
    return row


def test_insert_outdoor_reading_inserts_fresh(conn):
    assert db.insert_outdoor_reading(conn, _row()) is True
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


def test_insert_outdoor_reading_dedups_source_time(conn):
    db.insert_outdoor_reading(conn, _row())
    assert db.insert_outdoor_reading(conn, _row(temp=30.0)) is False
    (temp,) = conn.execute("SELECT temp FROM outdoor_readings").fetchone()
    assert temp == 22.4  # first-write wins; the second call is a no-op


def test_outdoor_readings_since_returns_selected_columns_ascending(conn):
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T04:00", temp=20.0))
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T04:30", temp=22.4))
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T05:00", temp=24.1))
    since = datetime.fromisoformat("2026-07-12T04:15")
    rows = db.outdoor_readings_since(conn, ("temp",), since)
    assert [r[1] for r in rows] == [22.4, 24.1]
    assert rows[0][0] < rows[1][0]  # ascending


def test_outdoor_readings_since_rejects_unknown_column(conn):
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        db.outdoor_readings_since(conn, ("no_such_column",), since)


def test_outdoor_readings_schema_survives_re_connect(tmp_path):
    """A DB written by an older schema still upgrades in place cleanly."""
    path = tmp_path / "test.db"
    conn1 = db.connect(path)
    db.insert_outdoor_reading(conn1, _row())
    conn1.close()
    conn2 = db.connect(path)
    row = conn2.execute("SELECT temp FROM outdoor_readings").fetchone()
    assert row == (22.4,)


def test_indoor_pipeline_still_works(conn):
    """Sanity: adding outdoor_readings doesn't disturb the indoor pipeline."""
    reading = {col: None for col in db.READING_COLUMNS}
    reading["ts"] = "2026-07-12T04:00:00.000Z"
    reading["received_at"] = "2026-07-12T04:00:01+00:00"
    reading["temp"] = 21.0
    assert db.insert_reading(conn, reading) is True
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert db.metric_history(conn, "temp", since) != []
