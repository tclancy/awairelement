"""Schema bootstrap and reading insertion."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import db

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "air_data_latest.json").read_text()
)


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
    conn.close()


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
    conn.close()
    assert "notified_value" in columns


def test_migration_tolerates_losing_the_startup_race(conn):
    # Poller and web both run connect() after restart.sh; the loser's ALTER
    # hits "duplicate column name", which must read as success.
    db._add_column(conn, "alert_events", "notified_value REAL")


def test_connect_adds_fans_engaged_column_to_legacy_db(tmp_path):
    # The fan score-gate latch. A live DB already has open alert_events rows,
    # so the ALTER must supply a default rather than a NOT NULL with no value.
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
    # An event that is already open when the migration lands — exactly the
    # in-flight voc event on homelab today.
    legacy.execute(
        "INSERT INTO alert_events (metric, tier, opened_at) VALUES ('voc', 'ceiling', ?)",
        (NOW.isoformat(),),
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
    assert "fans_engaged" in columns
    # The pre-existing open event defaults to unlatched — it must not
    # spuriously drive the fans the moment we deploy.
    assert db.get_open_events(conn)["voc"]["fans_engaged"] == 0
    conn.close()


# --- fans_engaged latch ---


def test_open_event_starts_unlatched(conn):
    # `fans_engaged` is retained history (ADR-002 removed the code that wrote
    # it). Nothing sets it any more, so the schema default is now the only
    # value a new event can have — pinned here so a migration cannot quietly
    # start defaulting rows to 1.
    _open_voc_event(conn)
    assert db.get_open_events(conn)["voc"]["fans_engaged"] == 0


def _open_voc_event(conn):
    return db.open_event(
        conn,
        metric="voc",
        tier="ceiling",
        opened_at=NOW,
        value=2500.0,
        baseline=800.0,
        threshold=2200.0,
        notified=True,
    )


# --- latest_reading: unbounded, unlike the freshness-bounded getters ---


def test_latest_reading_empty_is_none(conn):
    assert db.latest_reading(conn, ("ts", "score")) is None


def test_latest_reading_returns_the_newest_row_by_device_clock(conn):
    for minutes_ago, score in ((4, 84), (1, 70)):
        ts = db.iso_z(NOW - timedelta(minutes=minutes_ago))
        conn.execute(
            "INSERT INTO readings (ts, received_at, score) VALUES (?, ?, ?)",
            (ts, ts, score),
        )
    conn.commit()
    assert db.latest_reading(conn, ("ts", "score"))["score"] == 70


def test_latest_reading_is_unbounded_where_latest_pm25_is_not(conn):
    """The pair that makes the difference concrete, asserted side by side.

    `latest_pm25` gates the fan suppressor, so an hour-old value must read as
    "no data" — that is `test_latest_pm25_returns_none_when_only_stale_readings`
    further down. `latest_reading` feeds a consumer that does its own staleness
    arithmetic (#70), and returning None for an old row would make "the poller
    died" and "there has never been a sensor" the same answer.

    Written against `latest_score` in #70 and re-pointed here when ADR-002
    removed the score gate that was its only production caller. Any
    freshness-bounded getter carries the contrast; this one is chosen because
    it is still live.
    """
    ts = db.iso_z(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO readings (ts, received_at, score, pm25) VALUES (?, ?, ?, ?)",
        (ts, ts, 70, 5.0),
    )
    conn.commit()
    assert db.latest_pm25(conn, since=NOW - timedelta(minutes=5)) is None
    assert db.latest_reading(conn, ("ts", "score"))["score"] == 70


def test_latest_reading_keeps_nulls_rather_than_skipping_the_row(conn):
    """Unlike `latest_pm25`, which skips null rows to find a usable value.

    Here the newest row *is* the answer even if a sensor channel is missing:
    dropping it would silently serve an older, more complete reading under a
    newer timestamp, which is worse than publishing the null.
    """
    for minutes_ago, score in ((4, 72), (1, None)):
        ts = db.iso_z(NOW - timedelta(minutes=minutes_ago))
        conn.execute(
            "INSERT INTO readings (ts, received_at, score) VALUES (?, ?, ?)",
            (ts, ts, score),
        )
    conn.commit()
    assert db.latest_reading(conn, ("ts", "score"))["score"] is None


def test_latest_reading_rejects_an_unknown_column(conn):
    """Same guard as `readings_since` — the column list is interpolated into
    the SQL, so an unvalidated name is an injection point, not just a typo."""
    with pytest.raises(ValueError, match="unknown columns"):
        db.latest_reading(conn, ("ts", "score; DROP TABLE readings"))


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


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


def test_fan_state_schema_present(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fan_state)")}
    assert cols == {
        "fan_id",
        "last_action",
        "last_command_at",
        "run_started_at",
        "capped",
    }


def test_connect_adds_run_columns_to_legacy_fan_state(tmp_path):
    """The duration cap's bookkeeping lands on a DB that predates it (ADR-002).

    A live box has fan_state rows written before the cap existed. The ALTER
    must supply defaults rather than a bare NOT NULL, and a fan recorded as
    running must migrate to "no known start" — `run_exhausted` reads that as
    not-yet-exhausted, so the next poll adopts it rather than capping it on a
    start time nobody ever observed.
    """
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE fan_state ("
        " fan_id INTEGER PRIMARY KEY, last_action TEXT NOT NULL,"
        " last_command_at TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO fan_state (fan_id, last_action, last_command_at)"
        " VALUES (1, 'speed1', ?)",
        (NOW.isoformat(),),
    )
    legacy.commit()
    legacy.close()

    conn = db.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fan_state)")}
    assert {"run_started_at", "capped"} <= cols
    state = db.get_fan_state(conn, 1)
    assert state["last_action"] == "speed1"  # pre-existing state survives
    assert state["run_started_at"] is None
    assert state["capped"] is False


def test_set_fan_run_round_trips(conn):
    db.set_fan_run(conn, fan_id=1, started_at=NOW, capped=True)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["run_started_at"] == NOW
    assert state["capped"] is True

    db.set_fan_run(conn, fan_id=1, started_at=None, capped=False)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["run_started_at"] is None
    assert state["capped"] is False


def test_set_fan_run_leaves_the_command_state_alone(conn):
    """Run bookkeeping must not disturb what the fan is doing.

    `_drive_one` writes the run before `_command_fan` decides anything, so if
    this clobbered last_action the no-op filter would compare against the wrong
    value and re-send commands the fan is already obeying.
    """
    db.upsert_fan_state(conn, fan_id=1, action="speed1", command_at=NOW)
    db.set_fan_run(conn, fan_id=1, started_at=NOW, capped=True)
    state = db.get_fan_state(conn, fan_id=1)
    assert state["last_action"] == "speed1"
    assert state["last_command_at"] == NOW


def test_set_fan_run_creates_a_row_for_an_unknown_fan(conn):
    # First poll after a fresh install writes the run before any command.
    db.set_fan_run(conn, fan_id=9, started_at=NOW, capped=False)
    state = db.get_fan_state(conn, fan_id=9)
    assert state["last_action"] == "off"
    assert state["run_started_at"] == NOW


def test_fan_state_rejects_out_of_domain_action(conn):
    # CHECK constraint prevents a typo (e.g. 'Speed1') from writing state that
    # decide() would never match, causing infinite retries.
    # Narrowed from a bare Exception: this must fail at the CHECK constraint,
    # not at any error that happens to reach the caller. A blind assert passed
    # equally on an AttributeError from a wrong-typed command_at, which proves
    # nothing about the constraint the test is named for.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
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


def test_readings_since_rejects_an_unknown_column(conn):
    """The guard `latest_reading`'s own test cites as its precedent, which
    until #70 had no test of its own — both build their SQL by interpolating
    the caller's column list, so both are injection points."""
    with pytest.raises(ValueError, match="unknown columns"):
        db.readings_since(conn, ("score; DROP TABLE readings",), NOW)
