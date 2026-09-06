"""Ceiling-fan mitigation: run the fans while CO2 is high, off otherwise.

Mitigation was retired in #61 and is **live again as of ADR-002**, on a different
trigger. The retired design fired off co2/voc *spike events* — thresholds
relative to a rolling baseline, latched by the Awair score. Measured over 303 h
that ran the fans 32% of the time, because a voc-ceiling event in this house
stays open for half a day. See
`docs/decisions/001-retire-automatic-fan-mitigation.md`.

The trigger is now a single absolute number: **co2 >= CO2_FAN_ON (1000 ppm)**,
with hysteresis releasing at CO2_FAN_OFF. An absolute threshold means the same
thing in July and January, where a baseline-relative one drifts with the season.
Replayed over the 1365 h to 2026-09-06, the shipped rules run the fans 8.4 h —
a **0.62% duty cycle** across 7 runs, longest 1.5 h, none of it overnight. That
is the transient-burst behaviour #14 assumed and voc never had.
`docs/decisions/002-co2-only-fan-mitigation.md` records the reversal, and
`test_replay_summer_2026_stays_under_two_percent_duty` keeps it honest.

Three numbers, because they are easy to confuse: the bare threshold (co2 >=
1000, no hysteresis) covers 0.7% of the recording; adding hysteresis down to
900 raises it to 1.29%, since a run holds through the sags that would otherwise
end it; adding the cap brings it to 0.62%.

Two things outrank the trigger, in this order:

- **pm25 suppression** — particulate at or above PM25_SUPPRESS_THRESHOLD blocks
  turn-on and forces a running fan off, because fans re-suspend settled dust.
- **the duration cap** — FAN_MAX_RUN bounds one run regardless of co2; once
  capped the fans stay off until co2 recovers below CO2_FAN_OFF.

Split cleanly for testability:

- `co2_calls_for_fans(latest_co2, running)` — pure; the hysteresis band.
- `pm25_suppresses(latest_pm25)` — pure; does particulate veto the fans.
- `run_exhausted(started_at, now)` — pure; has this run hit the cap.
- `desired_action(latest_co2, latest_pm25, run, now)` — pure verdict.
- `next_run(run, verdict, now)` — pure; run bookkeeping to persist.
- `decide(fan_id, action, reason, state, now)` — rate-limit + no-op filter.
- `actuate(decision, config, opener)` — thin urllib GET at the NodeMCU endpoint.
- `release_fans(conn, notifier, config, now)` — let go of any fan we left running.
- `check_fans(conn, notifier, config, now)` — glue: reads state, drives fans, persists, alerts.
"""

import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from awair import db

log = logging.getLogger("awair.fans")


def _env_float(name: str, default: float) -> float:
    """Read one tunable from the environment, falling back to `default`.

    Read at import because systemd sets the environment before the process
    starts, and a malformed value should crash the poller at boot rather than
    surface as a strange fan decision hours later.

    These are env-tunable at all because CO2_FAN_ON sits close to the measured
    p99 (see below): if a winter baseline shift makes 1000 too eager, the fix
    should be a one-line env change and a restart, not a code deploy.
    """
    raw = os.environ.get(name)
    return default if raw is None else float(raw)


# Fans come on at CO2_FAN_ON and do not go off again until CO2_FAN_OFF. A single
# threshold chatters: polls are 30 s apart and the median episode is ~10 minutes
# of co2 wandering back and forth across the line.
CO2_FAN_ON = _env_float("AWAIR_CO2_FAN_ON", 1000.0)
CO2_FAN_OFF = _env_float("AWAIR_CO2_FAN_OFF", 900.0)
# One trigger, one speed. The old speed1/2/3 ladder ranked co2+voc combinations
# and has nothing left to rank; max observed co2 was 1408, so a magnitude ladder
# would leave speed3 permanently unreachable anyway.
FAN_SPEED = "speed1"
# Trust co2 only within this window — the fans must not keep running on a
# half-hour-old reading because the device dropped off the network.
CO2_FRESHNESS = timedelta(minutes=5)
# Raised 25 -> 100 in ADR-002. At 25 the suppressor vetoed 36.9% of fan-worthy time:
# cooking drives co2 and particulate together, so the old threshold switched the
# feature off in exactly the conditions that called for it. At 100 it vetoes
# 21.2% — still substantial, and still the smokiest fifth. Env-tunable because
# that trade is a judgement call about these fans in this kitchen, and 150
# (6.7%) or no suppressor at all are both defensible settings.
PM25_SUPPRESS_THRESHOLD = _env_float("AWAIR_PM25_SUPPRESS", 100.0)
PM25_SUPPRESS_REASON_PREFIX = "pm25 "  # decide() uses this to detect safety-off
# Near-miss watchpoint: pm25 readings at or above this value are logged so we
# can see how close the suppressor is to firing without changing behavior.
# Re-based for the 100 suppressor (ADR-002) from 8 weeks of readings: p99 = 44,
# p99.9 = 142, so 50 is a floor above ordinary noise and 50 below the
# suppressor. Left at 15 it would have fired on 3.4% of all polls — a watchpoint
# nobody reads is a watchpoint that never warns.
PM25_NEAR_MISS_THRESHOLD = _env_float("AWAIR_PM25_NEAR_MISS", 50.0)
RATE_LIMIT = timedelta(seconds=60)
# Trust pm25 only within this window — the suppressor must not act on a hours-old
# reading if the sensor drops pm25 for a while.
PM25_FRESHNESS = timedelta(minutes=5)
# A single run is bounded regardless of what co2 is doing, and once capped the
# fans stay off until co2 recovers below CO2_FAN_OFF.
# This is a working part of the controller, not standby insurance: over the
# recorded 8 weeks it ends 3 of 7 runs, holds the longest to 1.5 h rather than
# 6.2 h, and takes overnight running from 1.6 h to zero. Raising it is a real
# trade — at 240 min the duty cycle is 1.06% and two runs still cap; dropping to
# 60 min caps 6 of 7 and makes the timer, not the air, the thing in charge.
FAN_MAX_RUN = timedelta(minutes=_env_float("AWAIR_FAN_MAX_RUN_MINUTES", 90.0))
FAN_CMD_TIMEOUT_SECONDS = 5
DEFAULT_FAN_HOST = "192.168.68.68"
DEFAULT_FAN_IDS = (1, 2)
# Automatic fan mitigation was retired in #61 and is live again as of ADR-002 on a
# different trigger — absolute co2 rather than baseline-relative co2/voc spike
# events. This constant stays, rather than being deleted along with the
# retirement, because it is the kill switch ADR-001 established: one edit here
# takes the fans out of the loop and makes the poller release them.
# See docs/decisions/002-co2-only-fan-mitigation.md.
MITIGATION_RETIRED = False
# Recorded when a *disabled* poller commands a fan off. Deliberately not a
# verdict about the air like "no co2/voc spike" — it is the poller letting go of
# a fan it has stopped managing.
RELEASE_REASON = "fan mitigation disabled, releasing fan"
# A release that cannot reach the NodeMCU gives up after this many failures and
# says so once, on ntfy. The drive path retries indefinitely on purpose —
# somebody is watching fans they asked for — but the *retired* path must not
# become the only thing in this poller still talking to the network, once a
# minute, forever, with `if ok:` swallowing every notification. Counted per
# process, so restarting the poller is a deliberate "try again".
RELEASE_MAX_ATTEMPTS = 5
_release_attempts: dict[int, int] = {}


@dataclass(frozen=True)
class FansConfig:
    enabled: bool
    fan_host: str
    fan_ids: tuple[int, ...]


@dataclass(frozen=True)
class MitigationDecision:
    fan_id: int
    action: str  # "off" | "speed1" | "speed2" | "speed3"
    reason: str


@dataclass(frozen=True)
class RunState:
    """What we know about one fan's current mitigation run.

    Per-fan rather than global even though `check_fans` commands every fan
    alike: if one NodeMCU call fails the fans genuinely diverge, and per-fan
    state records that instead of averaging it away.
    """

    running: bool
    started_at: object | None  # datetime, or None when the fan is off
    capped: bool


@dataclass(frozen=True)
class Verdict:
    """The pure verdict for one poll: what to do, why, and the run flag to keep."""

    action: str
    reason: str
    capped: bool


def config_from_env() -> FansConfig:
    """Read fan config from the environment, honouring the code kill switch.

    While `MITIGATION_RETIRED` is set the enable flag cannot turn mitigation on.
    It is logged rather than silently dropped: a deploy whose env still asks for
    fans should say so out loud, so nobody debugs "why aren't the fans running"
    against a variable that no longer has any power. That switch is off as
    shipped (ADR-002) — this path is what a future retirement would use.
    """
    requested = (
        os.environ.get("AWAIR_FAN_MITIGATION_ENABLED", "false").lower() == "true"
    )
    if requested and MITIGATION_RETIRED:
        log.warning(
            "ignoring AWAIR_FAN_MITIGATION_ENABLED=true: automatic fan mitigation"
            " is disabled in code (fans.MITIGATION_RETIRED). Fans will be"
            " released off, not driven."
        )
    return FansConfig(
        enabled=requested and not MITIGATION_RETIRED,
        fan_host=os.environ.get("AWAIR_FAN_HOST", DEFAULT_FAN_HOST),
        fan_ids=DEFAULT_FAN_IDS,
    )


def co2_calls_for_fans(latest_co2: float, running: bool) -> bool:
    """Hysteresis: on at CO2_FAN_ON, off under CO2_FAN_OFF, hold in between.

    The band is what makes a single absolute threshold usable at a 30 s poll
    interval. Episodes here are ~10 minutes of co2 drifting across the line, so
    a bare `>= CO2_FAN_ON` would command the fans several times a minute; the
    rate limit would mask some of that and leave the rest as audible chatter.
    """
    if latest_co2 >= CO2_FAN_ON:
        return True
    if latest_co2 < CO2_FAN_OFF:
        return False
    return running


def run_exhausted(started_at, now) -> bool:
    """Whether the current run has been going for at least FAN_MAX_RUN.

    A run with no recorded start is never exhausted. That is the migration case
    — a fan already running when the cap shipped has no start time we actually
    observed, and inventing one would either cap it instantly or never.
    """
    return started_at is not None and now - started_at >= FAN_MAX_RUN


def pm25_suppresses(latest_pm25: float | None) -> bool:
    """Whether this pm25 reading blocks fan mitigation outright.

    A missing reading does *not* suppress: the suppressor needs positive
    evidence of particulate, and `check_fans` already only asks about readings
    inside PM25_FRESHNESS.
    """
    return latest_pm25 is not None and latest_pm25 >= PM25_SUPPRESS_THRESHOLD


def desired_action(
    latest_co2: float | None,
    latest_pm25: float | None,
    run: RunState,
    now,
) -> Verdict:
    """The target fan action for one poll, from sensor state plus this fan's run.

    Precedence:
      1. pm25 at/above the suppressor → off (particulate re-suspension risk).
      2. No fresh co2 reading → off; absent data means don't act.
      3. Capped, and co2 has not yet recovered → stay off.
      4. This run has hit FAN_MAX_RUN → off, and latch `capped`.
      5. Otherwise the hysteresis band decides.

    `capped` clears only once co2 drops under CO2_FAN_OFF. Without that the cap
    would defeat itself: it fires at 90 minutes while co2 is still, say, 1100,
    and the next poll's hysteresis would turn the fans straight back on.

    The two absent-data paths carry `run.capped` through unchanged rather than
    clearing it. A sensor gap is not evidence the air recovered, and clearing on
    it would hand back a fresh 90 minutes for free.
    """
    if pm25_suppresses(latest_pm25):
        return Verdict(
            "off",
            f"{PM25_SUPPRESS_REASON_PREFIX}{latest_pm25:g} suppresses fans",
            capped=run.capped,
        )
    if latest_co2 is None:
        return Verdict("off", "no fresh co2 reading", capped=run.capped)
    if run.capped and latest_co2 >= CO2_FAN_OFF:
        return Verdict("off", f"co2 {latest_co2:g} still high, run capped", capped=True)
    if run.running and run_exhausted(run.started_at, now):
        minutes = int(FAN_MAX_RUN.total_seconds() // 60)
        return Verdict("off", f"run hit the {minutes} min cap", capped=True)
    if co2_calls_for_fans(latest_co2, run.running):
        return Verdict(FAN_SPEED, f"co2 {latest_co2:g} elevated", capped=False)
    return Verdict("off", f"co2 {latest_co2:g} below {CO2_FAN_OFF:g}", capped=False)


def next_run(run: RunState, verdict: Verdict, now):
    """The `(run_started_at, capped)` pair to persist after applying `verdict`.

    A run starts when a stopped fan is told to spin up, carries its original
    start while it keeps running, and clears when the fan goes off. A fan found
    running with no recorded start — the migration case — adopts `now`, so the
    cap measures from when we first knew about it rather than never firing.
    """
    if verdict.action == "off":
        return None, verdict.capped
    if run.running and run.started_at is not None:
        return run.started_at, verdict.capped
    return now, verdict.capped


def decide(
    fan_id: int,
    action: str,
    reason: str,
    state: dict,
    now,
) -> MitigationDecision | None:
    """Rate-limit + no-op filter around desired_action's verdict.

    Returns None if there's no change to make. The 1-cmd/min rate limit applies
    to routine transitions but is bypassed for pm25-driven safety-off (fans
    stirring dust into a particulate spike is the exact failure mode the
    suppressor exists to prevent — don't let a recent command block it).
    """
    if state["last_action"] == action:
        return None
    is_safety_off = action == "off" and reason.startswith(PM25_SUPPRESS_REASON_PREFIX)
    if not is_safety_off and now - state["last_command_at"] < RATE_LIMIT:
        return None
    return MitigationDecision(fan_id=fan_id, action=action, reason=reason)


def actuate(decision: MitigationDecision, config: FansConfig, opener=None) -> bool:
    """Fire-and-forget GET at the NodeMCU. Returns True on 2xx, False otherwise.

    Failure never raises — the caller only advances last_action on success
    (avoids silent DB/physical desync on a transient NodeMCU blip). Wall-
    control / manual-remote changes remain a soft-partial: we can't observe them.
    """
    open_url = opener or urllib.request.urlopen
    url = f"http://{config.fan_host}/fan/{decision.fan_id}/{decision.action}"
    try:
        with open_url(url, timeout=FAN_CMD_TIMEOUT_SECONDS):
            return True
    except OSError as exc:
        log.warning("fan actuate failed %s: %s", url, exc)
        return False


def run_fan_test(conn, notifier, config: FansConfig, now, opener=None) -> None:
    """Manual smoke test (`--test`): every fan to speed1, then a "Fan test" page.

    Deliberately ignores config.enabled — proving the NodeMCU and ntfy plumbing
    works is what you do before flipping mitigation on. Successful commands are
    recorded so a running poller resumes from physical truth (and turns the
    fans back off once no event calls for them).
    """
    for fan_id in config.fan_ids:
        decision = MitigationDecision(
            fan_id=fan_id, action="speed1", reason="manual fan test"
        )
        ok = actuate(decision, config, opener)
        log.info("fan test: fan %d -> speed1 actuate=%s", fan_id, ok)
        if ok:
            db.upsert_fan_state(conn, fan_id=fan_id, action="speed1", command_at=now)
    notifier.send("Fan test")


def _log_pm25_observability(
    latest_co2: float | None, latest_pm25: float | None, verdict: Verdict
) -> None:
    """Emit two INFO lines for the pm25 suppressor without changing behavior (#15).

    1. **Near-miss**: any poll with pm25 >= PM25_NEAR_MISS_THRESHOLD is logged,
       whether or not a fan-on candidacy exists. Builds the distribution we need
       to see the suppressor's headroom shrink before it ever fires.
    2. **Candidacy trace**: when co2 alone would call for fans, log the pm25 the
       suppressor saw and the verdict it produced.

    The #15 note about not inferring the suppressor from `action == "off"` has
    since come true: the duration cap is a second off-path, so the trace asks
    `pm25_suppresses` directly and reports the cap as its own field.
    """
    if latest_pm25 is not None and latest_pm25 >= PM25_NEAR_MISS_THRESHOLD:
        log.info(
            "pm25 near-miss %g (suppressor fires at %g)",
            latest_pm25,
            PM25_SUPPRESS_THRESHOLD,
        )
    if latest_co2 is not None and latest_co2 >= CO2_FAN_ON:
        log.info(
            "fan-on candidacy: co2=%g pm25=%s suppressor=%s capped=%s action=%s",
            latest_co2,
            "unknown" if latest_pm25 is None else f"{latest_pm25:g}",
            "fired" if pm25_suppresses(latest_pm25) else "passed",
            verdict.capped,
            verdict.action,
        )


def _command_fan(conn, notifier, config: FansConfig, fan_id, action, reason, now):
    """Decide → actuate → persist → alert for one fan. Silent when nothing changes.

    Shared by the drive path (`check_fans`) and the release path
    (`release_fans`) so both inherit `decide`'s no-op filter and rate limit, and
    both persist failures the same way.

    Returns True/False for the actuation result, or None when nothing was due —
    `release_fans` needs to tell "the command failed" from "no command was owed
    this poll" to count its attempts honestly.
    """
    state = db.get_fan_state(conn, fan_id)
    decision = decide(fan_id, action, reason, state, now)
    if decision is None:
        return None
    ok = actuate(decision, config)
    log.info(
        "fan %d -> %s (%s) actuate=%s",
        fan_id,
        decision.action,
        decision.reason,
        ok,
    )
    # On failure, keep last_action == whatever the DB already believed —
    # don't record the failed target as "current." Stamp last_command_at
    # either way so the rate limit doubles as backoff (retry once per
    # RATE_LIMIT, not every poll).
    db.upsert_fan_state(
        conn,
        fan_id=fan_id,
        action=decision.action if ok else state["last_action"],
        command_at=now,
    )
    if ok:
        notifier.send(
            f"fan {fan_id} -> {decision.action} ({decision.reason})",
            title="Awair fan mitigation",
        )
    return ok


def _release_one(conn, notifier, config: FansConfig, fan_id, now) -> None:
    """Release one fan, giving up loudly after RELEASE_MAX_ATTEMPTS failures."""
    if _release_attempts.get(fan_id, 0) >= RELEASE_MAX_ATTEMPTS:
        return
    ok = _command_fan(conn, notifier, config, fan_id, "off", RELEASE_REASON, now)
    if ok is None:  # nothing owed this poll — already off, or rate-limited
        return
    if ok:
        _release_attempts.pop(fan_id, None)
        return
    _release_attempts[fan_id] = _release_attempts.get(fan_id, 0) + 1
    if _release_attempts[fan_id] < RELEASE_MAX_ATTEMPTS:
        return
    log.warning(
        "giving up releasing fan %d after %d attempts", fan_id, RELEASE_MAX_ATTEMPTS
    )
    notifier.send(
        f"could not turn fan {fan_id} off after {RELEASE_MAX_ATTEMPTS} tries —"
        " it may still be running. Fan mitigation is disabled, so nothing will"
        " try again until the poller restarts; switch it off at the wall.",
        title="Awair fan mitigation",
        priority="high",
    )


def release_fans(conn, notifier, config: FansConfig, now) -> None:
    """Command off, once, any fan this poller still believes it left running.

    Disabling mitigation has to mean "the fans are not running", not "the fans
    are frozen wherever the last command left them" (#61). The old early return
    meant flipping the kill switch mid-event stranded both fans at speed1 with
    nothing left running that would ever turn them back off — the complaint the
    switch exists to answer, made permanent.

    This is deliberately *not* `desired_action`'s verdict. On the live box the
    open voc event is latched, so the verdict is still "speed1"; asking for it
    would no-op against `last_action == "speed1"` and strand the fans exactly as
    the early return did. A release is unconditional: off, because we have
    stopped managing this fan, not because the air is clean.

    `decide`'s no-op filter makes it exactly one command per fan per transition:
    once `last_action` is "off" every later poll is silent, so a disabled poller
    does not fight a fan switched on at the wall afterwards.
    """
    for fan_id in config.fan_ids:
        _release_one(conn, notifier, config, fan_id, now)


def _drive_one(
    conn, notifier, config: FansConfig, fan_id, latest_co2, latest_pm25, now
):
    """Decide, persist the run bookkeeping, and command one fan. Returns the verdict.

    The `set_fan_run` write happens whether or not a command is owed: clearing
    `capped` happens on a poll where the fans are already off and nothing else
    would write. See `db.set_fan_run`.
    """
    state = db.get_fan_state(conn, fan_id)
    run = RunState(
        running=state["last_action"] != "off",
        started_at=state["run_started_at"],
        capped=state["capped"],
    )
    verdict = desired_action(latest_co2, latest_pm25, run, now)
    started_at, capped = next_run(run, verdict, now)
    db.set_fan_run(conn, fan_id, started_at=started_at, capped=capped)
    _command_fan(conn, notifier, config, fan_id, verdict.action, verdict.reason, now)
    return verdict


def check_fans(conn, notifier, config: FansConfig, now) -> None:
    """One poll's worth of fan control.

    When mitigation is disabled this releases the fans rather than returning —
    see `release_fans`.
    """
    if not config.enabled:
        release_fans(conn, notifier, config, now)
        return
    latest_co2 = db.latest_co2(conn, since=now - CO2_FRESHNESS)
    latest_pm25 = db.latest_pm25(conn, since=now - PM25_FRESHNESS)
    verdicts = [
        _drive_one(conn, notifier, config, fan_id, latest_co2, latest_pm25, now)
        for fan_id in config.fan_ids
    ]
    # One trace per poll, not per fan: the fans are commanded alike, and the
    # only thing that diverges between them is a failed actuate, which
    # `_command_fan` already logs on its own.
    if verdicts:
        _log_pm25_observability(latest_co2, latest_pm25, verdicts[0])
