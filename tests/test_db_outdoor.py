"""outdoor_readings table: schema, insert idempotency, query."""

import sqlite3
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


def _column_order(connection, table):
    return [r[1] for r in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def test_schema_column_order_matches_a_migrated_database():
    """A fresh install and a migrated one must agree on physical column order (#77).

    `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so the columns
    #71 added reach a live DB through `_migrate`'s ALTERs -- which can only
    append. If SCHEMA lists them anywhere but last, the two installs diverge and
    SCHEMA stops describing the database it claims to define.

    Nothing depends on the order *today*: there is no `SELECT *` in the
    codebase, every INSERT uses named placeholders and every SELECT names its
    columns. That is the point -- this is cheap to keep true now and expensive
    to discover later, and SCHEMA is read as documentation.

    Built by actually running both paths rather than by parsing the SQL, so it
    fails if `_migrate` changes too, not only if SCHEMA does.

    **Scoped to the two columns #71 added, and it cannot grow itself.** The
    "old" DB is derived from the current SCHEMA by dropping exactly those two,
    so a *future* column added to SCHEMA with no matching `_migrate` ALTER
    appears on both sides and survives this test -- measured, not assumed. The
    `fresh_order[-2:]` assertion below catches the ordering half of that
    (a new column appended after `aq_ts` fails here), but the missing-ALTER half
    needs the drop list to track `_migrate`. Add the column to both lists when
    you add the ALTER.
    """
    import sqlite3

    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)

    # An install predating #71, derived from SCHEMA rather than hand-written:
    # create the current table, then drop the two columns #71 added. DROP COLUMN
    # preserves the order of the survivors, so this is exactly the shape a
    # pre-#71 CREATE TABLE left behind -- and it cannot drift out of date the
    # way a pasted copy of the old DDL would.
    migrated = sqlite3.connect(":memory:")
    migrated.executescript(db.SCHEMA)
    migrated.execute("ALTER TABLE outdoor_readings DROP COLUMN weather_code")
    migrated.execute("ALTER TABLE outdoor_readings DROP COLUMN aq_ts")
    before = _column_order(migrated, "outdoor_readings")
    assert "weather_code" not in before, "fixture no longer models a pre-#71 DB"
    assert "aq_ts" not in before, "fixture no longer models a pre-#71 DB"

    db._migrate(migrated)

    fresh_order = _column_order(fresh, "outdoor_readings")
    migrated_order = _column_order(migrated, "outdoor_readings")
    assert "weather_code" in migrated_order, "the migration under test did not run"
    assert fresh_order == migrated_order
    # The property that makes the two orders agree, named so a future column
    # lands in the right place rather than merely keeping this test green.
    assert fresh_order[-2:] == ["weather_code", "aq_ts"]

    fresh.close()
    migrated.close()


# --- `ts` NOT NULL, and an insert that reports its own failure (#98) --------


def _legacy_outdoor_table(path):
    """A DB carrying the *deployed* outdoor table: `ts TEXT PRIMARY KEY`.

    SQLite's longstanding compatibility bug means that column is nullable, so
    this is the exact shape running on the homelab (verified 2026-09-09:
    `sqlite_master` there still reads `ts TEXT PRIMARY KEY`). Built by hand for
    the same reason `test_an_existing_db_gains_the_new_columns_on_reconnect`
    does — it pins the migration against the live shape, not against SCHEMA.
    """
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE outdoor_readings ("
        " ts TEXT PRIMARY KEY, received_at TEXT NOT NULL,"
        " temp REAL, humid REAL, wind_speed REAL, pressure REAL,"
        " precipitation REAL, pm25 REAL, pm10 REAL, us_aqi INTEGER,"
        " co REAL, o3 REAL, weather_code INTEGER, aq_ts TEXT)"
    )
    return old


def _ts_is_not_null(connection):
    for row in connection.execute("PRAGMA table_info(outdoor_readings)"):
        if row[1] == "ts":
            return bool(row[3])
    raise AssertionError("outdoor_readings has no `ts` column")


def test_the_legacy_table_really_does_admit_a_null_ts(tmp_path):
    """Control for every test below: the fixture models a *broken* DB.

    Without this, a migration that silently did nothing would still let the
    "ts is NOT NULL afterwards" assertions pass on a table that was never
    nullable to begin with.
    """
    old = _legacy_outdoor_table(tmp_path / "control.db")
    assert _ts_is_not_null(old) is False
    for _ in range(2):
        cursor = old.execute(
            "INSERT OR IGNORE INTO outdoor_readings (ts, received_at)"
            " VALUES (NULL, '2026-07-12T04:30:15+00:00')"
        )
        assert cursor.rowcount == 1  # both land; PRIMARY KEY does not dedup NULL
    assert old.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 2
    old.close()


def test_a_fresh_install_gets_a_not_null_ts(conn):
    assert _ts_is_not_null(conn) is True


def test_a_deployed_nullable_ts_migrates_in_place_keeping_its_rows(tmp_path):
    path = tmp_path / "legacy.db"
    old = _legacy_outdoor_table(path)
    old.execute(
        "INSERT INTO outdoor_readings (ts, received_at, temp, us_aqi, weather_code)"
        " VALUES ('2026-07-01T00:00:00+00:00', '2026-07-01T00:00:05+00:00', 19.0, 40, 3)"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    try:
        assert _ts_is_not_null(conn) is True
        assert db.latest_outdoor_reading(
            conn, ("ts", "temp", "us_aqi", "weather_code", "aq_ts")
        ) == {
            "ts": "2026-07-01T00:00:00+00:00",
            "temp": 19.0,
            "us_aqi": 40,
            "weather_code": 3,
            "aq_ts": None,
        }
        assert db.insert_outdoor_reading(conn, _row()) is True
    finally:
        conn.close()


def test_the_migration_drops_unkeyed_rows_and_names_the_count(tmp_path, caplog):
    """Unkeyed rows cannot be salvaged, so they are dropped — but never silently.

    Backfilling `ts` from `received_at` would manufacture an observation time
    the source never published, which is exactly what `_migrate`'s `aq_ts`
    comment refuses to do. Dropping is the honest option; saying so in the log
    is what keeps it from being another silent loss.
    """
    import logging

    path = tmp_path / "unkeyed.db"
    old = _legacy_outdoor_table(path)
    old.execute(
        "INSERT INTO outdoor_readings (ts, received_at, temp)"
        " VALUES ('2026-07-01T00:00:00+00:00', '2026-07-01T00:00:05+00:00', 19.0)"
    )
    for _ in range(3):
        old.execute(
            "INSERT INTO outdoor_readings (ts, received_at, temp)"
            " VALUES (NULL, '2026-07-01T01:00:05+00:00', 20.0)"
        )
    old.commit()
    old.close()

    with caplog.at_level(logging.WARNING, logger="awair.db"):
        conn = db.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1
        assert db.latest_outdoor_reading(conn, ("temp",)) == {"temp": 19.0}
    finally:
        conn.close()
    assert "3" in caplog.text, caplog.text
    assert "outdoor_readings" in caplog.text


def test_a_clean_migration_says_nothing(tmp_path, caplog):
    """The homelab's own case: 5,648 rows, none unkeyed. No warning is owed."""
    import logging

    path = tmp_path / "clean.db"
    old = _legacy_outdoor_table(path)
    old.execute(
        "INSERT INTO outdoor_readings (ts, received_at, temp)"
        " VALUES ('2026-07-01T00:00:00+00:00', '2026-07-01T00:00:05+00:00', 19.0)"
    )
    old.commit()
    old.close()
    with caplog.at_level(logging.WARNING, logger="awair.db"):
        db.connect(path).close()
    assert caplog.text == ""


def test_the_migration_is_idempotent_and_leaves_a_migrated_db_alone(tmp_path):
    path = tmp_path / "twice.db"
    old = _legacy_outdoor_table(path)
    old.commit()
    old.close()
    first = db.connect(path)
    db.insert_outdoor_reading(first, _row(weather_code=95))
    first.close()
    second = db.connect(path)
    try:
        assert _ts_is_not_null(second) is True
        assert db.latest_outdoor_reading(second, ("weather_code",)) == {
            "weather_code": 95
        }
    finally:
        second.close()


def test_a_migrated_table_has_the_same_column_order_as_a_fresh_one(tmp_path):
    """The #77 invariant, extended to the rebuild path.

    A rebuild retypes the whole table, so it is the one migration that *can*
    reorder columns. `SCHEMA` is read as documentation and must keep describing
    the live database.
    """
    import sqlite3

    path = tmp_path / "order.db"
    _legacy_outdoor_table(path).close()
    migrated = db.connect(path)
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(db.SCHEMA)
    try:
        assert _column_order(migrated, "outdoor_readings") == _column_order(
            fresh, "outdoor_readings"
        )
    finally:
        migrated.close()
        fresh.close()


def test_a_null_ts_raises_instead_of_reporting_a_duplicate(conn):
    """The regression `INSERT OR IGNORE` + NOT NULL would have introduced.

    `OR IGNORE` ignores *every* constraint violation, so under a NOT NULL `ts`
    it comes back `rowcount 0` and this function would report `False` — which
    `poll_once` renders as `"duplicate"`, the one status meaning nothing is
    wrong. That is #95's silent half, and adding NOT NULL without naming the
    conflict target would have moved outdoor into it rather than out of it.
    """
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_outdoor_reading(conn, _row(ts=None))
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_dedup_still_works_through_the_named_conflict_target(conn):
    """Naming `ts` must not cost the idempotency the OR IGNORE was there for."""
    assert db.insert_outdoor_reading(conn, _row()) is True
    assert db.insert_outdoor_reading(conn, _row(temp=30.0)) is False
    assert conn.execute("SELECT temp FROM outdoor_readings").fetchone() == (22.4,)


def test_a_failed_insert_does_not_keep_the_write_lock(tmp_path):
    """Verified against real sqlite3: a raising INSERT leaves `in_transaction`
    True, and a second connection then gets "database is locked" until some
    later poll commits. Both pollers share one `AWAIR_DB`, so that stalls the
    *indoor* writer on an outdoor payload fault.
    """
    path = tmp_path / "lock.db"
    conn = db.connect(path)
    other = db.connect(path)
    try:
        with pytest.raises(sqlite3.Error):
            # A nested object in an optional column is a bind-time
            # ProgrammingError -- `parse_reading` hands 12 of these to the
            # driver unchecked.
            db.insert_outdoor_reading(conn, _row(pm25={"unexpected": "object"}))
        assert conn.in_transaction is False
        other.execute("PRAGMA busy_timeout = 300")
        assert db.insert_outdoor_reading(other, _row()) is True
    finally:
        conn.close()
        other.close()


def test_the_loser_of_a_concurrent_migration_leaves_the_table_alone(tmp_path):
    """Poller and web both call `connect()` after restart.sh (#98).

    Two processes can read "nullable" before either takes the write lock. The
    winner rebuilds; the loser wakes up inside `BEGIN IMMEDIATE` holding a
    decision made against a schema that no longer exists, and must re-check
    rather than rebuild a second time.

    The interleaving cannot be produced single-threaded, so the *stale reading*
    is simulated -- one `True` from the schema probe, then the real function.
    Everything after that is real: a real second connection, the real lock, the
    real re-check.

    **This asserts that the loser does not rebuild, not merely that the table
    survives.** A rebuild of an already-migrated table reaches an identical end
    state, so every end-state assertion below passes with the re-check deleted
    -- measured, as a surviving mutant. What the guard actually buys is not
    rewriting several thousand rows under an exclusive write lock while the
    other poller waits on `busy_timeout`, and only a call count can see that.
    """
    from unittest.mock import patch

    path = tmp_path / "race.db"
    _legacy_outdoor_table(path).close()
    winner = db.connect(path)
    db.insert_outdoor_reading(winner, _row())
    winner.close()

    loser = sqlite3.connect(path)
    loser.execute("PRAGMA busy_timeout = 5000")
    real = db._outdoor_ts_is_nullable
    answers = [True]  # the stale reading, taken before the winner committed

    def stale_then_real(conn):
        return answers.pop() if answers else real(conn)

    rebuilds = []

    def counted_rebuild(conn):
        rebuilds.append(conn)
        return db._rebuild_outdoor_readings(conn)

    with (
        patch.object(db, "_outdoor_ts_is_nullable", stale_then_real),
        patch.object(db, "_rebuild_outdoor_readings", counted_rebuild),
    ):
        db._migrate_outdoor_ts_not_null(loser)

    assert not answers, "the stale reading was never consumed; test proves nothing"
    assert rebuilds == [], "the loser rebuilt a table that was already migrated"
    try:
        assert _ts_is_not_null(loser) is True
        assert loser.execute("SELECT temp FROM outdoor_readings").fetchone() == (22.4,)
        assert loser.in_transaction is False
    finally:
        loser.close()


def test_a_failed_rebuild_leaves_the_original_table_intact(tmp_path):
    """The rollback path. A half-migrated `outdoor_readings` is unrecoverable —
    the rebuild drops the source table — so the whole thing is one transaction.

    Forced by pre-creating the scratch table the rebuild wants, which is also
    the shape a *previous* crashed migration would leave behind.
    """
    path = tmp_path / "boom.db"
    old = _legacy_outdoor_table(path)
    old.execute(
        "INSERT INTO outdoor_readings (ts, received_at, temp)"
        " VALUES ('2026-07-01T00:00:00+00:00', '2026-07-01T00:00:05+00:00', 19.0)"
    )
    old.execute("CREATE TABLE outdoor_readings_rebuilt (blocked INTEGER)")
    old.commit()
    old.close()

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db._migrate_outdoor_ts_not_null(conn)
        assert conn.in_transaction is False
        assert _ts_is_not_null(conn) is False  # untouched, not half-migrated
        assert conn.execute("SELECT ts, temp FROM outdoor_readings").fetchall() == [
            ("2026-07-01T00:00:00+00:00", 19.0)
        ]
    finally:
        conn.close()


def test_the_schema_probe_refuses_a_table_it_cannot_read(tmp_path):
    """`PRAGMA table_info` on a missing table returns zero rows, not an error.

    Falling out of that loop with an implicit `None` would read as "not
    nullable" and skip the migration silently, so it raises instead.
    """
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="ts"):
            db._outdoor_ts_is_nullable(conn)
    finally:
        conn.close()
