"""outdoor_readings table: schema, insert idempotency, query."""

from datetime import UTC, datetime, timedelta

import pytest

from awair import db


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
    since = datetime(2026, 1, 1, tzinfo=UTC)
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
    conn2.close()
    assert row == (22.4,)


def test_indoor_pipeline_still_works(conn):
    """Sanity: adding outdoor_readings doesn't disturb the indoor pipeline."""
    now = datetime.now(UTC)
    reading = {col: None for col in db.READING_COLUMNS}
    reading["ts"] = db.iso_z(now - timedelta(hours=1))
    reading["received_at"] = (now - timedelta(hours=1)).isoformat()
    reading["temp"] = 21.0
    assert db.insert_reading(conn, reading) is True
    since = now - timedelta(days=1)
    assert db.metric_history(conn, "temp", since) != []


# --- weather_code + aq_ts, and latest_outdoor_reading (#71) -----------------


def test_new_columns_are_writable_and_read_back(conn):
    row = _row(weather_code=61, aq_ts="2026-07-12T04:00:00+00:00")
    assert db.insert_outdoor_reading(conn, row) is True
    stored = conn.execute("SELECT weather_code, aq_ts FROM outdoor_readings").fetchone()
    assert stored == (61, "2026-07-12T04:00:00+00:00")


def test_an_existing_db_gains_the_new_columns_on_reconnect(tmp_path):
    """The `_migrate` path, exercised against a table that predates #71.

    `CREATE TABLE IF NOT EXISTS` leaves a live table alone, so without the two
    `_add_column` calls the first poll after deploy would die on "no such
    column". Building the old table by hand rather than by an older `db.py` is
    deliberate: it pins the migration against the shape actually running on the
    homelab, not against whatever `SCHEMA` happens to say today.
    """
    path = tmp_path / "old.db"
    import sqlite3

    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE outdoor_readings ("
        " ts TEXT PRIMARY KEY, received_at TEXT NOT NULL,"
        " temp REAL, humid REAL, wind_speed REAL, pressure REAL,"
        " precipitation REAL, pm25 REAL, pm10 REAL, us_aqi INTEGER,"
        " co REAL, o3 REAL)"
    )
    old.execute(
        "INSERT INTO outdoor_readings (ts, received_at, temp, us_aqi)"
        " VALUES ('2026-07-01T00:00:00+00:00', '2026-07-01T00:00:05+00:00', 19.0, 40)"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    try:
        # Pre-migration rows keep their data and gain NULLs, not defaults.
        assert db.latest_outdoor_reading(
            conn, ("temp", "us_aqi", "weather_code", "aq_ts")
        ) == {"temp": 19.0, "us_aqi": 40, "weather_code": None, "aq_ts": None}
        # And the table is now writable at the new width.
        assert (
            db.insert_outdoor_reading(
                conn, _row(weather_code=3, aq_ts="2026-07-12T04:00:00+00:00")
            )
            is True
        )
    finally:
        conn.close()


def test_migration_is_idempotent_across_reconnects(tmp_path):
    """Poller and web both call `connect()` after restart.sh.

    `_add_column` swallows "duplicate column" for exactly this reason; a
    second connect must not raise, and must not disturb stored values.
    """
    path = tmp_path / "twice.db"
    first = db.connect(path)
    db.insert_outdoor_reading(first, _row(weather_code=95))
    first.close()
    second = db.connect(path)
    try:
        assert db.latest_outdoor_reading(second, ("weather_code",)) == {
            "weather_code": 95
        }
    finally:
        second.close()


def test_latest_outdoor_reading_returns_none_on_an_empty_table(conn):
    """ "No row" must be distinguishable from "old row" — see the docstring."""
    assert db.latest_outdoor_reading(conn, ("temp",)) is None


def test_latest_outdoor_reading_picks_the_newest_source_time(conn):
    """Newest by `ts`, and inserted out of order so ORDER BY has to do work."""
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T04:30:00+00:00", temp=22.4))
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T05:00:00+00:00", temp=23.9))
    db.insert_outdoor_reading(conn, _row(ts="2026-07-12T04:45:00+00:00", temp=23.0))
    assert db.latest_outdoor_reading(conn, ("ts", "temp")) == {
        "ts": "2026-07-12T05:00:00+00:00",
        "temp": 23.9,
    }


def test_latest_outdoor_reading_orders_by_source_time_not_arrival(conn):
    """The tie-break the docstring commits to, pinned.

    If Open-Meteo republishes a stale `current.time`, the older *observation*
    is still the latest observation. An implementation that ordered by
    `received_at` would pass every other test in this file and fail this one.
    """
    db.insert_outdoor_reading(
        conn,
        _row(
            ts="2026-07-12T05:00:00+00:00",
            received_at="2026-07-12T05:00:10+00:00",
            temp=23.9,
        ),
    )
    db.insert_outdoor_reading(
        conn,
        _row(
            ts="2026-07-12T04:30:00+00:00",
            received_at="2026-07-12T06:00:00+00:00",  # arrived LAST
            temp=22.4,
        ),
    )
    assert db.latest_outdoor_reading(conn, ("temp",)) == {"temp": 23.9}


def test_latest_outdoor_reading_rejects_unknown_columns(conn):
    with pytest.raises(ValueError):
        db.latest_outdoor_reading(conn, ("no_such_column",))


def test_latest_outdoor_reading_hands_back_an_ancient_row(conn):
    """Unbounded on purpose (#70's reasoning, inherited).

    A `since` filter would make "the outdoor poller died in March" and "this
    house has never had a weather feed" arrive as the same null.
    """
    db.insert_outdoor_reading(conn, _row(ts="2020-01-01T00:00:00+00:00", temp=1.0))
    assert db.latest_outdoor_reading(conn, ("temp",)) == {"temp": 1.0}
