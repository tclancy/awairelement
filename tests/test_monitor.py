"""Monitor glue: readings → detection → alert_events rows → notifications.

Uses ceiling-tier shapes (active even in cold start) to keep seeded
histories small; tier-1 shapes are covered in test_spikes.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db
from awair.monitor import DeviceHealth, check_metrics

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, message, title="", priority="default"):
        self.sent.append((title, message, priority))
        return True


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


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
