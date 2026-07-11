"""Glue between readings, spike detection, alert_events, and ntfy."""

import logging
from datetime import timedelta

from awair import db
from awair.spikes import METRICS, evaluate

log = logging.getLogger("awair.monitor")

PRIORITY = {"ceiling": "high", "relative": "default"}
HISTORY_WINDOW = timedelta(hours=24)


def check_metrics(conn, notifier, now):
    """Run detection for every metric; persist and notify on decisions."""
    open_events = db.get_open_events(conn)
    since = now - HISTORY_WINDOW
    for name, cfg in METRICS.items():
        history = db.metric_history(conn, name, since)
        event = open_events.get(name)
        if event and history:
            db.update_peak(conn, event["id"], history[-1][1])
        decision = evaluate(cfg, history, event, now)
        if decision is None:
            continue
        log.info("%s: %s (%s)", name, decision.action, decision.tier)
        if decision.action == "open":
            notified = notifier.send(
                f"{name.upper()} at {decision.value:g}"
                f" (baseline {decision.baseline:g}, threshold {decision.threshold:g})",
                title=f"{name.upper()} spike",
                priority=PRIORITY[decision.tier],
            )
            db.open_event(
                conn,
                metric=name,
                tier=decision.tier,
                opened_at=now,
                value=decision.value,
                baseline=decision.baseline,
                threshold=decision.threshold,
                notified=notified,
            )
        elif decision.action == "close":
            notified = notifier.send(
                f"{name.upper()} back to {decision.value:g}",
                title=f"{name.upper()} cleared",
            )
            db.close_event(conn, event["id"], closed_at=now, notified=notified)
        elif decision.action == "renotify":
            notifier.send(
                f"{name.upper()} still elevated at {decision.value:g}"
                f" (peak {event['peak_value']:g})",
                title=f"{name.upper()} still elevated",
            )
            db.mark_renotified(conn, event["id"], now)


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
