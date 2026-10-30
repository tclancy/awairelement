"""`basement_readings` storage: the key and the dedup.

Introduced whole, so like `minisplit_readings` it needs no `_migrate` entry but
does need the two guarantees the older tables were repaired to get: a `ts` that
cannot be null, and a conflict clause that names its target so a NOT NULL
violation raises instead of reading as a duplicate (#95, #98).
"""

from sqlite3 import IntegrityError

import pytest

from awair import db


def row(ts="2026-10-30T12:34:00+00:00", **overrides):
    base = {
        "ts": ts,
        "received_at": "2026-10-30T12:34:56.789000+00:00",
        "temp": 15.1268,
    }
    return {**base, **overrides}


def test_the_physical_columns_are_the_published_list_in_order(conn):
    physical = tuple(r[1] for r in conn.execute("PRAGMA table_info(basement_readings)"))
    assert physical == db.BASEMENT_COLUMNS


def test_a_fresh_minute_inserts_and_a_repeat_of_it_is_a_duplicate(conn):
    assert db.insert_basement_reading(conn, row()) is True
    assert db.insert_basement_reading(conn, row(temp=99.0)) is False
    assert conn.execute("SELECT temp FROM basement_readings").fetchall() == [(15.1268,)]


@pytest.mark.parametrize("column", ["ts", "received_at", "temp"])
def test_a_null_required_column_raises_rather_than_reading_as_a_duplicate(conn, column):
    with pytest.raises(IntegrityError):
        db.insert_basement_reading(conn, row(**{column: None}))
    # The rollback leaves the connection usable for the next poll.
    assert db.insert_basement_reading(conn, row()) is True
