"""`minisplit_readings` storage: the key, the dedup, and the range read (#139).

The key is the BRIDGE CYCLE's own clock, not the device's power-report window
and not our poll clock -- see `db.MINISPLIT_COLUMNS_DDL` for why the obvious
choice was the wrong one.

Both heads share the table, keyed on `(head, ts)` (the heads scope,
2026-10-31). A deployed dining-only table is rebuilt into that shape by
`_migrate_minisplit_heads`. The key keeps the two guarantees the older tables
had to be repaired to get: neither half can be null, and the conflict clause
names its target so a NOT NULL violation raises instead of reading as a
duplicate (#95, #98).
"""

from datetime import UTC, datetime
from sqlite3 import IntegrityError

import pytest

from awair import db

RECEIVED = "2026-10-26T07:30:00+00:00"


def row(ts, **overrides):
    """A full `minisplit_readings` row, so a column addition fails loudly here."""
    base = {
        "head": "dining",
        "ts": ts,
        "received_at": RECEIVED,
        "window_start": "2026-10-26T07:11:14+00:00",
        "window_close": "2026-10-26T07:24:34+00:00",
        "delta_wh": 10.0,
        "watts_spot": 30.0,
        "temp": 15.0,
        "mode": "cool",
        "switch": "on",
        "setpoint": 10.0,
        "target": 20.0,
        "target_source": "schedule",
        "decision_reason": "in-band",
        "temperature_at": "2026-10-26T07:20:00+00:00",
        "raw": None,
    }
    return {**base, **overrides}


def test_the_table_exists_after_a_plain_connect(conn):
    """A fresh install gets the per-head table straight from `SCHEMA`."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    assert "minisplit_readings" in names


def test_the_physical_columns_are_the_published_list_in_order(conn):
    """`MINISPLIT_COLUMNS` is what every query names, so it must match the DDL.

    One list rather than two is what keeps `insert_minisplit_reading`'s
    placeholder binding and the table's own shape from drifting; this is the
    assertion that makes the single definition provable rather than assumed.
    """
    physical = tuple(
        r[1] for r in conn.execute("PRAGMA table_info(minisplit_readings)")
    )
    assert physical == db.MINISPLIT_COLUMNS


def test_the_cycle_clock_cannot_be_null(conn):
    """SQLite gives a non-INTEGER primary key no implicit NOT NULL (#98).

    Without the explicit constraint two null-`ts` rows both insert and the
    table accumulates readings no dedup and no `ORDER BY ts` can key on.
    """
    with pytest.raises(IntegrityError):
        db.insert_minisplit_reading(conn, row(None))


def test_a_rejected_insert_does_not_leave_the_write_lock_held(conn, tmp_path):
    """The rollback in the error path, proven by a second connection writing.

    Three writers share one `AWAIR_DB`, so an unresolved implicit transaction
    here stalls the indoor and outdoor pollers too, not just the next mini
    split poll.
    """
    with pytest.raises(IntegrityError):
        db.insert_minisplit_reading(conn, row(None))
    other = db.connect(tmp_path / "test.db")
    try:
        assert db.insert_minisplit_reading(other, row("2026-10-26T07:24:34+00:00"))
    finally:
        other.close()


def test_the_same_window_is_stored_once_however_often_it_is_re_read(conn):
    assert db.insert_minisplit_reading(conn, row("2026-10-26T07:24:34+00:00"))
    assert not db.insert_minisplit_reading(
        conn, row("2026-10-26T07:24:34+00:00", delta_wh=99.0)
    )
    stored = conn.execute("SELECT delta_wh FROM minisplit_readings").fetchall()
    assert stored == [(10.0,)]


def test_the_range_read_returns_epoch_stamps_ascending(conn):
    for ts in (
        "2026-10-26T07:37:54+00:00",
        "2026-10-26T07:11:14+00:00",
        "2026-10-26T07:24:34+00:00",
    ):
        db.insert_minisplit_reading(conn, row(ts))
    rows = db.minisplit_readings_since(
        conn, "dining", ("temp", "delta_wh"), datetime(2026, 10, 26, tzinfo=UTC)
    )
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert rows[0][0] == datetime.fromisoformat("2026-10-26T07:11:14+00:00").timestamp()
    assert rows[0][1:] == (15.0, 10.0)


def test_the_range_read_is_bounded_by_the_cycle_clock(conn):
    """A window that opened before `since` is included if its cycle is inside it.

    The row exists when the bridge read it, so that is what the range filter
    answers -- and a window that closed earlier still travels on it.
    """
    db.insert_minisplit_reading(
        conn,
        row("2026-10-26T07:24:34+00:00", window_start="2026-10-26T07:11:14+00:00"),
    )
    since = datetime.fromisoformat("2026-10-26T07:20:00+00:00")
    assert len(db.minisplit_readings_since(conn, "dining", ("temp",), since)) == 1
    later = datetime.fromisoformat("2026-10-26T07:30:00+00:00")
    assert db.minisplit_readings_since(conn, "dining", ("temp",), later) == []


def test_an_unknown_column_is_refused_rather_than_interpolated(conn):
    """The column names reach the SQL text, so the whitelist is the injection
    boundary as well as a typo guard."""
    with pytest.raises(ValueError, match="unknown mini split columns"):
        db.minisplit_readings_since(
            conn, "dining", ("temp", "watts"), datetime.now(UTC)
        )


def test_no_row_is_a_shorter_list_not_an_error(conn):
    assert (
        db.minisplit_readings_since(conn, "dining", ("temp",), datetime.now(UTC)) == []
    )


def test_the_head_cannot_be_null_either(conn):
    with pytest.raises(IntegrityError):
        db.insert_minisplit_reading(conn, row("2026-10-26T07:24:34+00:00", head=None))


def test_both_heads_store_the_same_cycle_clock_and_each_dedups_alone(conn):
    """The dedup is per head: one head's re-read must not shadow the other's."""
    ts = "2026-10-26T07:24:34+00:00"
    assert db.insert_minisplit_reading(conn, row(ts))
    assert db.insert_minisplit_reading(conn, row(ts, head="bedroom"))
    assert not db.insert_minisplit_reading(conn, row(ts, head="bedroom"))
    assert conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 2


def test_the_range_read_returns_only_the_head_it_is_asked_for(conn):
    db.insert_minisplit_reading(conn, row("2026-10-26T07:24:34+00:00", temp=15.0))
    db.insert_minisplit_reading(
        conn, row("2026-10-26T07:25:00+00:00", head="bedroom", temp=14.0)
    )
    since = datetime(2026, 10, 26, tzinfo=UTC)
    assert [
        r[1] for r in db.minisplit_readings_since(conn, "dining", ("temp",), since)
    ] == [15.0]
    assert [
        r[1] for r in db.minisplit_readings_since(conn, "bedroom", ("temp",), since)
    ] == [14.0]


def test_the_latest_raw_skips_rows_that_stored_none(conn):
    db.insert_minisplit_reading(conn, row("2026-10-26T07:20:00+00:00", raw='{"a": 1}'))
    db.insert_minisplit_reading(conn, row("2026-10-26T07:25:00+00:00", raw=None))
    db.insert_minisplit_reading(
        conn, row("2026-10-26T07:30:00+00:00", head="bedroom", raw='{"b": 2}')
    )
    assert db.latest_minisplit_raw(conn, "dining") == '{"a": 1}'
    assert db.latest_minisplit_raw(conn, "bedroom") == '{"b": 2}'


# -- the migration from the dining-only table ---------------------------------

LEGACY_DDL = """
    ts TEXT NOT NULL PRIMARY KEY,
    received_at TEXT NOT NULL,
    window_start TEXT,
    window_close TEXT,
    delta_wh REAL,
    watts_spot REAL,
    temp REAL,
    mode TEXT,
    switch TEXT
"""


def legacy_db(path, rows):
    """A database whose `minisplit_readings` is the table deployed before this."""
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute(f"CREATE TABLE minisplit_readings ({LEGACY_DDL})")
    raw.executemany(
        "INSERT INTO minisplit_readings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    raw.commit()
    raw.close()


LEGACY_ROWS = [
    ("2026-10-30T10:00:00+00:00", RECEIVED, None, None, None, None, 18.0, "heat", "on"),
    ("2026-10-30T10:05:00+00:00", RECEIVED, "a", "b", 5.0, 30.0, 18.5, "heat", "on"),
]


def test_a_deployed_dining_table_is_rebuilt_with_every_row_as_dining(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_db(path, LEGACY_ROWS)
    conn = db.connect(path)
    try:
        physical = tuple(
            r[1] for r in conn.execute("PRAGMA table_info(minisplit_readings)")
        )
        assert physical == db.MINISPLIT_COLUMNS
        legacy = ", ".join(db.MINISPLIT_LEGACY_COLUMNS)
        stored = conn.execute(
            f"SELECT head, {legacy} FROM minisplit_readings ORDER BY ts"
        ).fetchall()
        assert stored == [("dining", *r) for r in LEGACY_ROWS]
        # And the new key is live on the migrated table.
        assert db.insert_minisplit_reading(conn, row(LEGACY_ROWS[0][0], head="bedroom"))
        assert not db.insert_minisplit_reading(conn, row(LEGACY_ROWS[0][0]))
    finally:
        conn.close()


def test_a_migrated_table_is_the_same_table_as_a_fresh_one(tmp_path):
    legacy_db(tmp_path / "legacy.db", LEGACY_ROWS)
    migrated = db.connect(tmp_path / "legacy.db")
    fresh = db.connect(tmp_path / "fresh.db")
    try:
        # Name, type, NOT NULL, default and key position of every column. Not
        # the stored CREATE text: a RENAME quotes the table name in it.
        shape = "PRAGMA table_info(minisplit_readings)"
        assert migrated.execute(shape).fetchall() == fresh.execute(shape).fetchall()
    finally:
        migrated.close()
        fresh.close()


def test_the_migration_runs_once(tmp_path):
    """A second connect finds the table already re-keyed and leaves it be."""
    path = tmp_path / "legacy.db"
    legacy_db(path, LEGACY_ROWS)
    db.connect(path).close()
    conn = db.connect(path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 2
        )
    finally:
        conn.close()


def test_a_column_the_rebuild_does_not_know_refuses_rather_than_dropping(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy_db(path, LEGACY_ROWS)
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE minisplit_readings ADD COLUMN mystery TEXT")
    raw.commit()
    raw.close()
    with pytest.raises(sqlite3.OperationalError, match="mystery"):
        db.connect(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 2
    finally:
        raw.close()
