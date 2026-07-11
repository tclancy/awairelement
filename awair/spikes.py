"""Spike detection: pure functions over reading history.

All state comes in as arguments (history rows, the open event) and the
verdict goes out as a Decision — nothing here touches the DB or network,
which is what makes the hysteresis/re-arm rules unit-testable.
"""

from dataclasses import dataclass
from datetime import timedelta
from statistics import median


@dataclass(frozen=True)
class MetricConfig:
    name: str
    floor: float  # minimum spread — MAD collapses to ~0 during stable periods
    ceiling: float  # tier-2 absolute threshold
    k: float = 6.0  # tier-1 opens at baseline + k * spread
    m: int = 4  # consecutive polls required to open tier-1


METRICS = {
    "co2": MetricConfig("co2", floor=50.0, ceiling=1200.0),
    "voc": MetricConfig("voc", floor=50.0, ceiling=1000.0),
    "pm25": MetricConfig("pm25", floor=4.0, ceiling=35.0),
}

CEILING_CONSECUTIVE = 2  # never page on a single sample
CLOSE_CONSECUTIVE = 20  # ~10 min at 30s cadence, below BOTH thresholds
MIN_HISTORY = timedelta(hours=6)  # cold start: tier-1 off until this much data
RENOTIFY_EVERY = timedelta(hours=12)
ESCALATION_FACTOR = 2.0  # page when the level doubles since the last notice
ESCALATION_WINDOW = 4  # median over this many polls — one blip must not page


@dataclass(frozen=True)
class Decision:
    action: str  # open | close | escalate | renotify
    tier: str = ""  # relative | ceiling; on escalate, the (possibly new) tier
    value: float = 0.0
    baseline: float = 0.0
    threshold: float = 0.0


def baseline_spread(values, floor):
    """Trailing-window median and MAD-with-floor spread."""
    med = median(values)
    mad = median(abs(v - med) for v in values)
    return med, max(mad, floor)


def evaluate(cfg, history, open_event, now):
    """One detection step for one metric.

    history: [(datetime, value)] ascending, trailing 24h, nulls excluded.
    open_event: dict with tier/opened_at/renotified_at, or None.
    Returns a Decision or None.
    """
    values = [v for _, v in history]
    if not values:
        return None
    if open_event is None:
        return _maybe_open(cfg, history, values)
    return _maybe_close_or_renotify(cfg, values, open_event, now)


def _maybe_open(cfg, history, values):
    recent = values[-CEILING_CONSECUTIVE:]
    if len(recent) == CEILING_CONSECUTIVE and all(v > cfg.ceiling for v in recent):
        baseline, _ = baseline_spread(values, cfg.floor)
        return Decision("open", "ceiling", values[-1], baseline, cfg.ceiling)

    span = history[-1][0] - history[0][0]
    if span < MIN_HISTORY or len(values) < cfg.m:
        return None
    baseline, spread = baseline_spread(values, cfg.floor)
    threshold = baseline + cfg.k * spread
    if all(v > threshold for v in values[-cfg.m :]):
        return Decision("open", "relative", values[-1], baseline, threshold)
    return None


def _maybe_close_or_renotify(cfg, values, open_event, now):
    baseline, close_threshold = _close_reference(cfg, values, open_event)
    recent = values[-CLOSE_CONSECUTIVE:]
    if len(recent) == CLOSE_CONSECUTIVE and all(
        v < close_threshold and v < cfg.ceiling for v in recent
    ):
        return Decision(
            "close", open_event["tier"], values[-1], baseline, close_threshold
        )

    escalation = _maybe_escalate(cfg, values, open_event, baseline)
    if escalation is not None:
        return escalation

    last_notice = open_event["renotified_at"] or open_event["opened_at"]
    if now - last_notice >= RENOTIFY_EVERY:
        return Decision(
            "renotify", open_event["tier"], values[-1], baseline, close_threshold
        )
    return None


def _close_reference(cfg, values, open_event):
    """(baseline, close_threshold) frozen at event open.

    Recomputing from trailing history lets a long event contaminate its own
    baseline — the median drifts up to the plateau and the event "closes"
    while air is still far above pre-event levels. The stats stored on the
    event row are the pre-spike truth; rows from before they were stored
    fall back to the old recomputation.
    """
    baseline = open_event.get("baseline")
    threshold = open_event.get("threshold")
    if baseline is None or threshold is None:
        baseline, spread = baseline_spread(values, cfg.floor)
        return baseline, baseline + (cfg.k / 2) * spread
    return baseline, baseline + (threshold - baseline) / 2


def _maybe_escalate(cfg, values, open_event, baseline):
    """Mid-event escalations: relative→ceiling promotion, or a doubling.

    Both re-arm the reference (monitor stores the new notified_value), so
    magnitude escalations ladder — 2x, 4x, 8x each page exactly once.
    """
    recent = values[-CEILING_CONSECUTIVE:]
    if (
        open_event["tier"] == "relative"
        and len(recent) == CEILING_CONSECUTIVE
        and all(v > cfg.ceiling for v in recent)
    ):
        return Decision("escalate", "ceiling", values[-1], baseline, cfg.ceiling)

    reference = open_event.get("notified_value") or open_event.get("peak_value")
    window = values[-ESCALATION_WINDOW:]
    if (
        reference
        and len(window) == ESCALATION_WINDOW
        and median(window) >= ESCALATION_FACTOR * reference
    ):
        return Decision(
            "escalate",
            open_event["tier"],
            values[-1],
            baseline,
            ESCALATION_FACTOR * reference,
        )
    return None
