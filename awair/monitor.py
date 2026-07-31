"""Glue between readings, spike detection, alert_events, and ntfy.

`check_metrics` is the loop; the four things it can decide to do with a metric
(open, close, escalate, renotify) each live in their own `_apply_*` function and
are reached through `_ACTIONS`. They were an elif chain until #57 — the chain
scored grade C on its own, and every branch re-threaded the same seven values
(`conn`, `notifier`, the metric name, the decision, the open event, `now`, the
display unit) through `_fmt` calls by hand. `_Notice` carries that set once so a
handler reads as message-then-persist.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from awair import db, units
from awair.spikes import METRICS, evaluate

log = logging.getLogger("awair.monitor")

PRIORITY = {"ceiling": "high", "relative": "default"}
HISTORY_WINDOW = timedelta(hours=24)


def _fmt(name, value, temp_unit):
    """Format a metric value for a notification message.

    Temp values are converted to the configured display unit and suffixed
    with the symbol; all other metrics render as `%g` unchanged.
    """
    if name == "temp":
        converted = units.from_celsius(value, temp_unit)
        return f"{converted:g}{units.symbol(temp_unit)}"
    return f"{value:g}"


@dataclass(frozen=True)
class _Notice:
    """One metric's decision plus everything needed to announce and persist it.

    `event` is the currently-open event row for this metric, or None when the
    decision is `open` (there is nothing open yet). The other three actions all
    act on an existing event, so they may read it.
    """

    conn: Any
    notifier: Any
    name: str
    decision: Any
    event: dict | None
    now: Any
    temp_unit: str

    @property
    def label(self) -> str:
        """The metric name as it appears in a notification title/body."""
        return self.name.upper()

    def fmt(self, value) -> str:
        """Render a value of *this* metric in the configured display unit."""
        return _fmt(self.name, value, self.temp_unit)


def _apply_open(notice):
    """Announce a newly-detected spike and record the event."""
    notified = notice.notifier.send(
        f"{notice.label} at {notice.fmt(notice.decision.value)}"
        f" (baseline {notice.fmt(notice.decision.baseline)},"
        f" threshold {notice.fmt(notice.decision.threshold)})",
        title=f"{notice.label} spike",
        priority=PRIORITY[notice.decision.tier],
    )
    db.open_event(
        notice.conn,
        metric=notice.name,
        tier=notice.decision.tier,
        opened_at=notice.now,
        value=notice.decision.value,
        baseline=notice.decision.baseline,
        threshold=notice.decision.threshold,
        notified=notified,
    )


def _apply_close(notice):
    """Announce that a metric came back down and close its event."""
    notified = notice.notifier.send(
        f"{notice.label} back to {notice.fmt(notice.decision.value)}",
        title=f"{notice.label} cleared",
    )
    db.close_event(
        notice.conn, notice.event["id"], closed_at=notice.now, notified=notified
    )


def _escalation_detail(notice) -> str:
    """Why this escalation fired — a new ceiling crossing, or a doubling."""
    promoted = notice.decision.tier != notice.event["tier"]
    if promoted:
        return f"crossed the {notice.fmt(notice.decision.threshold)} ceiling"
    return f"doubled since last notice (peak {notice.fmt(notice.event['peak_value'])})"


def _apply_escalate(notice):
    """Page on an open event that got materially worse."""
    notice.notifier.send(
        f"{notice.label} at {notice.fmt(notice.decision.value)}"
        f" — {_escalation_detail(notice)}",
        title=f"{notice.label} escalating",
        priority="high",
    )
    db.escalate_event(
        notice.conn,
        notice.event["id"],
        notice.now,
        value=notice.decision.value,
        tier=notice.decision.tier,
    )


def _apply_renotify(notice):
    """Re-state a long-running event that has neither cleared nor worsened."""
    notice.notifier.send(
        f"{notice.label} still elevated at"
        f" {notice.fmt(notice.decision.value)}"
        f" (peak {notice.fmt(notice.event['peak_value'])})",
        title=f"{notice.label} still elevated",
    )
    db.mark_renotified(
        notice.conn, notice.event["id"], notice.now, value=notice.decision.value
    )


_ACTIONS = {
    "open": _apply_open,
    "close": _apply_close,
    "escalate": _apply_escalate,
    "renotify": _apply_renotify,
}


def _refresh_peak(conn, event, history):
    """Fold the newest sample into an open event's running peak."""
    latest = history[-1][1]
    db.update_peak(conn, event["id"], latest)
    event["peak_value"] = max(event["peak_value"] or latest, latest)


def check_metrics(conn, notifier, now):
    """Run detection for every metric; persist and notify on decisions."""
    open_events = db.get_open_events(conn)
    since = now - HISTORY_WINDOW
    temp_unit = units.get_temperature_unit()
    for name, cfg in METRICS.items():
        history = db.metric_history(conn, name, since)
        event = open_events.get(name)
        if event and history:
            _refresh_peak(conn, event, history)
        decision = evaluate(cfg, history, event, now)
        if decision is None:
            continue
        log.info("%s: %s (%s)", name, decision.action, decision.tier)
        apply = _ACTIONS.get(decision.action)
        if apply is None:
            # The elif chain this replaced dropped an unrecognised action in
            # silence, which is the worst outcome for an alerting path: a
            # decision was made and nobody hears about it. Still a no-op, but
            # a loud one.
            log.warning("%s: no handler for action %r", name, decision.action)
            continue
        apply(_Notice(conn, notifier, name, decision, event, now, temp_unit))


class DeviceHealth:
    """Consecutive-status tracker for the two device failure modes.

    'error' = fetch failed; 'duplicate' = HTTP 200 but device timestamp
    unchanged (the wedged-but-serving failure mode). Either one sustained
    for `threshold` polls is an alert; any fresh insert is recovery.
    """

    def __init__(self, threshold=10):
        self.threshold = threshold
        self.errors = 0
        self.duplicates = 0
        self.alerted = None  # None | "unreachable" | "stale"

    def observe(self, status):
        if status == "error":
            self.errors += 1
            self.duplicates = 0
            if self.errors == self.threshold and self.alerted is None:
                self.alerted = "unreachable"
                return "unreachable"
        elif status == "duplicate":
            self.duplicates += 1
            self.errors = 0
            if self.duplicates == self.threshold and self.alerted is None:
                self.alerted = "stale"
                return "stale"
        else:  # inserted
            self.errors = 0
            self.duplicates = 0
            if self.alerted is not None:
                self.alerted = None
                return "recovered"
        return None
