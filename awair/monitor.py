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
from typing import Any, ClassVar

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


class OutdoorHealth:
    """Consecutive-status tracker for the outdoor poller's three failure modes.

    Deliberately **not** `DeviceHealth` with a different threshold. Outdoor
    `poll_once` returns four statuses where the indoor one returns three, and
    the extra one is `"partial"` — the weather half written, the air-quality
    half not. `DeviceHealth.observe` reaches its healthy branch through a bare
    `else`, so `"partial"` lands there and reads as a successful insert:
    measured on #94, twenty consecutive `"partial"` polls never alert, a single
    `"partial"` zeroes a run of errors, and one after an alert reports
    `"recovered"`. A permanently broken AQ endpoint would therefore be invisible
    to the very signal this class exists to provide, and it is the failure mode
    most likely to persist quietly — a dead *weather* endpoint is the one a
    human notices.

    **The run counted is "consecutive non-inserting polls", not "consecutive
    polls of one status", and that distinction is the whole fix.** A first draft
    counted a separate run per status and zeroed the others on every poll, which
    reproduces the `DeviceHealth` bug one level up: `poll_once` returns
    `"duplicate"` before it returns `"partial"` (`outdoor.py`), so a broken AQ
    endpoint plus a weather half that republishes a stale `current.time` emits
    an alternating `partial`/`duplicate` stream, and under per-status runs sixty
    consecutive useless polls — fifteen hours — alerted zero times. Caught in
    review, and the test that was supposed to pin it asserted the same
    expectation of the buggy tracker and the fixed one.

    The tier reported is the **most recent** failing status, because that is
    what is happening now; a mixed run is still one outage from the operator's
    point of view. The three tiers are kept apart because only one of them is
    actionable here: `unreachable` is our network or our box, while `degraded`
    and `stale` are Open-Meteo publishing badly and nothing on this end can fix
    either. Folding them together would page Tom for an upstream hiccup he
    cannot act on.

    `threshold` counts polls, not wall-clock, because the poll interval is the
    unit that matters — but note the outdoor cadence is 900 s against indoor's
    30 s, so `DeviceHealth`'s default of 10 would be two and a half hours of
    silence here. Four polls is about one hour, which is roughly Open-Meteo's
    publish cycle: one missed publish cannot trip it, a stuck endpoint does.
    """

    #: poll status -> the tier a sustained run ending in it opens.
    TIERS: ClassVar[dict[str, str]] = {
        "error": "unreachable",
        "partial": "degraded",
        "duplicate": "stale",
    }

    #: The only status that counts as health. Anything else is not recovery.
    HEALTHY = "inserted"

    #: ~1 hour at the default 900 s cadence. See the class docstring.
    DEFAULT_THRESHOLD = 4

    def __init__(self, threshold=DEFAULT_THRESHOLD):
        self.threshold = threshold
        self.unhealthy = 0  # consecutive non-inserting polls, of any mix
        self.alerted = None  # None | "unreachable" | "degraded" | "stale"

    def observe(self, status):
        """Fold one poll status in; return a verdict to announce, or None.

        An unrecognised status is ignored rather than treated as health — the
        fail-open `else` is exactly the bug this class was written to avoid, so
        a fifth status added to `poll_once` later must be classified here
        deliberately instead of silently clearing an open alert. Ignored means
        ignored: it neither advances the run nor resets it, because an
        unclassified status is not evidence either way.
        """
        if status == self.HEALTHY:
            self.unhealthy = 0
            if self.alerted is not None:
                self.alerted = None
                return "recovered"
            return None
        if status not in self.TIERS:
            return None
        self.unhealthy += 1
        if self.unhealthy >= self.threshold and self.alerted is None:
            self.alerted = self.TIERS[status]
            return self.alerted
        return None
