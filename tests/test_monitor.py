"""Monitor glue: readings → detection → alert_events rows → notifications.

Uses ceiling-tier shapes (active even in cold start) to keep seeded
histories small; tier-1 shapes are covered in test_spikes.py.
"""

from datetime import UTC, datetime, timedelta

import pytest

from awair import db
from awair.monitor import DeviceHealth, check_metrics
from tests._helpers import FakeNotifier


@pytest.fixture(autouse=True)
def default_celsius(monkeypatch):
    """Isolate each test from any inherited TEMPERATURE_UNIT override."""
    monkeypatch.delenv("TEMPERATURE_UNIT", raising=False)


NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


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


# --- the exact ntfy payload, per action (#57) ---
#
# Every other assertion in this file is deliberately loose ("co2" in
# message.lower()). That is fine for detection logic, but it means the string
# Tom actually reads on his phone was invisible to the suite: a code review of
# the #57 refactor mutated away the baseline/threshold clause, the "back to"
# wording, the "(peak ...)" clause and all three titles, and the suite stayed
# green on every one. These four tests pin the payload; the loose ones above
# stay as they are, because they are asserting *which* decision fired.


def test_open_sends_the_exact_spike_payload(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)
    assert notifier.sent == [
        ("CO2 spike", "CO2 at 1350 (baseline 500, threshold 1200)", "high")
    ]


def test_close_sends_the_exact_cleared_payload(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    notifier = FakeNotifier()
    check_metrics(conn, notifier, now=NOW)
    later = NOW + timedelta(minutes=15)
    seed(conn, [500] * 25, end=later)
    check_metrics(conn, notifier, now=later)
    assert notifier.sent[1] == ("CO2 cleared", "CO2 back to 500", "default")


def test_escalate_on_tier_promotion_names_the_ceiling_crossed(conn):
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
    assert notifier.sent == [
        ("CO2 escalating", "CO2 at 1350 — crossed the 1200 ceiling", "high")
    ]


def test_escalate_within_a_tier_reports_the_doubling_and_peak(conn):
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
    assert notifier.sent == [
        (
            "CO2 escalating",
            "CO2 at 2700 — doubled since last notice (peak 2700)",
            "high",
        )
    ]


def test_renotify_sends_the_exact_still_elevated_payload(conn):
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
    assert notifier.sent == [
        ("CO2 still elevated", "CO2 still elevated at 1300 (peak 1300)", "default")
    ]


class SilentlyFailingNotifier:
    """A Notifier whose send() reports failure — ntfy down, or a 5xx.

    `notified` on the event row is how the poller records whether the human was
    actually told, so it must come from the notifier's return value and not be
    assumed True.
    """

    def send(self, message, title="", priority="default"):
        return False


def _notified_flags(conn, event_id):
    return conn.execute(
        "SELECT open_notified, close_notified FROM alert_events WHERE id = ?",
        (event_id,),
    ).fetchone()


def test_open_records_whether_the_notification_actually_landed(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    check_metrics(conn, FakeNotifier(), now=NOW)
    assert _notified_flags(conn, db.get_open_events(conn)["co2"]["id"])[0] == 1

    conn.execute("DELETE FROM alert_events")
    conn.commit()
    check_metrics(conn, SilentlyFailingNotifier(), now=NOW)
    assert _notified_flags(conn, db.get_open_events(conn)["co2"]["id"])[0] == 0


def test_close_records_whether_the_notification_actually_landed(conn):
    seed(conn, [500, 500, 500, 1300, 1350])
    check_metrics(conn, FakeNotifier(), now=NOW)
    event_id = db.get_open_events(conn)["co2"]["id"]
    later = NOW + timedelta(minutes=15)
    seed(conn, [500] * 25, end=later)
    check_metrics(conn, SilentlyFailingNotifier(), now=later)
    assert _notified_flags(conn, event_id)[1] == 0


def test_notification_titles_upcase_the_metric():
    """Nothing pinned this: every other label assertion normalises with .lower().

    Found by mutating `_Notice.label` to drop `.upper()` — the whole suite stayed
    green while every ntfy title changed from "CO2 spike" to "co2 spike".
    """
    from awair.monitor import _Notice

    notice = _Notice(
        conn=None,
        notifier=None,
        name="co2",
        decision=None,
        event=None,
        now=NOW,
        temp_unit="C",
    )
    assert notice.label == "CO2"


def test_notice_formats_values_in_its_own_temp_unit():
    """`temp` has no MetricConfig yet, so this plumbing has no end-to-end path.

    `METRICS` is co2/voc/pm25, which means `_Notice.fmt` never reaches `_fmt`'s
    temperature branch through `check_metrics` today — a mutation hardcoding the
    unit to "C" survived the whole suite. The threading is still correct and is
    what a future `temp` metric will ride on, so pin it at the unit level.
    """
    from awair.monitor import _Notice

    def notice_for(temp_unit):
        return _Notice(
            conn=None,
            notifier=None,
            name="temp",
            decision=None,
            event=None,
            now=NOW,
            temp_unit=temp_unit,
        )

    assert notice_for("C").fmt(22.5) == "22.5°C"
    assert notice_for("F").fmt(22.5) == "72.5°F"


# --- decision dispatch (#57) ---


def test_every_decision_action_spikes_can_emit_has_a_handler():
    """The docstring on `Decision.action` is the contract; pin it to _ACTIONS.

    `Decision.action` is a bare `str` whose permitted values live only in a
    comment, so a new action can ship without a handler and reach production as
    silence. This test makes the comment executable.
    """
    import inspect
    import re

    from awair.monitor import _ACTIONS
    from awair.spikes import Decision

    source = inspect.getsource(Decision)
    match = re.search(r"action: str\s*#\s*(.+)", source)
    assert match, "Decision.action lost its trailing comment — is it a Literal now?"
    assert set(re.split(r"\s*\|\s*", match.group(1).strip())) == set(_ACTIONS)


def test_unhandled_decision_action_warns_instead_of_vanishing(
    conn, monkeypatch, caplog
):
    """An action with no handler is still a no-op, but a loud one.

    The elif chain this replaced dropped it in silence — the worst outcome on
    an alerting path, because a decision was made and nobody hears it.
    """
    from awair import monitor
    from awair.spikes import Decision

    seed(conn, [500, 500, 500, 1300, 1350])
    monkeypatch.setattr(
        monitor,
        "evaluate",
        lambda cfg, history, event, now: (
            Decision(action="teleport", tier="ceiling", value=1400.0)
            if cfg.name == "co2"
            else None
        ),
    )
    notifier = FakeNotifier()
    caplog.set_level("WARNING", logger="awair.monitor")

    check_metrics(conn, notifier, now=NOW)

    assert notifier.sent == []
    assert db.get_open_events(conn) == {}
    warnings = [r for r in caplog.records if "no handler" in r.message]
    assert len(warnings) == 1
    assert "teleport" in warnings[0].message
