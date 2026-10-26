"""`minisplit_readings` storage: the key, the dedup, and the range read (#139).

The table is introduced whole, so it needs no entry in `_migrate` — but it does
need the two guarantees the older two tables had to be repaired to get: a `ts`
that cannot be null, and a conflict clause that names its target so a NOT NULL
violation raises instead of reading as a duplicate (#95, #98).
"""

from datetime import UTC, datetime
from sqlite3 import IntegrityError

import pytest

from awair import db

RECEIVED = "2026-10-26T07:30:00+00:00"


def row(ts, **overrides):
    """A full `minisplit_readings` row, so a column addition fails loudly here."""
    base = {
        "ts": ts,
        "received_at": RECEIVED,
        "window_start": "2026-10-26T07:11:14+00:00",
        "delta_wh": 10.0,
        "watts_spot": 30.0,
        "temp": 15.0,
        "mode": "cool",
        "switch": "on",
    }
    return {**base, **overrides}


def test_the_table_exists_after_a_plain_connect(conn):
    """No migration entry: `CREATE TABLE IF NOT EXISTS` covers both installs."""
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


def test_the_window_close_cannot_be_null(conn):
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
        conn, ("temp", "delta_wh"), datetime(2026, 10, 26, tzinfo=UTC)
    )
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert rows[0][0] == datetime.fromisoformat("2026-10-26T07:11:14+00:00").timestamp()
    assert rows[0][1:] == (15.0, 10.0)


def test_the_range_read_is_bounded_by_the_window_close(conn):
    """A window that opened before `since` and closed inside it is included.

    That is the honest rule for a metered quantity: the energy is only known
    once the device closes the window, so the close is when the reading exists.
    """
    db.insert_minisplit_reading(
        conn,
        row("2026-10-26T07:24:34+00:00", window_start="2026-10-26T07:11:14+00:00"),
    )
    since = datetime.fromisoformat("2026-10-26T07:20:00+00:00")
    assert len(db.minisplit_readings_since(conn, ("temp",), since)) == 1
    later = datetime.fromisoformat("2026-10-26T07:30:00+00:00")
    assert db.minisplit_readings_since(conn, ("temp",), later) == []


def test_an_unknown_column_is_refused_rather_than_interpolated(conn):
    """The column names reach the SQL text, so the whitelist is the injection
    boundary as well as a typo guard."""
    with pytest.raises(ValueError, match="unknown mini split columns"):
        db.minisplit_readings_since(conn, ("temp", "watts"), datetime.now(UTC))


def test_no_row_is_a_shorter_list_not_an_error(conn):
    assert db.minisplit_readings_since(conn, ("temp",), datetime.now(UTC)) == []
