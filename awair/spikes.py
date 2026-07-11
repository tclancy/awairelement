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


@dataclass(frozen=True)
class Decision:
    action: str  # open | close | renotify
    tier: str = ""  # relative | ceiling (open); event's tier otherwise
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
    baseline, spread = baseline_spread(values, cfg.floor)
    close_threshold = baseline + (cfg.k / 2) * spread
    recent = values[-CLOSE_CONSECUTIVE:]
    if len(recent) == CLOSE_CONSECUTIVE and all(
        v < close_threshold and v < cfg.ceiling for v in recent
    ):
        return Decision(
            "close", open_event["tier"], values[-1], baseline, close_threshold
        )

    last_notice = open_event["renotified_at"] or open_event["opened_at"]
    if now - last_notice >= RENOTIFY_EVERY:
        return Decision(
            "renotify", open_event["tier"], values[-1], baseline, close_threshold
        )
    return None
