"""The outdoor poller's positive health signal (#94).

Two of these tests are regression pins for defects the *design* avoids rather
than for defects the code once had, and they are written that way on purpose:
both were reproduced against the pre-existing machinery before any of this was
written, and both come back the moment someone "simplifies" this into a reuse
of the indoor poller's tracker.
"""

from datetime import UTC, datetime

import pytest

from awair import db
from awair.monitor import DeviceHealth, OutdoorHealth
from awair.outdoor import _health_window, handle_outdoor_health


class _RecordingNotifier:
    def __init__(self, return_value=True):
        self.calls = []
        self.return_value = return_value

    def send(self, message, title="", priority="default"):
        self.calls.append({"message": message, "title": title, "priority": priority})
        return self.return_value


def _now():
    return datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The tracker
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "tier"),
    [("error", "unreachable"), ("partial", "degraded"), ("duplicate", "stale")],
)
def test_a_sustained_run_of_each_failure_status_opens_its_own_tier(status, tier):
    """Each of the three failure statuses reaches threshold on its own count."""
    health = OutdoorHealth(threshold=3)
    assert [health.observe(status) for _ in range(2)] == [None, None]
    assert health.observe(status) == tier


def test_partial_is_not_health_which_is_the_whole_reason_this_class_exists():
    """A sustained `"partial"` must alert. `DeviceHealth` never does.

    This is the #94 finding. `poll_once` returns `"partial"` when the weather
    half was written and the air-quality half was not, so a row *is* inserted
    and `received_at` advances — the hub's staleness rule reads the poller as
    healthy. `DeviceHealth.observe` reaches its healthy branch through a bare
    `else`, so `"partial"` lands there.

    Asserting the contrast rather than only the new behaviour: a test that
    checked `OutdoorHealth` alone would still pass if someone swapped the call
    site back to `DeviceHealth` and raised its threshold.
    """
    outdoor = OutdoorHealth(threshold=4)
    verdicts = [outdoor.observe("partial") for _ in range(4)]
    assert verdicts == [None, None, None, "degraded"]

    indoor = DeviceHealth(threshold=4)
    assert [indoor.observe("partial") for _ in range(20)] == [None] * 20


def test_a_partial_does_not_reset_a_run_of_errors_the_way_an_insert_does():
    """Alternating error/partial reached no threshold under `DeviceHealth`.

    Each `"partial"` zeroed the error run, so the two failure modes interleaved
    could persist forever without a word. Here the two runs are counted
    independently, so neither masks the other.
    """
    indoor = DeviceHealth(threshold=3)
    interleaved = ["error", "error", "partial", "error", "error", "partial"]
    assert [indoor.observe(s) for s in interleaved] == [None] * 6

    outdoor = OutdoorHealth(threshold=3)
    assert [outdoor.observe(s) for s in interleaved] == [None] * 6
    # ...and the error run resumes from zero rather than being lost entirely.
    assert [outdoor.observe("error") for _ in range(3)] == [None, None, "unreachable"]


def test_only_an_insert_is_recovery_and_a_partial_is_not():
    """`"partial"` after an alert must not clear it — `DeviceHealth` says it does."""
    indoor = DeviceHealth(threshold=1)
    indoor.observe("error")
    assert indoor.observe("partial") == "recovered"

    outdoor = OutdoorHealth(threshold=1)
    assert outdoor.observe("error") == "unreachable"
    assert outdoor.observe("partial") is None
    assert outdoor.observe("inserted") == "recovered"


def test_an_unrecognised_status_is_ignored_rather_than_read_as_health():
    """Fail closed: a future fifth status must be classified, not assumed good.

    The fail-open `else` is the bug this class exists to avoid, so an unknown
    status must not silently clear an open alert.
    """
    health = OutdoorHealth(threshold=1)
    assert health.observe("error") == "unreachable"
    assert health.observe("something-new") is None
    assert health.alerted == "unreachable"
    assert health.observe("inserted") == "recovered"


def test_one_tier_alerts_at_a_time_and_does_not_realert_until_recovery():
    """`alerted` latches, so a continuing outage does not renotify every poll."""
    health = OutdoorHealth(threshold=2)
    health.observe("error")
    assert health.observe("error") == "unreachable"
    assert [health.observe("error") for _ in range(10)] == [None] * 10


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------


def test_below_threshold_writes_nothing_and_notifies_nothing(conn):
    notifier = _RecordingNotifier()
    handle_outdoor_health(conn, notifier, OutdoorHealth(threshold=4), "error", _now())
    assert notifier.calls == []
    assert db.get_open_events(conn) == {}


def test_the_event_is_keyed_outdoor_not_device(conn):
    """The metric name is the fix, not a label.

    `db.get_open_events` returns at most one open event per metric and the two
    pollers share a database. Under `metric="device"` an outdoor recovery closes
    the *indoor* poller's row and sends a false all-clear — reproduced on #94.
    """
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now())
    assert set(db.get_open_events(conn)) == {"outdoor"}


def test_an_outdoor_alert_leaves_an_open_indoor_event_alone(conn):
    """The collision, driven end to end rather than argued."""
    notifier = _RecordingNotifier()
    db.open_event(
        conn,
        metric="device",
        tier="unreachable",
        opened_at=_now(),
        value=None,
        baseline=None,
        threshold=None,
        notified=True,
    )
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now())
    handle_outdoor_health(conn, notifier, health, "inserted", _now())

    still_open = db.get_open_events(conn)
    assert "device" in still_open, "an outdoor recovery closed the indoor event"
    assert still_open["device"]["tier"] == "unreachable"
    assert "outdoor" not in still_open


@pytest.mark.parametrize(
    ("status", "tier", "priority"),
    [
        ("error", "unreachable", "high"),
        ("partial", "degraded", "default"),
        ("duplicate", "stale", "default"),
    ],
)
def test_only_the_actionable_tier_pages(conn, status, tier, priority):
    """`unreachable` is ours to fix; the other two are Open-Meteo's."""
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, status, _now())
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["priority"] == priority
    assert notifier.calls[0]["title"] == f"Outdoor {tier}"
    assert db.get_open_events(conn)["outdoor"]["tier"] == tier


def test_recovery_closes_the_row_and_says_so(conn):
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now())
    handle_outdoor_health(conn, notifier, health, "inserted", _now())
    assert [c["title"] for c in notifier.calls] == [
        "Outdoor unreachable",
        "Outdoor recovered",
    ]
    assert "outdoor" not in db.get_open_events(conn)


def test_recovery_without_a_stored_row_still_notifies(conn):
    """The tracker is the source of truth; a pruned DB must not swallow it."""
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now())
    conn.execute("DELETE FROM alert_events")
    conn.commit()
    handle_outdoor_health(conn, notifier, health, "inserted", _now())
    assert notifier.calls[-1]["title"] == "Outdoor recovered"


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


def test_the_message_states_wall_clock_not_a_poll_count(monkeypatch):
    """4 polls means 5 minutes indoors and an hour outdoors — say which."""
    monkeypatch.delenv("AWAIR_OUTDOOR_POLL_SECONDS", raising=False)
    assert _health_window(OutdoorHealth(threshold=4)) == "1h"


def test_the_window_follows_a_configured_interval(monkeypatch):
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "60")
    assert _health_window(OutdoorHealth(threshold=5)) == "5 min"


def test_the_notification_carries_the_window(conn, monkeypatch):
    monkeypatch.delenv("AWAIR_OUTDOOR_POLL_SECONDS", raising=False)
    notifier = _RecordingNotifier()
    handle_outdoor_health(conn, notifier, OutdoorHealth(threshold=4), "error", _now())
    assert notifier.calls == []
    health = OutdoorHealth(threshold=4)
    for _ in range(4):
        handle_outdoor_health(conn, notifier, health, "error", _now())
    assert "~1h of polls" in notifier.calls[0]["message"]
