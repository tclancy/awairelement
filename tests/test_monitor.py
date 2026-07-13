"""Monitor glue: readings → detection → alert_events rows → notifications.

Uses ceiling-tier shapes (active even in cold start) to keep seeded
histories small; tier-1 shapes are covered in test_spikes.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db
from awair.monitor import DeviceHealth, check_metrics
from tests._helpers import FakeNotifier


@pytest.fixture(autouse=True)
def default_celsius(monkeypatch):
    """Isolate each test from any inherited TEMPERATURE_UNIT override."""
    monkeypatch.delenv("TEMPERATURE_UNIT", raising=False)


NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def seed(conn, co2_values, end=NOW):
    n = len(co2_values)
    rows = []
    for i, co2 in enumerate(co2_values):
        ts = end - timedelta(seconds=30 * (n - 1 - i))
        rows.append((iso_z(ts), iso_z(ts), co2, 100, 3.0))
    conn.executemany(
        "INSERT INTO readings (ts, received_at, co2, voc, pm25) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def test_ceiling_spike_opens_event_and_notifies(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)

    events = db.get_open_events(conn)
    assert set(events) == {"co2"}
    assert events["co2"]["tier"] == "ceiling"
    assert len(notifier.sent) == 1
    title, message, priority = notifier.sent[0]
    assert "co2" in title.lower() or "co2" in message.lower()
    assert priority == "high"  # ceilings page loudly


def test_open_event_does_not_renotify_on_next_poll(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)
    seed(conn, [1400], end=NOW + timedelta(seconds=30))
    check_metrics(conn, notifier, now=NOW + timedelta(seconds=30))
    assert len(notifier.sent) == 1  # anti-spam: one notification per event
    assert db.get_open_events(conn)["co2"]["peak_value"] == 1400.0


def test_recovery_closes_event_and_sends_cleared(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)

    later = NOW + timedelta(minutes=15)
    seed(conn, [500] * 25, end=later)  # >10 min below both thresholds
    check_metrics(conn, notifier, now=later)

    assert db.get_open_events(conn) == {}
    assert len(notifier.sent) == 2
    assert (
        "clear" in notifier.sent[1][0].lower() or "clear" in notifier.sent[1][1].lower()
    )


# --- escalation: mid-event tier promotion and reference laddering ---


def test_ceiling_crossing_escalates_open_event(conn):
    db.open_event(
        conn,
        metric="co2",
        tier="relative",
        opened_at=NOW - timedelta(hours=1),
        value=900.0,
        baseline=500.0,
        threshold=800.0,
        notified=True,
    )
    seed(conn, [1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)

    event = db.get_open_events(conn)["co2"]
    assert event["tier"] == "ceiling"
    assert event["notified_value"] == 1350.0
    assert len(notifier.sent) == 1
    title, message, priority = notifier.sent[0]
    assert priority == "high"
    assert "escalat" in (title + message).lower()


def test_escalation_ladder_survives_low_outlier_sample(conn):
    db.open_event(
        conn,
        metric="co2",
        tier="ceiling",
        opened_at=NOW - timedelta(hours=1),
        value=1300.0,
        baseline=500.0,
        threshold=1200.0,
        notified=True,
    )
    seed(conn, [2700, 2700, 2700, 700])  # median 2700 trips 2x1300; 700 is noise
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)
    assert len(notifier.sent) == 1

    # Next poll back at the plateau: the ladder must have re-armed at the
    # sustained level (2700), so 2700 < 5400 stays silent.
    seed(conn, [2700], end=NOW + timedelta(seconds=30))
    check_metrics(conn, notifier, now=NOW + timedelta(seconds=30))
    assert len(notifier.sent) == 1


def test_escalation_message_includes_trigger_sample_in_peak(conn):
    db.open_event(
        conn,
        metric="co2",
        tier="ceiling",
        opened_at=NOW - timedelta(hours=1),
        value=1300.0,
        baseline=500.0,
        threshold=1200.0,
        notified=True,
    )
    seed(conn, [2700] * 4)
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)
    _, message, _ = notifier.sent[0]
    assert "peak 2700" in message  # must reflect the poll that escalated


def test_open_records_notified_value(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    check_metrics(conn, FakeNotifier(), now=NOW)
    assert db.get_open_events(conn)["co2"]["notified_value"] == 1350.0


def test_renotify_resets_escalation_reference(conn):
    db.open_event(
        conn,
        metric="co2",
        tier="ceiling",
        opened_at=NOW - timedelta(hours=13),
        value=900.0,
        baseline=500.0,
        threshold=1200.0,
        notified=True,
    )
    seed(conn, [1300] * 40)
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)

    event = db.get_open_events(conn)["co2"]
    assert event["notified_value"] == 1300.0  # future doubling measured from here
    assert len(notifier.sent) == 1  # the 12h still-elevated reminder


# --- device health: unreachable and stale ---


def test_device_unreachable_after_10_errors_then_recovery():
    health = DeviceHealth(threshold=10)
    decisions = [health.observe("error") for _ in range(10)]
    assert decisions[:9] == [None] * 9
    assert decisions[9] == "unreachable"
    assert health.observe("error") is None  # already alerted, no spam
    assert health.observe("inserted") == "recovered"


def test_device_stale_after_10_duplicates():
    health = DeviceHealth(threshold=10)
    decisions = [health.observe("duplicate") for _ in range(10)]
    assert decisions[9] == "stale"
    assert health.observe("inserted") == "recovered"


def test_mixed_statuses_do_not_trip():
    health = DeviceHealth(threshold=10)
    for _ in range(20):  # alternating: never 10 consecutive of one kind
        assert health.observe("error") is None
        assert health.observe("inserted") is None


# --- notification value formatting under TEMPERATURE_UNIT ---


def test_notification_format_non_temp_metric_ignores_unit():
    from awair.monitor import _fmt

    assert _fmt("co2", 1400.0, "F") == "1400"
    assert _fmt("co2", 1400.0, "C") == "1400"


def test_notification_format_temp_converts_and_suffixes():
    from awair.monitor import _fmt

    assert _fmt("temp", 22.5, "C") == "22.5°C"
    assert _fmt("temp", 22.5, "F") == "72.5°F"
    assert _fmt("temp", 0.0, "K") == "273.15K"
