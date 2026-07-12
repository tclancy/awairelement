"""Fan mitigation: verdict, rate limit, actuation, and the check_fans glue.

Each scenario maps to a rule in issue #10 / #14. Trigger surface is
`spikes` open events; suppressor is a raw pm25 read.
"""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db, fans
from awair.fans import (
    FansConfig,
    MitigationDecision,
    actuate,
    check_fans,
    decide,
    desired_action,
    events_to_engage,
)
from tests._helpers import FakeNotifier, fake_url_opener

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)

# Below FAN_SCORE_GATE — the score at which an event is worth spending fans on.
BAD_SCORE = 70
GOOD_SCORE = 84


def _event(metric, tier="relative", fans_engaged=1, event_id=1):
    """An open event. Latched by default — most fan rules predate the gate."""
    return {
        "metric": metric,
        "tier": tier,
        "id": event_id,
        "fans_engaged": fans_engaged,
    }


# --- desired_action: spike-event tiers → fan speed ---


def test_no_events_and_clean_air_is_off():
    action, reason = desired_action({}, latest_pm25=5.0)
    assert action == "off"
    assert "no co2/voc spike" in reason


def test_single_trigger_relative_yields_speed1():
    action, reason = desired_action({"co2": _event("co2")}, latest_pm25=5.0)
    assert action == "speed1"
    assert "co2" in reason


def test_both_triggers_relative_yield_speed2():
    action, _ = desired_action(
        {"co2": _event("co2"), "voc": _event("voc")}, latest_pm25=5.0
    )
    assert action == "speed2"


def test_both_triggers_with_any_ceiling_yield_speed3():
    action, _ = desired_action(
        {"co2": _event("co2", tier="ceiling"), "voc": _event("voc")},
        latest_pm25=5.0,
    )
    assert action == "speed3"


def test_pm25_suppressor_overrides_active_events():
    # Fan re-suspends particulate — even with co2 spiking, pm25>=25 wins.
    action, reason = desired_action(
        {"co2": _event("co2", tier="ceiling")}, latest_pm25=30.0
    )
    assert action == "off"
    assert "pm25" in reason


def test_pm25_at_threshold_boundary_suppresses():
    action, _ = desired_action({"co2": _event("co2")}, latest_pm25=25.0)
    assert action == "off"


def test_pm25_just_below_threshold_does_not_suppress():
    action, _ = desired_action({"co2": _event("co2")}, latest_pm25=24.9)
    assert action == "speed1"


def test_missing_pm25_never_suppresses():
    # Sensor null / cold-boot: don't hallucinate a suppression.
    action, _ = desired_action({"co2": _event("co2")}, latest_pm25=None)
    assert action == "speed1"


def test_device_metric_events_do_not_trigger_fans():
    # `device` unreachable/stale events must not be misread as air quality.
    action, _ = desired_action({"device": _event("device", tier="unreachable")}, 5.0)
    assert action == "off"


def test_pm25_metric_event_does_not_trigger_fans():
    # PM25 spikes must not turn fans on (still a suppressor at raw threshold).
    action, _ = desired_action({"pm25": _event("pm25", tier="ceiling")}, 5.0)
    assert action == "off"


# --- desired_action: the score gate (only latched events drive fans) ---


def test_unlatched_event_does_not_drive_fans():
    # A voc spike the score never agreed with: TVOC is elevated but the air is
    # fine overall. This is the "closed the windows, fans came on" complaint.
    action, reason = desired_action(
        {"voc": _event("voc", tier="ceiling", fans_engaged=0)}, latest_pm25=5.0
    )
    assert action == "off"
    assert "no co2/voc spike" in reason


def test_only_latched_events_count_toward_speed():
    # co2 latched, voc not: one effective trigger, so speed1 — not speed2.
    action, reason = desired_action(
        {
            "co2": _event("co2", fans_engaged=1),
            "voc": _event("voc", fans_engaged=0),
        },
        latest_pm25=5.0,
    )
    assert action == "speed1"
    assert "co2" in reason
    assert "voc" not in reason


def test_pm25_suppression_still_beats_a_latched_event():
    # The latch is a relevance gate, not an override of the safety suppressor.
    action, reason = desired_action(
        {"voc": _event("voc", tier="ceiling", fans_engaged=1)}, latest_pm25=30.0
    )
    assert action == "off"
    assert "pm25" in reason


# --- events_to_engage: which open events latch on this poll ---


def test_score_below_gate_engages_an_open_trigger():
    open_events = {"voc": _event("voc", fans_engaged=0, event_id=7)}
    assert events_to_engage(open_events, latest_score=BAD_SCORE) == [7]


def test_score_above_gate_engages_nothing():
    open_events = {"voc": _event("voc", fans_engaged=0, event_id=7)}
    assert events_to_engage(open_events, latest_score=GOOD_SCORE) == []


def test_score_exactly_at_gate_does_not_engage():
    # Gate is a strict "drops below 75" — 75 itself is still acceptable air.
    open_events = {"voc": _event("voc", fans_engaged=0, event_id=7)}
    assert events_to_engage(open_events, latest_score=fans.FAN_SCORE_GATE) == []


def test_missing_score_engages_nothing():
    # Absent/stale data means don't act. Never hallucinate a bad score.
    open_events = {"voc": _event("voc", fans_engaged=0, event_id=7)}
    assert events_to_engage(open_events, latest_score=None) == []


def test_already_latched_event_is_not_re_engaged():
    # Idempotence: the latch is written once, not re-stamped every poll.
    open_events = {"voc": _event("voc", fans_engaged=1, event_id=7)}
    assert events_to_engage(open_events, latest_score=BAD_SCORE) == []


def test_non_fan_trigger_events_never_engage():
    # A pm25 or device event must not latch — they aren't fan triggers.
    open_events = {
        "pm25": _event("pm25", fans_engaged=0, event_id=7),
        "device": _event("device", fans_engaged=0, event_id=8),
    }
    assert events_to_engage(open_events, latest_score=BAD_SCORE) == []


def test_multiple_open_triggers_engage_together():
    open_events = {
        "co2": _event("co2", fans_engaged=0, event_id=3),
        "voc": _event("voc", fans_engaged=0, event_id=4),
    }
    assert sorted(events_to_engage(open_events, latest_score=BAD_SCORE)) == [3, 4]


# --- decide: no-op filter + 1-cmd/min per-fan rate limit ---


def _state(action="off", last_cmd_seconds_ago=3600):
    return {
        "fan_id": 1,
        "last_action": action,
        "last_command_at": NOW - timedelta(seconds=last_cmd_seconds_ago),
    }


def test_same_action_is_noop():
    assert decide(1, "off", "no spike", _state("off"), NOW) is None


def test_state_change_within_rate_limit_is_skipped():
    # 30s < 60s: last command still in cooldown.
    assert (
        decide(1, "speed1", "co2 spike", _state("off", last_cmd_seconds_ago=30), NOW)
        is None
    )


def test_state_change_outside_rate_limit_is_allowed():
    d = decide(1, "speed1", "co2 spike", _state("off", last_cmd_seconds_ago=90), NOW)
    assert d == MitigationDecision(fan_id=1, action="speed1", reason="co2 spike")


def test_rate_limit_at_exact_boundary_allows():
    # Exactly 60s ago: RATE_LIMIT is not strictly-less, so this fires.
    d = decide(1, "speed1", "co2 spike", _state("off", last_cmd_seconds_ago=60), NOW)
    assert d is not None


def test_fresh_fan_state_never_blocks():
    # A never-set fan state has last_command_at at the 1970 sentinel — must not
    # rate-limit the first-ever command.
    state = {
        "fan_id": 1,
        "last_action": "off",
        "last_command_at": datetime(1970, 1, 1, tzinfo=timezone.utc),
    }
    d = decide(1, "speed1", "co2 spike", state, NOW)
    assert d is not None


def test_pm25_safety_off_bypasses_rate_limit():
    # Fans were just kicked to speed3 for a co2 spike; 20s later pm25 crosses
    # the suppressor threshold. Waiting the rest of the 60s to turn them off
    # would keep them stirring particulate — the safety-off must fire now.
    reason = "pm25 40 suppresses fans"
    d = decide(1, "off", reason, _state("speed3", last_cmd_seconds_ago=20), NOW)
    assert d is not None
    assert d.action == "off"


def test_non_pm25_off_still_respects_rate_limit():
    # Ordinary "spike closed → off" transitions are not safety-critical; they
    # still respect the rate limit.
    d = decide(
        1, "off", "no co2/voc spike", _state("speed1", last_cmd_seconds_ago=20), NOW
    )
    assert d is None


# --- actuate: fire-and-forget urllib GET ---


def test_actuate_hits_the_fan_endpoint():
    calls = []
    ok = actuate(
        MitigationDecision(fan_id=2, action="speed1", reason="voc"),
        FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2)),
        opener=fake_url_opener(calls),
    )
    assert ok is True
    assert calls == [("http://host.local/fan/2/speed1", fans.FAN_CMD_TIMEOUT_SECONDS)]


def test_actuate_failure_returns_false():
    def broken(url, timeout):
        raise OSError("connection refused")

    ok = actuate(
        MitigationDecision(fan_id=1, action="off", reason="pm25"),
        FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2)),
        opener=broken,
    )
    assert ok is False


# --- check_fans: end-to-end glue over the DB ---


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def _seed_reading(conn, pm25, ts=NOW, score=BAD_SCORE):
    """One reading. Score defaults BELOW the gate so fans are free to engage —
    tests that care about the gate pass GOOD_SCORE explicitly."""
    ts_iso = db.iso_z(ts)
    conn.execute(
        "INSERT INTO readings (ts, received_at, score, co2, voc, pm25)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts_iso, ts_iso, score, 500, 100, pm25),
    )
    conn.commit()


def _seed_event(conn, metric, tier="relative"):
    return db.open_event(
        conn,
        metric=metric,
        tier=tier,
        opened_at=NOW - timedelta(minutes=5),
        value=1500.0,
        baseline=500.0,
        threshold=800.0,
        notified=True,
    )


def test_check_fans_no_op_when_disabled(conn):
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1, 2))
    check_fans(conn, notifier, cfg, NOW)
    assert notifier.sent == []
    assert conn.execute("SELECT COUNT(*) FROM fan_state").fetchone()[0] == 0


def test_check_fans_drives_both_fans_on_co2_ceiling(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2))
    _seed_reading(conn, pm25=5.0)
    _seed_event(conn, "co2", tier="ceiling")
    check_fans(conn, notifier, cfg, NOW)

    # One event open only (co2) => speed1 on both fans.
    urls = [url for url, _ in calls]
    assert urls == ["http://host.local/fan/1/speed1", "http://host.local/fan/2/speed1"]
    assert len(notifier.sent) == 2
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"
    assert db.get_fan_state(conn, 2)["last_action"] == "speed1"


def test_check_fans_forces_off_when_pm25_suppresses(conn, monkeypatch):
    """PM2.5 suppressor overrides a prior speed1 the poller set itself."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    # Seed: fan 1 already at speed1 from an earlier tick, 5 min ago.
    db.upsert_fan_state(
        conn,
        fan_id=1,
        action="speed1",
        command_at=NOW - timedelta(minutes=5),
    )
    _seed_reading(conn, pm25=30.0)
    _seed_event(conn, "co2", tier="ceiling")  # would drive speed3 without pm25
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    notifier = FakeNotifier()
    check_fans(conn, notifier, cfg, NOW)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_check_fans_holds_when_rate_limited(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    # Fan 1 changed 30s ago — inside the 60s cooldown.
    db.upsert_fan_state(
        conn,
        fan_id=1,
        action="off",
        command_at=NOW - timedelta(seconds=30),
    )
    _seed_reading(conn, pm25=5.0)
    _seed_event(conn, "co2", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    notifier = FakeNotifier()
    check_fans(conn, notifier, cfg, NOW)

    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_check_fans_actuate_failure_does_not_advance_last_action(conn, monkeypatch):
    """A transient NodeMCU failure must not desync the DB from physical state.

    Without the guard, next tick sees state.last_action == desired and skips
    the retry entirely; the fan stays physically off while the DB claims on.
    """

    def broken(url, timeout):
        raise OSError("boom")

    monkeypatch.setattr("urllib.request.urlopen", broken)
    _seed_reading(conn, pm25=5.0)
    _seed_event(conn, "co2", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    notifier = FakeNotifier()
    check_fans(conn, notifier, cfg, NOW)

    # No user-visible notification when nothing physical changed.
    assert notifier.sent == []
    # last_action stays "off" (the pre-existing state), NOT "speed1".
    state = db.get_fan_state(conn, 1)
    assert state["last_action"] == "off"
    # But last_command_at IS stamped so the rate limit gates the retry to
    # 1 attempt / RATE_LIMIT — a broken NodeMCU is not spammed every poll.
    assert state["last_command_at"] == NOW


# --- check_fans: the score gate + latch, end to end ---


def test_check_fans_ignores_a_spike_the_score_disagrees_with(conn, monkeypatch):
    """The bug report: TVOC ceiling breached, but overall air is fine (score 84).

    Closing the windows nudges TVOC up without meaningfully degrading air
    quality. No fan should move, and no latch should be written.
    """
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    _seed_reading(conn, pm25=5.0, score=GOOD_SCORE)
    _seed_event(conn, "voc", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "off"
    assert db.get_open_events(conn)["voc"]["fans_engaged"] == 0


def test_check_fans_engages_and_persists_the_latch(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    _seed_reading(conn, pm25=5.0, score=BAD_SCORE)
    _seed_event(conn, "voc", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/speed1"]
    assert db.get_open_events(conn)["voc"]["fans_engaged"] == 1


def test_latched_event_keeps_fans_on_after_the_score_recovers(conn, monkeypatch):
    """The whole point of the latch.

    The score lives astride the gate (p1=73, p5=76 in real data). Once we've
    committed to running the fans for an event, a score bobbing back over 75
    must NOT turn them off — that oscillation is what would have Tom fighting
    the fans.
    """
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))

    # Poll 1: score dips, event latches, fan spins up.
    _seed_reading(conn, pm25=5.0, score=BAD_SCORE)
    _seed_event(conn, "voc", tier="ceiling")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"

    # Poll 2, two minutes later (past the rate limit): score has recovered to 84,
    # but the event is still open and still latched.
    calls.clear()
    later = NOW + timedelta(minutes=2)
    _seed_reading(conn, pm25=5.0, ts=later, score=GOOD_SCORE)
    check_fans(conn, FakeNotifier(), cfg, later)

    # No new command at all: desired is still speed1, so decide() no-ops.
    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"


def test_score_gate_does_not_block_the_pm25_safety_off(conn, monkeypatch):
    """A good score must never strand the fans ON during a particulate spike."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(minutes=5)
    )
    # Score is fine, pm25 is not. Suppressor must still force the fan off.
    _seed_reading(conn, pm25=30.0, score=GOOD_SCORE)
    _seed_event(conn, "voc", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_stale_score_does_not_engage_fans(conn, monkeypatch):
    """A score older than SCORE_FRESHNESS reads as no data — don't act on it."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    # Bad score, but from an hour ago — well outside the freshness window.
    _seed_reading(conn, pm25=5.0, ts=NOW - timedelta(hours=1), score=BAD_SCORE)
    _seed_event(conn, "voc", tier="ceiling")
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert calls == []
    assert db.get_open_events(conn)["voc"]["fans_engaged"] == 0


# --- config: env parsing ---


def test_config_from_env_defaults_off(monkeypatch):
    monkeypatch.delenv("AWAIR_FAN_MITIGATION_ENABLED", raising=False)
    monkeypatch.delenv("AWAIR_FAN_HOST", raising=False)
    cfg = fans.config_from_env()
    assert cfg.enabled is False
    assert cfg.fan_host == fans.DEFAULT_FAN_HOST
    assert cfg.fan_ids == (1, 2)


def test_config_from_env_reads_toggles(monkeypatch):
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    monkeypatch.setenv("AWAIR_FAN_HOST", "10.0.0.10")
    cfg = fans.config_from_env()
    assert cfg.enabled is True
    assert cfg.fan_host == "10.0.0.10"


def test_config_from_env_enabled_is_strict(monkeypatch):
    # Anything other than the literal "true" (case-insensitive) is off — a
    # partial rename (e.g. "on") must never accidentally activate fans.
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "on")
    assert fans.config_from_env().enabled is False


# --- run_fan_test: the poller's manual --test smoke switch ---


def test_run_fan_test_actuates_all_fans_and_notifies(conn):
    calls = []
    notifier = FakeNotifier()
    config = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2))
    fans.run_fan_test(conn, notifier, config, NOW, opener=fake_url_opener(calls))

    assert [url for url, _ in calls] == [
        "http://host.local/fan/1/speed1",
        "http://host.local/fan/2/speed1",
    ]
    assert notifier.sent == [("", "Fan test", "default")]
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"
    assert db.get_fan_state(conn, 2)["last_action"] == "speed1"


def test_run_fan_test_ignores_enabled_flag(conn):
    # Proving the plumbing works is exactly what you do BEFORE flipping
    # mitigation on, so --test must not be gated on enabled.
    calls = []
    config = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1,))
    fans.run_fan_test(conn, FakeNotifier(), config, NOW, opener=fake_url_opener(calls))
    assert calls


def test_run_fan_test_does_not_record_state_on_actuate_failure(conn):
    def broken(url, timeout):
        raise OSError("connection refused")

    notifier = FakeNotifier()
    config = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    fans.run_fan_test(conn, notifier, config, NOW, opener=broken)

    assert db.get_fan_state(conn, 1)["last_action"] == "off"  # DB truth preserved
    assert notifier.sent  # the ntfy half still runs
