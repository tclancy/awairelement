"""alert_events persistence helpers."""

from datetime import datetime, timezone

import pytest

from awair import db

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_no_open_events_initially(conn):
    assert db.get_open_events(conn) == {}


def test_open_event_roundtrip(conn):
    event_id = db.open_event(
        conn,
        metric="co2",
        tier="ceiling",
        opened_at=NOW,
        value=1400.0,
        baseline=520.0,
        threshold=1200.0,
        notified=True,
    )
    events = db.get_open_events(conn)
    assert set(events) == {"co2"}
    event = events["co2"]
    assert event["id"] == event_id
    assert event["tier"] == "ceiling"
    assert event["opened_at"] == NOW
    assert event["renotified_at"] is None
    assert event["peak_value"] == 1400.0


def test_close_event_removes_from_open(conn):
    event_id = db.open_event(
        conn,
        metric="co2",
        tier="relative",
        opened_at=NOW,
        value=900.0,
        baseline=500.0,
        threshold=800.0,
        notified=True,
    )
    db.close_event(conn, event_id, closed_at=NOW, notified=True)
    assert db.get_open_events(conn) == {}
    row = conn.execute(
        "SELECT closed_at, close_notified FROM alert_events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row[0] is not None and row[1] == 1


def test_update_peak_keeps_maximum(conn):
    event_id = db.open_event(
        conn,
        metric="voc",
        tier="relative",
        opened_at=NOW,
        value=800.0,
        baseline=200.0,
        threshold=500.0,
        notified=True,
    )
    db.update_peak(conn, event_id, 1200.0)
    db.update_peak(conn, event_id, 900.0)  # lower: must not regress
    assert db.get_open_events(conn)["voc"]["peak_value"] == 1200.0


def test_mark_renotified(conn):
    event_id = db.open_event(
        conn,
        metric="pm25",
        tier="ceiling",
        opened_at=NOW,
        value=50.0,
        baseline=5.0,
        threshold=35.0,
        notified=True,
    )
    db.mark_renotified(conn, event_id, NOW)
    assert db.get_open_events(conn)["pm25"]["renotified_at"] == NOW
