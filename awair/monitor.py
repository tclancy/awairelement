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


def adopt_open_event(health, conn, metric):
    """Seed a starting health tracker from an alert event left open on disk.

    Both trackers keep `alerted` in memory while the row that mirrors it lives
    in the database, so a poller that restarts mid-outage came back with the
    latch clear and opened a *second* row for the same metric (#100).
    `db.get_open_events` returns at most one row per metric and the later one
    wins, so the earlier row then became unreachable: recovery closed the new
    one and the original stayed open on the dashboard forever.

    Called once at startup, after the connection is open and before the poll
    loop. Returns the tier adopted, or None when nothing was open -- which is
    the ordinary case and is why this logs nothing then.

    **It restores the latch, not the run counter**, and that is the whole of
    what it can do: a process that died *before* its threshold opened no row,
    so there is nothing on disk to read. That other half -- the crash-loop
    shorter than the threshold, which is #100's "worse outdoors" argument and
    was out of that issue's Done-when -- is `adopt_health_run` (#124), and both
    are called at startup. Neither is sufficient alone: see that function.

    **What it does to the orphans this bug has already left on disk**, measured
    rather than reasoned: they drain, one per restart-plus-recovery cycle, and
    no prune job is needed (which retires the ticket's option 3). Seeded with
    two open `device` rows, cycle 0 adopts and closes the newer, cycle 1 adopts
    and closes the older, cycle 2 finds nothing. The cost is one spurious
    "recovered" notification per drained row, because adoption sets the latch
    and the first healthy poll then reads as recovery. Bounded at N pings over N
    restarts and then silent forever -- but expect a small burst on the deploy
    that first carries this, and do not read it as a live incident.

    Adopting the tier rather than a bare flag matters because `alerted` is read
    as a tier, not as a boolean -- and because a mid-outage tier change is not
    something either tracker records *within* one process either: `observe`
    only reports a tier while `alerted is None`. So a poller that adopts
    "degraded" and then starts erroring stays "degraded" until it recovers,
    exactly as an un-restarted one would.
    """
    event = db.get_open_events(conn).get(metric)
    if event is None:
        return None
    health.alerted = event["tier"]
    log.info(
        "resuming the open %s alert (%s) opened at %s -- a previous process "
        "left it open",
        metric,
        event["tier"],
        event["opened_at"].isoformat(),
    )
    return event["tier"]


#: Poll intervals of silence after which a persisted health run stops being
#: evidence about now (#124). Two, matching `web._OUTDOOR_CARRY_INTERVALS` --
#: both answer the same question, "how old may an observation be before holding
#: onto it asserts something nobody measured". One interval would discard a run
#: on the ordinary restart this exists to survive; the systemd budget is far
#: smaller than a poll on both units (`RestartSec=10` against 30 s indoors,
#: `RestartSec=30` against 900 s outdoors).
HEALTH_RUN_MAX_AGE_INTERVALS = 2


def health_run_max_age_seconds(interval_seconds):
    """How stale a persisted health run may be and still be adopted (#124).

    A function rather than a module constant for the reason
    `web._outdoor_carry_max_age_seconds` is one: bound at import, `2 * 900` and
    a literal `1800` are indistinguishable to every test that can be written,
    and the claim being made is that this window *tracks* the poll cadence
    rather than restating a number that happens to match it today. Read per
    call, a test can move the cadence and watch the window follow.

    A non-positive cadence yields a non-positive window, so every persisted run
    is stale and adoption becomes a no-op. That is the safe direction -- it
    degrades to the pre-#124 behaviour rather than to a wrong one -- and it is
    reachable only from a deliberately degenerate `AWAIR_POLL_SECONDS=0`.
    """
    return HEALTH_RUN_MAX_AGE_INTERVALS * interval_seconds


def adopt_health_run(health, conn, metric, now, interval_seconds):
    """Seed a starting health tracker's run counter from the last poll on disk.

    The companion to `adopt_open_event`, and the half it cannot do. That one
    restores the `alerted` **latch** from an open `alert_events` row; a process
    that died *before* its threshold opened no row, so there is nothing to
    adopt and the run counter restarts at zero (#124). Outdoors that is a lost
    alert rather than a cosmetic one: `Restart=always` / `RestartSec=30` against
    4 polls x 900 s means a crash-loop never accumulates four consecutive bad
    polls, and a sustained upstream outage is silent.

    Called once at startup, beside `adopt_open_event`. Returns the run length
    adopted, or 0 -- which covers all three ordinary cases (nothing persisted,
    a healthy run, a run too old to be evidence) because none of them changes
    the tracker.

    **Both adoptions are needed and neither is sufficient.** Restoring the run
    without the latch makes every restart past the threshold open another row,
    since the run is already there and nothing says it has been announced.
    Restoring the latch without the run is `main` today.
    """
    state = db.get_health_state(conn, metric)
    if state is None:
        return 0
    age = (now - state["observed_at"]).total_seconds()
    max_age = health_run_max_age_seconds(interval_seconds)
    if age > max_age:
        log.info(
            "discarding the persisted %s health run (%s x%d): last observed %.0fs "
            "ago, past the %.0fs window -- nothing was watching in between",
            metric,
            state["last_status"],
            state["run_length"],
            age,
            max_age,
        )
        return 0
    adopted = health.restore(state["last_status"], state["run_length"])
    if adopted:
        log.info(
            "resuming a run of %d %s %s poll(s) from a previous process",
            adopted,
            metric,
            state["last_status"],
        )
    return adopted


def record_health_run(health, conn, metric, now):
    """Persist this poller's run so the next process can resume it (#124).

    Called after `observe`, every poll. Returns whether it wrote.

    **It skips a run it has already written**, which is what keeps the ticket's
    stated cost ("it makes every poll a write") from being true. The run moves
    on every non-inserting poll, so an outage does write per poll -- those are
    the polls that matter, and on the indoor poller `"error"` and `"duplicate"`
    write nothing at all today. Steady health settles on one unchanged row and
    then writes nothing, forever.

    Skipping leaves `observed_at` ageing on that healthy row, which is
    deliberate and harmless: the staleness rule discards it, and adopting a run
    of zero is indistinguishable from not adopting.
    """
    snapshot = health.snapshot()
    if snapshot == health.persisted:
        return False
    db.upsert_health_state(conn, metric, snapshot[0], snapshot[1], now)
    health.persisted = snapshot
    return True


class DeviceHealth:
    """Consecutive-status tracker for the two device failure modes.

    'error' = fetch failed; 'duplicate' = HTTP 200 but device timestamp
    unchanged (the wedged-but-serving failure mode). Either one sustained
    for `threshold` polls is an alert; any fresh insert is recovery.
    """

    #: The `alert_events.metric` this tracker's handler opens under. Named here
    #: rather than spelled at each call site because a divergence between
    #: `handle_device_health` and `main`'s `adopt_open_event` would be a silent
    #: no-op -- adoption would find nothing and the #100 bug would be back.
    METRIC: ClassVar[str] = "device"

    #: The one status that is health. `observe` still reaches its healthy branch
    #: through a bare `else` -- that is safe here and only here, because the
    #: indoor `poll_once` returns exactly three statuses (see `OutdoorHealth`,
    #: where a fourth made the same `else` a defect). Named so `snapshot` has
    #: something to say when there is no run, rather than a bare literal.
    HEALTHY: ClassVar[str] = "inserted"

    def __init__(self, threshold=10):
        self.threshold = threshold
        self.errors = 0
        self.duplicates = 0
        self.alerted = None  # None | "unreachable" | "stale"
        #: The snapshot `record_health_run` last wrote, so an unchanged run is
        #: not re-written. None until this process has written or adopted one:
        #: seeding it with the healthy no-op instead would let a *stale* row
        #: left by an earlier process survive a healthy poller indefinitely,
        #: because every poll would match the seed and none would correct the
        #: record. One write per process start buys that invariant back.
        self.persisted = None

    def snapshot(self):
        """`(last_status, run_length)` -- the run a restart would resume (#124).

        `last_status` is *derived* rather than stored. The two counters are
        mutually exclusive by construction (each branch of `observe` zeroes the
        other), so a non-zero one names the status on its own, and this class's
        documented absence of a `last_status` field survives the feature.
        """
        if self.errors:
            return ("error", self.errors)
        if self.duplicates:
            return ("duplicate", self.duplicates)
        return (self.HEALTHY, 0)

    def restore(self, last_status, run_length):
        """Put a persisted run back on the counter its status names.

        Restoring onto the wrong counter would be worse than not restoring at
        all: it would announce `stale` for a device that is unreachable, which
        sends a human to the wrong box. Anything that is not a failure status
        restores nothing, which is the same no-op as a fresh tracker.
        """
        if last_status not in ("error", "duplicate"):
            return 0
        self.errors = run_length if last_status == "error" else 0
        self.duplicates = run_length if last_status == "duplicate" else 0
        self.persisted = (last_status, run_length)
        return run_length

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

    #: The `alert_events.metric` this tracker's handler opens under. Not
    #: `DeviceHealth.METRIC`: the two pollers are separate processes against one
    #: DB, and a shared key means an outdoor recovery closes the indoor poller's
    #: open row and sends a false all-clear (#94).
    METRIC: ClassVar[str] = "outdoor"

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
        #: The newest *classified* status, stored rather than derived (#124).
        #: `DeviceHealth` can derive its own from which counter is non-zero;
        #: this one cannot, because `unhealthy` deliberately counts a mixed run
        #: and one integer cannot say which tier it ended in. Only ever
        #: `HEALTHY` or a `TIERS` key -- an unrecognised status is not evidence
        #: and must not overwrite the last thing that was.
        self.last_status = self.HEALTHY
        #: See `DeviceHealth.persisted`.
        self.persisted = None

    def snapshot(self):
        """`(last_status, run_length)` -- the run a restart would resume (#124)."""
        return (self.last_status, self.unhealthy)

    def restore(self, last_status, run_length):
        """Put a persisted run back, or decline to.

        Only a `TIERS` status carries a run. `HEALTHY` restores nothing because
        there is nothing to restore, and an unrecognised status restores nothing
        for the same reason `observe` ignores one: it is not evidence either way,
        and this class exists because a fail-open branch once read a fourth
        status as health.
        """
        if last_status not in self.TIERS:
            return 0
        self.unhealthy = run_length
        self.last_status = last_status
        self.persisted = (last_status, run_length)
        return run_length

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
            self.last_status = status
            if self.alerted is not None:
                self.alerted = None
                return "recovered"
            return None
        if status not in self.TIERS:
            return None
        self.unhealthy += 1
        self.last_status = status
        if self.unhealthy >= self.threshold and self.alerted is None:
            self.alerted = self.TIERS[status]
            return self.alerted
        return None
