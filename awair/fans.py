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
RATE_LIMIT = timedelta(seconds=60)
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
        return "off", f"pm25 {latest_pm25:g} suppresses fans"
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

    Returns None if there's no change to make, or if we're inside the 1-cmd/min
    per-fan cooldown from the last command.
    """
    if state["last_action"] == action:
        return None
    if now - state["last_command_at"] < RATE_LIMIT:
        return None
    return MitigationDecision(fan_id=fan_id, action=action, reason=reason)


def actuate(decision: MitigationDecision, config: FansConfig, opener=None) -> bool:
    """Fire-and-forget GET at the NodeMCU. Returns True on 2xx, False otherwise.

    Failure never raises — the caller writes the intended state and moves on
    (soft-partial: a wall control or manual remote can change fan state out of
    band and we won't know, same trade-off Tom accepted on #10).
    """
    open_url = opener or urllib.request.urlopen
    url = f"http://{config.fan_host}/fan/{decision.fan_id}/{decision.action}"
    try:
        with open_url(url, timeout=FAN_CMD_TIMEOUT_SECONDS):
            return True
    except OSError as exc:
        log.warning("fan actuate failed %s: %s", url, exc)
        return False


def check_fans(conn, notifier, config: FansConfig, now) -> None:
    """One poll's worth of fan control. No-op when config.enabled is False."""
    if not config.enabled:
        return
    open_events = db.get_open_events(conn)
    latest_pm25 = db.latest_pm25(conn)
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
        db.upsert_fan_state(
            conn,
            fan_id=fan_id,
            action=decision.action,
            changed_at=now,
            command_at=now,
        )
        if ok:
            notifier.send(
                f"fan {fan_id} -> {decision.action} ({decision.reason})",
                title="Awair fan mitigation",
            )
