"""Ceiling-fan mitigation: turn fans on when CO2/TVOC spike, off otherwise.

The trigger surface reuses `awair.spikes` events (co2/voc open = fans should run;
both closed = fans off). PM2.5 is a **suppressor** — an elevated pm25 reading
blocks turn-on and forces any running fan off, because fans re-suspend particulate
and would worsen the local reading. See issue #10 for the design memo.

Split cleanly for testability:

- `desired_action(open_events, latest_pm25)` — pure verdict from sensor state.
- `decide(fan_id, action, reason, state, now)` — rate-limit + no-op filter.
- `actuate(decision, config, opener)` — thin urllib GET at the NodeMCU endpoint.
- `check_fans(conn, notifier, config, now)` — glue: reads state, drives fans, persists, alerts.
"""

import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from awair import db

log = logging.getLogger("awair.fans")

FAN_TRIGGERS = ("co2", "voc")
PM25_SUPPRESS_THRESHOLD = 25.0
PM25_SUPPRESS_REASON_PREFIX = "pm25 "  # decide() uses this to detect safety-off
RATE_LIMIT = timedelta(seconds=60)
# Trust pm25 only within this window — the suppressor must not act on a hours-old
# reading if the sensor drops pm25 for a while.
PM25_FRESHNESS = timedelta(minutes=5)
FAN_CMD_TIMEOUT_SECONDS = 5
DEFAULT_FAN_HOST = "192.168.68.68"
DEFAULT_FAN_IDS = (1, 2)


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


def config_from_env() -> FansConfig:
    return FansConfig(
        enabled=os.environ.get("AWAIR_FAN_MITIGATION_ENABLED", "false").lower()
        == "true",
        fan_host=os.environ.get("AWAIR_FAN_HOST", DEFAULT_FAN_HOST),
        fan_ids=DEFAULT_FAN_IDS,
    )


def desired_action(open_events: dict, latest_pm25: float | None) -> tuple[str, str]:
    """From spike events + latest pm25, compute the target fan action.

    Rules (see #10):
      - pm25 >= 25 always suppresses fans (particulate re-suspension risk).
      - No co2/voc events open → off.
      - One of co2/voc open → speed1.
      - Both open, both relative tier → speed2.
      - Both open, either at ceiling tier → speed3.
    """
    if latest_pm25 is not None and latest_pm25 >= PM25_SUPPRESS_THRESHOLD:
        return "off", f"{PM25_SUPPRESS_REASON_PREFIX}{latest_pm25:g} suppresses fans"
    active = [open_events[m] for m in FAN_TRIGGERS if m in open_events]
    if not active:
        return "off", "no co2/voc spike"
    metrics = "+".join(sorted(e["metric"] for e in active))
    if len(active) == 1:
        return "speed1", f"{metrics} elevated"
    if any(e["tier"] == "ceiling" for e in active):
        return "speed3", f"{metrics} at ceiling"
    return "speed2", f"{metrics} elevated"


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


def check_fans(conn, notifier, config: FansConfig, now) -> None:
    """One poll's worth of fan control. No-op when config.enabled is False."""
    if not config.enabled:
        return
    open_events = db.get_open_events(conn)
    latest_pm25 = db.latest_pm25(conn, since=now - PM25_FRESHNESS)
    action, reason = desired_action(open_events, latest_pm25)
    for fan_id in config.fan_ids:
        state = db.get_fan_state(conn, fan_id)
        decision = decide(fan_id, action, reason, state, now)
        if decision is None:
            continue
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
