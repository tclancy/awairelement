"""Fan mitigation: verdict, rate limit, actuation, and the check_fans glue.

Each scenario maps to a rule in issue #10 / #14. Trigger surface is
`spikes` open events; suppressor is a raw pm25 read.
"""

from datetime import UTC, datetime, timedelta

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

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)

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


# --- the two predicates desired_action composes (#57) ---


def test_pm25_suppresses_needs_positive_evidence():
    """No reading is not a suppression: the veto needs particulate to point at.

    `check_fans` only ever passes a reading inside PM25_FRESHNESS, so None here
    means "the sensor has told us nothing recent" — which must not silently
    behave like a clean reading *or* like a dirty one. It means don't veto.
    """
    assert fans.pm25_suppresses(None) is False
    assert fans.pm25_suppresses(fans.PM25_SUPPRESS_THRESHOLD - 0.1) is False
    assert fans.pm25_suppresses(fans.PM25_SUPPRESS_THRESHOLD) is True  # inclusive
    assert fans.pm25_suppresses(fans.PM25_SUPPRESS_THRESHOLD + 100) is True


def test_engaged_triggers_returns_only_latched_co2_voc_events():
    """Trigger metrics only, latch set only, and in FAN_TRIGGERS order."""
    active = fans.engaged_triggers(
        {
            "voc": _event("voc", fans_engaged=1),
            "co2": _event("co2", fans_engaged=1),
            "pm25": _event("pm25", fans_engaged=1),  # suppressor, never a trigger
            "temp": _event("temp", fans_engaged=1),  # not a fan metric at all
        }
    )
    assert [e["metric"] for e in active] == ["co2", "voc"]
    assert fans.engaged_triggers({"co2": _event("co2", fans_engaged=0)}) == []
    assert fans.engaged_triggers({}) == []


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
        "last_command_at": datetime(1970, 1, 1, tzinfo=UTC),
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


def test_disabled_poller_touches_nothing_when_no_fan_is_running(conn):
    # Nothing recorded => nothing to release. A disabled poller on a clean DB
    # must stay completely silent rather than spamming an off command.
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1, 2))
    check_fans(conn, notifier, cfg, NOW)
    assert notifier.sent == []
    assert conn.execute("SELECT COUNT(*) FROM fan_state").fetchone()[0] == 0


def test_disabled_poller_releases_fans_it_left_running(conn, monkeypatch):
    # The state on the homelab when #61 was filed: voc-ceiling event 42 open and
    # latched, both fans at speed1 since 15:22 ET, ten hours and counting. Under
    # the old early return, disabling mitigation here stranded them at speed1
    # forever — the complaint the kill switch exists to answer, made permanent.
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1, 2))
    _seed_reading(conn, pm25=5.0)
    _seed_event(conn, "voc", tier="ceiling")
    for fan_id in (1, 2):
        db.upsert_fan_state(
            conn, fan_id=fan_id, action="speed1", command_at=NOW - timedelta(hours=10)
        )

    check_fans(conn, notifier, cfg, NOW)

    assert [url for url, _ in calls] == [
        "http://host.local/fan/1/off",
        "http://host.local/fan/2/off",
    ]
    assert db.get_fan_state(conn, 1)["last_action"] == "off"
    assert db.get_fan_state(conn, 2)["last_action"] == "off"
    assert len(notifier.sent) == 2


def test_release_is_one_shot_not_a_command_every_poll(conn, monkeypatch):
    # After the release the poller must go quiet: a disabled poller that
    # re-sent "off" every 30s would fight a fan switched on at the wall.
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1,))
    db.upsert_fan_state(
        conn, fan_id=1, action="speed2", command_at=NOW - timedelta(hours=1)
    )

    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert len(calls) == 1
    for extra_polls in range(1, 5):
        check_fans(conn, FakeNotifier(), cfg, NOW + timedelta(minutes=extra_polls * 5))
    assert len(calls) == 1


def test_release_keeps_db_truth_and_retries_when_the_nodemcu_is_down(conn, monkeypatch):
    # Same contract as the drive path: a failed command must not be recorded as
    # the fan's current state, and the rate limit doubles as the retry backoff.
    def broken(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", broken)
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1,))
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(hours=1)
    )

    check_fans(conn, notifier, cfg, NOW)
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"  # DB truth preserved
    assert notifier.sent == []

    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    check_fans(conn, notifier, cfg, NOW + timedelta(seconds=30))  # inside RATE_LIMIT
    assert calls == []
    check_fans(conn, notifier, cfg, NOW + timedelta(seconds=61))  # backoff elapsed
    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


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
    # Retirement is lifted for the two parse tests below. With it in force they
    # would pass for free — every input maps to enabled=False — and would stop
    # saying anything about the parse they exist to pin.
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", False)
    monkeypatch.delenv("AWAIR_FAN_MITIGATION_ENABLED", raising=False)
    monkeypatch.delenv("AWAIR_FAN_HOST", raising=False)
    cfg = fans.config_from_env()
    assert cfg.enabled is False
    assert cfg.fan_host == fans.DEFAULT_FAN_HOST
    assert cfg.fan_ids == (1, 2)


def test_config_from_env_reads_fan_host(monkeypatch):
    monkeypatch.setenv("AWAIR_FAN_HOST", "10.0.0.10")
    assert fans.config_from_env().fan_host == "10.0.0.10"


def test_fan_mitigation_ships_retired(monkeypatch):
    """The as-shipped default, asserted through behaviour and not monkeypatched.

    Every other retirement test sets `MITIGATION_RETIRED` explicitly, which
    leaves the value the repo actually ships completely unpinned — a mutation
    round flipping it to False kept the whole suite green. That is the one
    constant standing between #61 and the fans coming back on, so it gets a
    test with nothing patched over it. Un-retiring should have to edit this
    test on purpose, in the same diff, where a reviewer will see it.
    """
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    assert fans.MITIGATION_RETIRED is True
    assert fans.config_from_env().enabled is False


def test_retirement_overrides_the_enable_flag(monkeypatch, caplog):
    # The homelab deploy still ships AWAIR_FAN_MITIGATION_ENABLED=true; while
    # mitigation is retired (#61) that variable must not be able to switch the
    # fans back on, and it must say so rather than being silently dropped.
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    with caplog.at_level("WARNING", logger="awair.fans"):
        assert fans.config_from_env().enabled is False
    assert "retired (#61)" in caplog.text


def test_no_warning_when_the_env_does_not_ask_for_fans(monkeypatch, caplog):
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    monkeypatch.delenv("AWAIR_FAN_MITIGATION_ENABLED", raising=False)
    with caplog.at_level("WARNING", logger="awair.fans"):
        assert fans.config_from_env().enabled is False
    assert caplog.text == ""


def test_lifting_the_retirement_restores_the_enable_flag(monkeypatch):
    # The whole point of retiring in place rather than deleting the module:
    # un-retiring is one constant. If this ever fails, "flip it back" is a lie
    # and the ADR's "reverses if" clause has nothing behind it.
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", False)
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    assert fans.config_from_env().enabled is True


def test_lifting_the_retirement_restores_the_whole_drive_loop(conn, monkeypatch):
    # End-to-end through config_from_env, not a hand-built FansConfig: proves
    # the retired machinery is still wired to the env, not just still present.
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", False)
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    monkeypatch.setenv("AWAIR_FAN_HOST", "host.local")
    _seed_reading(conn, pm25=5.0)
    _seed_event(conn, "co2", tier="ceiling")

    check_fans(conn, FakeNotifier(), fans.config_from_env(), NOW)

    assert [url for url, _ in calls] == [
        "http://host.local/fan/1/speed1",
        "http://host.local/fan/2/speed1",
    ]


def test_config_from_env_enabled_is_strict(monkeypatch):
    # Anything other than the literal "true" (case-insensitive) is off — a
    # partial rename (e.g. "on") must never accidentally activate fans.
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", False)
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


# --- pm25 observability logs (#15) ---


def test_check_fans_logs_pm25_near_miss(conn, monkeypatch, caplog):
    """A pm25 reading at/above 15 is INFO-logged even when no fan candidacy exists."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=17.0)  # no open events -> no candidacy, just a near-miss
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    hits = [r for r in caplog.records if "pm25 near-miss" in r.message]
    assert len(hits) == 1
    assert "17" in hits[0].message
    # Suppressor threshold echoed so the log line is self-describing.
    assert "25" in hits[0].message


def test_check_fans_does_not_log_near_miss_below_threshold(conn, monkeypatch, caplog):
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=10.0)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert not any("near-miss" in r.message for r in caplog.records)


def test_check_fans_logs_candidacy_when_engaged_and_suppressor_passes(
    conn, monkeypatch, caplog
):
    """An engaged event + clean pm25 records the value + a 'passed' verdict."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=8.0)
    _seed_event(conn, "co2")  # opens with fans_engaged=1 via _seed_event default? no
    # _seed_event doesn't set the latch; the score-gate path does. Emulate by
    # writing the latch directly.
    conn.execute("UPDATE alert_events SET fans_engaged=1")
    conn.commit()
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    candidacy = [r for r in caplog.records if "fan-on candidacy" in r.message]
    assert len(candidacy) == 1
    assert "pm25=8" in candidacy[0].message
    assert "suppressor=passed" in candidacy[0].message


def test_check_fans_logs_candidacy_when_suppressor_fires(conn, monkeypatch, caplog):
    """Engaged event + pm25>=25 records the value + 'fired' verdict + action=off."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=30.0)
    _seed_event(conn, "co2")
    conn.execute("UPDATE alert_events SET fans_engaged=1")
    conn.commit()
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    candidacy = [r for r in caplog.records if "fan-on candidacy" in r.message]
    assert len(candidacy) == 1
    assert "pm25=30" in candidacy[0].message
    assert "suppressor=fired" in candidacy[0].message
    assert "action=off" in candidacy[0].message


def test_check_fans_no_candidacy_log_when_no_engaged_event(conn, monkeypatch, caplog):
    """A near-miss without an engaged event logs the near-miss but not a candidacy."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=18.0)  # near-miss zone but no open event
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert not any("fan-on candidacy" in r.message for r in caplog.records)
    assert any("pm25 near-miss" in r.message for r in caplog.records)
