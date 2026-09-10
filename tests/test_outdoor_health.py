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


def test_an_interleaved_failure_stream_alerts_rather_than_masking_itself():
    """A mixed run of failures is one outage and must trip the threshold.

    This is the case that made the first draft wrong, and the reason the tracker
    counts *non-inserting polls* rather than a run per status. `poll_once`
    returns `"duplicate"` before it returns `"partial"`, so a broken AQ endpoint
    plus a weather half republishing a stale `current.time` emits an alternating
    stream — and a per-status counter zeroes each run with every switch. Under
    that draft sixty consecutive useless polls, fifteen hours, alerted zero
    times.

    Asserting against `DeviceHealth` too, because the contrast is the point: the
    indoor tracker still says nothing, which is exactly what must not happen
    here.
    """
    interleaved = ["error", "partial", "duplicate"] * 20

    indoor = DeviceHealth(threshold=3)
    assert [indoor.observe(s) for s in interleaved] == [None] * 60

    outdoor = OutdoorHealth(threshold=3)
    verdicts = [outdoor.observe(s) for s in interleaved]
    assert verdicts[:3] == [None, None, "stale"], "a mixed run must reach threshold"
    assert verdicts.count("stale") == 1, "the latch must hold for the rest"


def test_a_failure_run_survives_a_switch_of_failure_status():
    """Two errors then a partial is three bad polls, not one."""
    health = OutdoorHealth(threshold=3)
    assert health.observe("error") is None
    assert health.observe("error") is None
    assert health.observe("partial") == "degraded"


def test_the_tier_named_is_the_most_recent_failing_status():
    """A mixed run is still one outage; report what is happening now."""
    health = OutdoorHealth(threshold=4)
    for status in ("error", "error", "error"):
        assert health.observe(status) is None
    assert health.observe("duplicate") == "stale"


def test_only_an_insert_clears_the_run():
    """The run resets on health and on nothing else."""
    health = OutdoorHealth(threshold=3)
    health.observe("error")
    health.observe("partial")
    assert health.observe("inserted") is None  # nothing was alerted yet
    assert health.unhealthy == 0
    assert [health.observe("error") for _ in range(3)] == [None, None, "unreachable"]


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
    # With nothing latched yet — the arm that matters. A first draft only
    # checked the post-alert case, where the latch short-circuits before the
    # status is ever looked up, so deleting the guard survived mutation.
    fresh = OutdoorHealth(threshold=2)
    assert fresh.observe("something-new") is None
    assert fresh.observe("something-new") is None
    assert fresh.unhealthy == 0, "an unclassified status advanced the run"
    assert fresh.alerted is None

    # ...and it must not clear an alert that is already open either.
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
    handle_outdoor_health(
        conn, notifier, OutdoorHealth(threshold=4), "error", _now(), 900
    )
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
    handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
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
    handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
    handle_outdoor_health(conn, notifier, health, "inserted", _now(), 900)

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
    handle_outdoor_health(conn, notifier, health, status, _now(), 900)
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["priority"] == priority
    assert notifier.calls[0]["title"] == f"Outdoor {tier}"
    assert db.get_open_events(conn)["outdoor"]["tier"] == tier


def test_recovery_closes_the_row_and_says_so(conn):
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
    handle_outdoor_health(conn, notifier, health, "inserted", _now(), 900)
    assert [c["title"] for c in notifier.calls] == [
        "Outdoor unreachable",
        "Outdoor recovered",
    ]
    assert "outdoor" not in db.get_open_events(conn)


def test_recovery_without_a_stored_row_still_notifies(conn):
    """The tracker is the source of truth; a pruned DB must not swallow it."""
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=1)
    handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
    conn.execute("DELETE FROM alert_events")
    conn.commit()
    handle_outdoor_health(conn, notifier, health, "inserted", _now(), 900)
    assert notifier.calls[-1]["title"] == "Outdoor recovered"


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------


def test_the_message_states_wall_clock_not_a_poll_count():
    """4 polls means 5 minutes indoors and an hour outdoors — say which."""
    assert _health_window(OutdoorHealth(threshold=4), 900) == "1h"


@pytest.mark.parametrize(
    ("threshold", "interval", "expected"),
    [
        (5, 60, "5 min"),
        (4, 100, "7 min"),  # rounds; truncation would say 6
        (2, 10, "20s"),  # under a minute; truncation would say "0 min"
        (2, 1800, "1h"),
    ],
)
def test_the_window_renders_the_configured_cadence(threshold, interval, expected):
    assert _health_window(OutdoorHealth(threshold=threshold), interval) == expected


def test_the_notification_carries_the_window(conn):
    notifier = _RecordingNotifier()
    health = OutdoorHealth(threshold=4)
    for _ in range(3):
        handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
    assert notifier.calls == []
    handle_outdoor_health(conn, notifier, health, "error", _now(), 900)
    assert "~1h of polls" in notifier.calls[0]["message"]


# --------------------------------------------------------------------------
# The wiring
#
# Everything above this line exercises `OutdoorHealth` and
# `handle_outdoor_health` directly. A review round found that every edit
# crossing a module boundary — the call from `main()`, the threshold env var,
# and `web._NON_MEASUREMENT_METRICS` — survived mutation: the whole feature
# could be unwired and all 386 tests stayed green. Coverage said 99% on
# `outdoor.py`, because `main()` is driven by three other tests, so the line
# executed and nothing asserted on it. These close that.
# --------------------------------------------------------------------------


def _run_main_once(monkeypatch, tmp_path, status_payload, env=None):
    """Drive `outdoor.main()` for exactly one poll and hand back the DB path.

    SIGTERM is raised from inside the first fetch, the same seam
    `test_main_logs_an_unusable_payload_at_warning_not_info` uses, so the loop
    runs one iteration and stops. `Notifier` is replaced because `main()` builds
    a live one aimed at the real ntfy host and these tests deliberately push the
    threshold down to 1 — without the seam the first of them would post.
    """
    import os
    import signal

    from awair import outdoor as outdoor_module

    db_path = tmp_path / "out.db"
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", str(db_path))
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "0")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    sent = _RecordingNotifier()
    monkeypatch.setattr(outdoor_module, "Notifier", lambda **kwargs: sent)

    def weather():
        os.kill(os.getpid(), signal.SIGTERM)
        return status_payload

    monkeypatch.setattr(
        outdoor_module,
        "make_fetch",
        lambda url: weather if "air-quality" not in url else (lambda: "{}"),
    )
    outdoor_module.main()
    return db_path, sent


def test_main_actually_calls_the_health_handler(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """Delete the call from `main()` and this is the test that goes red.

    Nothing else asserts the feature is connected — the mutation round proved
    the deletion was silent across the whole suite.
    """
    db_path, sent = _run_main_once(
        monkeypatch,
        tmp_path,
        "not json at all",
        env={"AWAIR_OUTDOOR_HEALTH_POLLS": "1"},
    )
    conn = db.connect(db_path)
    try:
        events = db.get_open_events(conn)
    finally:
        conn.close()
    assert "outdoor" in events, "main() never reached handle_outdoor_health"
    assert events["outdoor"]["tier"] == "unreachable"
    assert [c["title"] for c in sent.calls] == ["Outdoor unreachable"]


def test_main_reads_the_threshold_from_the_environment(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """The same single bad poll must stay quiet at the default threshold.

    Pins `AWAIR_OUTDOOR_HEALTH_POLLS` as *read*, not merely present: hard-coding
    `threshold=4` in `main()` also survived the mutation round, because nothing
    distinguished the configured value from the default.
    """
    db_path, sent = _run_main_once(monkeypatch, tmp_path, "not json at all")
    conn = db.connect(db_path)
    try:
        assert db.get_open_events(conn) == {}
    finally:
        conn.close()
    assert sent.calls == []
