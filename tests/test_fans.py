"""Fan mitigation: verdict, rate limit, actuation, and the check_fans glue.

Trigger surface is an absolute co2 threshold with hysteresis (ADR-002); suppressors
are a raw pm25 read and a per-run duration cap. Scenarios that predate ADR-002 and
still hold — the rate limit, actuation, and the whole release path from #61 —
are unchanged from the versions written against the spike-event trigger.
"""

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from awair import db, fans
from awair.fans import (
    FansConfig,
    MitigationDecision,
    RunState,
    actuate,
    check_fans,
    co2_calls_for_fans,
    decide,
    desired_action,
    next_run,
    run_exhausted,
)
from tests._helpers import FakeNotifier, fake_url_opener

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)

# Above CO2_FAN_ON, inside the hysteresis band, and below CO2_FAN_OFF.
CO2_HIGH = 1100.0
CO2_BAND = 950.0
CO2_LOW = 500.0


@pytest.fixture(autouse=True)
def _clear_release_budget():
    """`fans._release_attempts` is per-process state; don't leak it across tests."""
    fans._release_attempts.clear()
    yield
    fans._release_attempts.clear()


def _counting(fn, sink):
    """Wrap `fn`, appending one entry to `sink` per call."""

    def wrapper(*args, **kwargs):
        sink.append(args)
        return fn(*args, **kwargs)

    return wrapper


def _run(running=False, started_at=None, capped=False):
    """A RunState, defaulting to 'fan is off and has no history'."""
    return RunState(running=running, started_at=started_at, capped=capped)


# --- co2_calls_for_fans: the hysteresis band ---


def test_at_or_above_the_on_threshold_calls_for_fans():
    assert co2_calls_for_fans(fans.CO2_FAN_ON, running=False) is True
    assert co2_calls_for_fans(fans.CO2_FAN_ON + 200, running=False) is True


def test_below_the_off_threshold_never_calls_for_fans():
    assert co2_calls_for_fans(fans.CO2_FAN_OFF - 0.1, running=True) is False
    assert co2_calls_for_fans(CO2_LOW, running=True) is False


def test_inside_the_band_holds_whatever_the_fan_is_already_doing():
    """The band is the whole point: 950 ppm neither starts nor stops a fan.

    Without it, co2 drifting either side of a single threshold would command
    the fans several times a minute during an ordinary cooking episode.
    """
    assert co2_calls_for_fans(CO2_BAND, running=True) is True
    assert co2_calls_for_fans(CO2_BAND, running=False) is False


def test_the_off_threshold_is_the_inclusive_edge_of_the_band():
    # >= CO2_FAN_OFF holds; strictly below releases.
    assert co2_calls_for_fans(fans.CO2_FAN_OFF, running=True) is True
    assert co2_calls_for_fans(fans.CO2_FAN_OFF, running=False) is False


# --- pm25_suppresses / run_exhausted: the two vetoes ---


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


def test_run_exhausted_needs_a_recorded_start():
    """No start time means not exhausted — the migration case.

    A fan already running when the cap shipped has no observed start. Treating
    that as exhausted would cap it the instant the poller restarts; treating it
    as ancient would cap it forever. It is adopted into a fresh run instead.
    """
    assert run_exhausted(None, NOW) is False


def test_run_exhausted_at_and_around_the_cap():
    assert run_exhausted(NOW - fans.FAN_MAX_RUN, NOW) is True  # inclusive
    assert run_exhausted(NOW - fans.FAN_MAX_RUN + timedelta(seconds=1), NOW) is False
    assert run_exhausted(NOW - fans.FAN_MAX_RUN * 3, NOW) is True


# --- desired_action: co2 + the two vetoes, in precedence order ---


def test_clean_air_is_off():
    verdict = desired_action(CO2_LOW, 5.0, _run(), NOW)
    assert verdict.action == "off"
    assert "below" in verdict.reason


def test_high_co2_runs_the_fans_at_speed1():
    verdict = desired_action(CO2_HIGH, 5.0, _run(), NOW)
    assert verdict.action == fans.FAN_SPEED == "speed1"
    assert "1100" in verdict.reason


def test_pm25_suppressor_overrides_high_co2():
    # Fans re-suspend particulate — even with co2 well over the line, pm25 wins.
    verdict = desired_action(CO2_HIGH, fans.PM25_SUPPRESS_THRESHOLD + 5, _run(), NOW)
    assert verdict.action == "off"
    assert verdict.reason.startswith(fans.PM25_SUPPRESS_REASON_PREFIX)


def test_pm25_just_below_threshold_does_not_suppress():
    verdict = desired_action(CO2_HIGH, fans.PM25_SUPPRESS_THRESHOLD - 0.1, _run(), NOW)
    assert verdict.action == "speed1"


def test_missing_pm25_never_suppresses():
    # Sensor null / cold-boot: don't hallucinate a suppression.
    assert desired_action(CO2_HIGH, None, _run(), NOW).action == "speed1"


def test_missing_co2_is_off_not_a_guess():
    """A stale or absent co2 read must not keep the fans running.

    `check_fans` only passes readings inside CO2_FRESHNESS, so None means the
    device has gone quiet. Absent data means don't act — the same rule the old
    score gate followed.
    """
    verdict = desired_action(None, 5.0, _run(running=True, started_at=NOW), NOW)
    assert verdict.action == "off"
    assert "no fresh co2" in verdict.reason


def test_missing_co2_does_not_clear_the_cap():
    # A sensor gap is not evidence the air recovered; it must not hand back a
    # fresh 90 minutes of runtime for free.
    verdict = desired_action(None, 5.0, _run(capped=True), NOW)
    assert verdict.capped is True


def test_pm25_suppression_does_not_clear_the_cap():
    verdict = desired_action(CO2_HIGH, 500.0, _run(capped=True), NOW)
    assert verdict.action == "off"
    assert verdict.capped is True


# --- desired_action: the duration cap ---


def test_a_run_past_the_cap_is_turned_off_and_latched():
    run = _run(running=True, started_at=NOW - fans.FAN_MAX_RUN)
    verdict = desired_action(CO2_HIGH, 5.0, run, NOW)
    assert verdict.action == "off"
    assert verdict.capped is True
    assert "cap" in verdict.reason


def test_a_run_inside_the_cap_keeps_going():
    run = _run(running=True, started_at=NOW - fans.FAN_MAX_RUN + timedelta(minutes=1))
    verdict = desired_action(CO2_HIGH, 5.0, run, NOW)
    assert verdict.action == "speed1"
    assert verdict.capped is False


def test_capped_fans_stay_off_while_co2_is_still_high():
    """The cap has to outlast the episode that triggered it.

    Without the latch the cap defeats itself: it fires at 90 minutes while co2
    is still 1100, and the very next poll's hysteresis turns the fans back on.
    """
    verdict = desired_action(CO2_HIGH, 5.0, _run(capped=True), NOW)
    assert verdict.action == "off"
    assert verdict.capped is True
    assert "capped" in verdict.reason


def test_capped_fans_stay_off_inside_the_hysteresis_band():
    # Recovery is measured against CO2_FAN_OFF, not CO2_FAN_ON — otherwise the
    # cap would release while co2 is still high enough to immediately re-engage.
    verdict = desired_action(CO2_BAND, 5.0, _run(capped=True), NOW)
    assert verdict.action == "off"
    assert verdict.capped is True


def test_the_cap_clears_once_co2_recovers():
    verdict = desired_action(CO2_LOW, 5.0, _run(capped=True), NOW)
    assert verdict.action == "off"
    assert verdict.capped is False


def test_fans_may_run_again_after_a_cap_clears():
    # Recover, then spike again: a new episode gets a fresh run.
    cleared = desired_action(CO2_LOW, 5.0, _run(capped=True), NOW)
    assert cleared.capped is False
    again = desired_action(CO2_HIGH, 5.0, _run(capped=cleared.capped), NOW)
    assert again.action == "speed1"


# --- next_run: the bookkeeping desired_action's verdict implies ---


def test_a_new_run_starts_now():
    started_at, capped = next_run(_run(), fans.Verdict("speed1", "co2", False), NOW)
    assert started_at == NOW
    assert capped is False


def test_a_continuing_run_keeps_its_original_start():
    """The cap measures the whole run, not the time since the last poll."""
    began = NOW - timedelta(minutes=40)
    started_at, _ = next_run(
        _run(running=True, started_at=began),
        fans.Verdict("speed1", "co2", False),
        NOW,
    )
    assert started_at == began


def test_a_fan_found_running_without_a_start_adopts_now():
    # Migration: the cap measures from when we first knew, rather than never.
    started_at, _ = next_run(
        _run(running=True, started_at=None),
        fans.Verdict("speed1", "co2", False),
        NOW,
    )
    assert started_at == NOW


def test_turning_off_clears_the_run_but_keeps_the_cap_flag():
    started_at, capped = next_run(
        _run(running=True, started_at=NOW - timedelta(hours=2)),
        fans.Verdict("off", "run hit the cap", True),
        NOW,
    )
    assert started_at is None
    assert capped is True


# --- decide: no-op filter + 1-cmd/min per-fan rate limit ---


def _state(action="off", last_cmd_seconds_ago=3600):
    return {
        "fan_id": 1,
        "last_action": action,
        "last_command_at": NOW - timedelta(seconds=last_cmd_seconds_ago),
        "run_started_at": None,
        "capped": False,
    }


def test_same_action_is_noop():
    assert decide(1, "off", "co2 low", _state("off"), NOW) is None


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
    state = _state()
    state["last_command_at"] = datetime(1970, 1, 1, tzinfo=UTC)
    assert decide(1, "speed1", "co2 spike", state, NOW) is not None


def test_pm25_safety_off_bypasses_rate_limit():
    # Fans were just kicked to speed1 for a co2 spike; 20s later pm25 crosses
    # the suppressor threshold. Waiting the rest of the 60s to turn them off
    # would keep them stirring particulate — the safety-off must fire now.
    reason = "pm25 140 suppresses fans"
    d = decide(1, "off", reason, _state("speed1", last_cmd_seconds_ago=20), NOW)
    assert d is not None
    assert d.action == "off"


def test_non_pm25_off_still_respects_rate_limit():
    # Ordinary "co2 recovered → off" transitions are not safety-critical; they
    # still respect the rate limit. The duration cap included: 90 minutes in,
    # another 40 seconds of fan is not an emergency.
    assert (
        decide(
            1,
            "off",
            "run hit the 90 min cap",
            _state("speed1", last_cmd_seconds_ago=20),
            NOW,
        )
        is None
    )


# --- actuate: fire-and-forget urllib GET ---


def test_actuate_hits_the_fan_endpoint():
    calls = []
    ok = actuate(
        MitigationDecision(fan_id=2, action="speed1", reason="co2"),
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


def _seed_reading(conn, pm25, co2=CO2_LOW, ts=NOW, score=80):
    """One reading. co2 defaults below the band so fans stay off by default."""
    ts_iso = db.iso_z(ts)
    conn.execute(
        "INSERT INTO readings (ts, received_at, score, co2, voc, pm25)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts_iso, ts_iso, score, co2, 100, pm25),
    )
    conn.commit()


def test_disabled_poller_touches_nothing_when_no_fan_is_running(conn):
    # Nothing recorded => nothing to release. A disabled poller on a clean DB
    # must stay completely silent rather than spamming an off command.
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1, 2))
    check_fans(conn, notifier, cfg, NOW)
    assert notifier.sent == []
    assert conn.execute("SELECT COUNT(*) FROM fan_state").fetchone()[0] == 0


def test_disabled_poller_releases_fans_it_left_running(conn, monkeypatch):
    """Disabling must mean "the fans are not running", not "frozen where they were".

    The state on the homelab when #61 was filed: both fans at speed1 for ten
    hours with the trigger still true. A release that merely asked for
    `desired_action`'s verdict would get "speed1" back — co2 is still high —
    no-op against last_action, and strand them exactly as the old early return
    did. The release is unconditional for that reason, so the fixture keeps co2
    high to prove it.
    """
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1, 2))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    assert (
        desired_action(CO2_HIGH, 5.0, _run(running=True, started_at=NOW), NOW).action
        == "speed1"
    )
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
    assert [msg for _, msg, _ in notifier.sent] == [
        f"fan 1 -> off ({fans.RELEASE_REASON})",
        f"fan 2 -> off ({fans.RELEASE_REASON})",
    ]


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


def test_release_gives_up_loudly_instead_of_retrying_a_dead_nodemcu_forever(
    conn, monkeypatch
):
    """A release that can never land must stop, and must say so.

    Routing a disabled poller through `release_fans` means a NodeMCU that is
    unplugged — the natural thing to do after "kill that behavior entirely" —
    would otherwise make the disabled path the only thing in this poller still
    talking to the network, one GET per fan per minute forever, with `if ok:`
    swallowing every notification.
    """

    def broken(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", broken)
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1,))
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(hours=1)
    )

    # A day of 30s polls, well past RELEASE_MAX_ATTEMPTS.
    attempts = []
    monkeypatch.setattr(fans, "actuate", _counting(fans.actuate, attempts))
    for poll in range(2880):
        check_fans(conn, notifier, cfg, NOW + timedelta(seconds=30 * poll))

    assert len(attempts) == fans.RELEASE_MAX_ATTEMPTS
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"  # never lies
    assert len(notifier.sent) == 1
    _, message, priority = notifier.sent[0]
    assert priority == "high"
    assert "switch it off at the wall" in message


def test_release_attempt_budget_resets_when_the_nodemcu_comes_back(conn, monkeypatch):
    # Four failures then a success must not leave the fan one failure away from
    # being abandoned the next time mitigation is disabled mid-outage.
    def broken(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", broken)
    cfg = FansConfig(enabled=False, fan_host="host.local", fan_ids=(1,))
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(hours=1)
    )
    for poll in range(fans.RELEASE_MAX_ATTEMPTS - 1):
        check_fans(conn, FakeNotifier(), cfg, NOW + timedelta(seconds=90 * poll))
    assert fans._release_attempts[1] == fans.RELEASE_MAX_ATTEMPTS - 1

    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    check_fans(conn, FakeNotifier(), cfg, NOW + timedelta(hours=1))
    assert calls
    assert 1 not in fans._release_attempts


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


def test_check_fans_drives_both_fans_on_high_co2(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    notifier = FakeNotifier()
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    check_fans(conn, notifier, cfg, NOW)

    urls = [url for url, _ in calls]
    assert urls == ["http://host.local/fan/1/speed1", "http://host.local/fan/2/speed1"]
    assert len(notifier.sent) == 2
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"
    assert db.get_fan_state(conn, 2)["last_action"] == "speed1"
    # The run is stamped so the cap has something to measure from.
    assert db.get_fan_state(conn, 1)["run_started_at"] == NOW


def test_check_fans_leaves_fans_off_below_the_threshold(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    _seed_reading(conn, pm25=5.0, co2=CO2_LOW)
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_check_fans_forces_off_when_pm25_suppresses(conn, monkeypatch):
    """PM2.5 suppressor overrides a prior speed1 the poller set itself."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    # Seed: fan 1 already at speed1 from an earlier tick, 5 min ago.
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(minutes=5)
    )
    _seed_reading(conn, pm25=fans.PM25_SUPPRESS_THRESHOLD + 5, co2=CO2_HIGH)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_check_fans_holds_when_rate_limited(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    # Fan 1 changed 30s ago — inside the 60s cooldown.
    db.upsert_fan_state(
        conn, fan_id=1, action="off", command_at=NOW - timedelta(seconds=30)
    )
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

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
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
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


def test_check_fans_ignores_a_stale_co2_reading(conn, monkeypatch):
    """A co2 read older than CO2_FRESHNESS reads as no data — don't act on it."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH, ts=NOW - timedelta(hours=1))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    check_fans(conn, FakeNotifier(), cfg, NOW)

    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_check_fans_releases_fans_when_the_device_goes_quiet(conn, monkeypatch):
    """Fans running + co2 goes stale => turn them off, don't run on forever."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"

    # An hour later with no new reading at all.
    calls.clear()
    check_fans(conn, FakeNotifier(), cfg, NOW + timedelta(hours=1))
    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]


# --- check_fans: hysteresis and the duration cap, end to end ---


def test_fans_keep_running_inside_the_band(conn, monkeypatch):
    """Once on, co2 sagging to 950 must not turn the fans off and on again."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))

    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"

    calls.clear()
    later = NOW + timedelta(minutes=5)
    _seed_reading(conn, pm25=5.0, co2=CO2_BAND, ts=later)
    check_fans(conn, FakeNotifier(), cfg, later)

    assert calls == []  # no command at all: desired is still speed1
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"


def test_fans_stop_once_co2_drops_below_the_release_threshold(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    check_fans(conn, FakeNotifier(), cfg, NOW)

    calls.clear()
    later = NOW + timedelta(minutes=5)
    _seed_reading(conn, pm25=5.0, co2=CO2_LOW, ts=later)
    check_fans(conn, FakeNotifier(), cfg, later)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    assert db.get_fan_state(conn, 1)["run_started_at"] is None


def test_the_cap_stops_a_run_that_outlasts_it(conn, monkeypatch):
    """ADR-001's failure mode, bounded: a trigger that stays true all day.

    co2 never recovers, so the hysteresis alone would run the fans
    indefinitely. The cap ends the run and the latch keeps them off until the
    air actually improves.
    """
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))

    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert db.get_fan_state(conn, 1)["last_action"] == "speed1"

    calls.clear()
    capped_at = NOW + fans.FAN_MAX_RUN
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH, ts=capped_at)
    check_fans(conn, FakeNotifier(), cfg, capped_at)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]
    state = db.get_fan_state(conn, 1)
    assert state["capped"] is True
    assert state["run_started_at"] is None

    # Still high an hour later: the fans must stay off, not re-engage.
    calls.clear()
    for extra in (1, 2, 3):
        at = capped_at + timedelta(hours=extra)
        _seed_reading(conn, pm25=5.0, co2=CO2_HIGH, ts=at)
        check_fans(conn, FakeNotifier(), cfg, at)
    assert calls == []
    assert db.get_fan_state(conn, 1)["last_action"] == "off"


def test_a_cleared_cap_lets_the_next_episode_run(conn, monkeypatch):
    """The cap is a bound on one run, not a permanent shutdown."""
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    db.upsert_fan_state(
        conn, fan_id=1, action="off", command_at=NOW - timedelta(hours=1)
    )
    db.set_fan_run(conn, 1, started_at=None, capped=True)

    # Air recovers: the flag clears even though no command is owed.
    _seed_reading(conn, pm25=5.0, co2=CO2_LOW)
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert calls == []
    assert db.get_fan_state(conn, 1)["capped"] is False

    # A later episode runs normally.
    later = NOW + timedelta(hours=1)
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH, ts=later)
    check_fans(conn, FakeNotifier(), cfg, later)
    assert [url for url, _ in calls] == ["http://host.local/fan/1/speed1"]


# --- replay: the measured duty cycle is a regression gate (ADR-002) ---


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "co2_summer_2026.txt"


def _replay_samples():
    """`(minutes_from_start, co2)` from the recorded fixture.

    A bare line is a reading one grid step (5 min) after the previous one;
    `!N` says the next reading is N minutes later instead (a polling outage).
    """
    minute, gap, first = 0, None, True
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("!"):
            gap = int(line[1:])
            continue
        if first:
            first = False
        else:
            minute += gap if gap is not None else 5
        gap = None
        yield minute, float(line)


def _replay_duty_cycle():
    """Fraction of recorded time the shipped rules would have run the fans.

    pm25 is passed as None throughout: the fixture carries co2 only, so this
    isolates the trigger and the cap. Real duty is lower, because the pm25
    suppressor can only ever subtract fan-on time.
    """
    base = datetime(2026, 7, 11, tzinfo=UTC)
    run = RunState(running=False, started_at=None, capped=False)
    on_minutes = total_minutes = 0
    previous = None
    for minute, co2 in _replay_samples():
        now = base + timedelta(minutes=minute)
        verdict = desired_action(co2, None, run, now)
        started_at, capped = next_run(run, verdict, now)
        if previous is not None:
            # Credit the elapsed interval to the state that held during it.
            step = min(minute - previous, 10)
            total_minutes += step
            on_minutes += step if run.running else 0
        previous = minute
        run = RunState(
            running=verdict.action != "off", started_at=started_at, capped=capped
        )
    return on_minutes / total_minutes


def test_replay_summer_2026_stays_under_two_percent_duty():
    """The measurement that justified un-retiring, pinned as a gate (ADR-002).

    ADR-001 retired fan mitigation over a 32% duty cycle. The shipped co2 rules
    measure 0.62% across the same house's next eight weeks, and this replays
    that recording through them so a future threshold change cannot quietly
    walk back toward #61.

    Summer data: windows open, low baseline co2. It is a regression gate on the
    rules, not a promise about January — see ADR-002.
    """
    duty = _replay_duty_cycle()
    assert 0 < duty < 0.02, f"duty cycle {duty:.1%} over the recorded 8 weeks"


def test_replay_would_fail_at_a_lower_threshold(monkeypatch):
    """Proves the gate above can actually fail.

    At 800 ppm the same recording gives 7.6% — the tuning mistake the gate
    exists to catch. A test that passes at every threshold would be worthless.
    """
    monkeypatch.setattr(fans, "CO2_FAN_ON", 800.0)
    monkeypatch.setattr(fans, "CO2_FAN_OFF", 700.0)
    assert _replay_duty_cycle() > 0.02


# --- config: env parsing ---


def test_config_from_env_defaults_off(monkeypatch):
    monkeypatch.delenv("AWAIR_FAN_MITIGATION_ENABLED", raising=False)
    monkeypatch.delenv("AWAIR_FAN_HOST", raising=False)
    cfg = fans.config_from_env()
    assert cfg.enabled is False
    assert cfg.fan_host == fans.DEFAULT_FAN_HOST
    assert cfg.fan_ids == (1, 2)


def test_config_from_env_reads_fan_host(monkeypatch):
    monkeypatch.setenv("AWAIR_FAN_HOST", "10.0.0.10")
    assert fans.config_from_env().fan_host == "10.0.0.10"


def test_fan_mitigation_ships_live(monkeypatch):
    """The as-shipped default, asserted through behaviour and not monkeypatched.

    The inverse of #61's `test_fan_mitigation_ships_retired`, and load-bearing
    for the same reason: every other test sets `MITIGATION_RETIRED` explicitly,
    which would leave the shipped value unpinned. Retiring mitigation again
    should have to edit this test on purpose, in the same diff, where a
    reviewer will see it — exactly as un-retiring had to.
    """
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    assert fans.MITIGATION_RETIRED is False
    assert fans.config_from_env().enabled is True


def test_the_kill_switch_still_overrides_the_enable_flag(monkeypatch, caplog):
    # ADR-002 turned mitigation back on but kept ADR-001's kill switch. Setting it
    # must still beat the environment, and must say so rather than being
    # silently dropped.
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    with caplog.at_level("WARNING", logger="awair.fans"):
        assert fans.config_from_env().enabled is False
    assert "disabled in code" in caplog.text


def test_no_warning_when_the_env_does_not_ask_for_fans(monkeypatch, caplog):
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    monkeypatch.delenv("AWAIR_FAN_MITIGATION_ENABLED", raising=False)
    with caplog.at_level("WARNING", logger="awair.fans"):
        assert fans.config_from_env().enabled is False
    assert caplog.text == ""


def test_the_kill_switch_makes_a_running_poller_release_the_fans(conn, monkeypatch):
    # End-to-end through config_from_env: setting the switch must not merely
    # stop driving the fans, it must let go of any it left running (#61).
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener(calls))
    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    monkeypatch.setenv("AWAIR_FAN_MITIGATION_ENABLED", "true")
    monkeypatch.setenv("AWAIR_FAN_HOST", "host.local")
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    db.upsert_fan_state(
        conn, fan_id=1, action="speed1", command_at=NOW - timedelta(hours=1)
    )

    check_fans(conn, FakeNotifier(), fans.config_from_env(), NOW)

    assert [url for url, _ in calls] == ["http://host.local/fan/1/off"]


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


# --- pm25 observability logs (#15) ---


def test_check_fans_logs_pm25_near_miss(conn, monkeypatch, caplog):
    """A pm25 reading at/above the watchpoint is logged even with no candidacy."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    near_miss = fans.PM25_NEAR_MISS_THRESHOLD + 2
    _seed_reading(conn, pm25=near_miss, co2=CO2_LOW)  # no candidacy, just a near-miss
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    hits = [r for r in caplog.records if "pm25 near-miss" in r.message]
    assert len(hits) == 1
    assert f"{near_miss:g}" in hits[0].message
    # Suppressor threshold echoed so the log line is self-describing.
    assert f"{fans.PM25_SUPPRESS_THRESHOLD:g}" in hits[0].message


def test_check_fans_does_not_log_near_miss_below_threshold(conn, monkeypatch, caplog):
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=fans.PM25_NEAR_MISS_THRESHOLD - 1, co2=CO2_LOW)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert not any("near-miss" in r.message for r in caplog.records)


def test_check_fans_logs_candidacy_when_the_suppressor_passes(
    conn, monkeypatch, caplog
):
    """High co2 + clean pm25 records the value and a 'passed' verdict."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=8.0, co2=CO2_HIGH)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    candidacy = [r for r in caplog.records if "fan-on candidacy" in r.message]
    assert len(candidacy) == 1
    assert "pm25=8" in candidacy[0].message
    assert "suppressor=passed" in candidacy[0].message
    assert "action=speed1" in candidacy[0].message


def test_check_fans_logs_candidacy_when_the_suppressor_fires(conn, monkeypatch, caplog):
    """High co2 + high pm25 records the value, 'fired', and action=off."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    dirty = fans.PM25_SUPPRESS_THRESHOLD + 20
    _seed_reading(conn, pm25=dirty, co2=CO2_HIGH)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    candidacy = [r for r in caplog.records if "fan-on candidacy" in r.message]
    assert len(candidacy) == 1
    assert f"pm25={dirty:g}" in candidacy[0].message
    assert "suppressor=fired" in candidacy[0].message
    assert "action=off" in candidacy[0].message


def test_check_fans_no_candidacy_log_when_co2_is_low(conn, monkeypatch, caplog):
    """A near-miss without a co2 candidacy logs the near-miss but no candidacy."""
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=fans.PM25_NEAR_MISS_THRESHOLD + 5, co2=CO2_LOW)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1,))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert not any("fan-on candidacy" in r.message for r in caplog.records)
    assert any("pm25 near-miss" in r.message for r in caplog.records)


def test_candidacy_is_logged_once_per_poll_not_once_per_fan(conn, monkeypatch, caplog):
    monkeypatch.setattr("urllib.request.urlopen", fake_url_opener([]))
    _seed_reading(conn, pm25=5.0, co2=CO2_HIGH)
    cfg = FansConfig(enabled=True, fan_host="host.local", fan_ids=(1, 2, 3))
    caplog.set_level("INFO", logger="awair.fans")
    check_fans(conn, FakeNotifier(), cfg, NOW)
    assert len([r for r in caplog.records if "fan-on candidacy" in r.message]) == 1
