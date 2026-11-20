"""The feeds this app records, and whether each is still recording (#158).

The deploy health check for awairelement used to be the dashboard page. That
proves the web process is up and the database opens, and says nothing at all
about the four processes that put readings in it: every poller could have
been dead for a week and `GET /` would still answer 200 with a chart of a
flat line. This module is the missing half -- one entry per feed, the time of
its newest stored reading, and a verdict.

**It reads the database and nothing else.** No device, no bridge, no
Open-Meteo. That is not an optimisation, it is the point: a health check that
makes a network call cannot distinguish "the feed stopped recording" from "the
check could not reach the thing it was asking about", and it fails during
exactly the outage you most want an answer during. Everything here is
answerable from rows already on disk.

**Staleness is judged per feed, against that feed's own cadence.** The four
pollers run 30x apart at the extremes -- 30 s indoors, 900 s outdoors -- so one
wall-clock threshold for all of them is either blind indoors or permanently
red outdoors. Each feed's `poll_seconds` is the poller's own reader, so the
derivation is observable: move `AWAIR_OUTDOOR_POLL_SECONDS` and this module's
outdoor window follows, which is the only way to tell a derivation from a
coincidence while the number happens to be 900.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from awair import basement, db, minisplit, outdoor, poller

#: How many polls a feed may miss in a row before `/health` calls it stale.
#:
#: One count across every feed, multiplied by four different cadences, which is
#: what makes the four thresholds differ without four numbers to keep in step.
#:
#: Six, and the repo's own numbers bracket it on both sides. Below: the
#: carry-forward windows are **two** intervals (`web._OUTDOOR_CARRY_INTERVALS`,
#: `monitor.HEALTH_RUN_MAX_AGE_INTERVALS`) -- that is the floor for "this
#: observation is still evidence about now", and a verdict that can fail a
#: deploy needs more margin than a gap in a chart. Above: the pollers' own
#: alerting fires at **ten** indoor polls (`monitor.DeviceHealth`) and **four**
#: outdoor ones (`monitor.OutdoorHealth.DEFAULT_THRESHOLD`).
#:
#: Six is also what the mini split needs. `minisplit.MAX_REPORT_WINDOW_SECONDS`
#: is 800, so the bridge can legitimately leave 800 s between two rows at a
#: 300 s cadence; six polls is 1800 s, which clears that twice over. Two or
#: three would call a healthy bridge dead.
#:
#: Tunable without a deploy of this repo, for the same reason
#: `AWAIR_OUTDOOR_HEALTH_POLLS` is: if this flaps in practice the fix should
#: not need a code change. Read per call, so a test can move it.
DEFAULT_STALE_AFTER_POLLS = 6


def stale_after_polls() -> int:
    """How many missed polls make a feed stale, per `AWAIR_HEALTH_STALE_POLLS`."""
    return int(os.environ.get("AWAIR_HEALTH_STALE_POLLS", DEFAULT_STALE_AFTER_POLLS))


@dataclass(frozen=True)
class Feed:
    """One recorded feed: where its rows land, and how fast they should arrive.

    `poll_seconds` is a *callable*, not a number, and holding the function
    rather than its result is the whole design. Called at import, an env
    override set in `~/.config/awairelement/environment` after the web worker
    booted would be ignored, and -- worse for the reader -- `6 * 900` and a
    literal `5400` would be indistinguishable to every test anyone could
    write. Held as the poller's own function, the cadence has exactly one
    definition and `/health` demonstrably reads it from there.

    `head` is NULL for every feed but the mini split's, whose rows share one
    table keyed `(head, ts)`.
    """

    name: str
    label: str
    table: str
    head: str | None
    poll_seconds: Callable[[], int]

    def stale_after_seconds(self) -> int:
        """How old this feed's newest reading may be and still count as fresh."""
        return stale_after_polls() * self.poll_seconds()


#: Every feed the app records, in the order `/health` publishes them.
#:
#: The mini split's heads are **derived from the poller's own enumeration**
#: rather than listed here. `minisplit.zones` is the function that decides what
#: a `/status` body contains a head for, so asking it is how a third head
#: appears on `/health` without anybody remembering to come back to this file.
#: It is passed an empty body because only the head *names* are wanted, and
#: those are a property of the function rather than of any payload.
FEEDS = (
    Feed("awair", "Awair Element", "readings", None, poller.poll_seconds),
    Feed(
        "basement",
        "Basement sensor",
        "basement_readings",
        None,
        basement.poll_seconds,
    ),
    Feed(
        "outdoor",
        "Outdoor weather",
        "outdoor_readings",
        None,
        outdoor.poll_seconds,
    ),
    *(
        Feed(
            f"minisplit-{head}",
            f"Mini split ({head})",
            "minisplit_readings",
            head,
            minisplit.poll_seconds,
        )
        for head, _ in minisplit.zones({})
    ),
)


def _z(moment: datetime) -> str:
    """`moment` in the one UTC spelling this app publishes clocks in.

    `db.iso_z` formats whatever wall time it is handed and stamps a `Z` on it,
    which is correct for its callers -- they are all already UTC -- and would
    silently mislabel an offset timestamp here. `received_at` is written
    `+00:00` by all four pollers, so the conversion is a no-op on every row in
    the database today; it is here so that a hand-edited or restored row with a
    real offset is published as the instant it denotes rather than as its own
    wall clock wearing a `Z`.
    """
    return db.iso_z(moment.astimezone(UTC))


def is_fresh(age_seconds: float | None, stale_after_seconds: float) -> bool:
    """Is a reading of this age evidence that the feed is still recording?

    No rows at all is not fresh. That is the case the dashboard check could
    never fail on and the one most worth catching: a poller that has never
    run, or a database restored without a table, reads as a feed that records
    nothing rather than as a feed that is merely behind.

    **Bounded in both directions**, for `monitor.adopt_health_run`'s reason. A
    bare `age <= limit` calls every *negative* age fresh, so a box that comes
    up behind real time -- no RTC, a restored clock, a timesyncd correction
    still pending -- reads every ancient row as newer than now and reports a
    dead feed green, forever, with nothing in the payload to say so. Ordinary
    NTP slew is seconds and stays well inside the window either way.
    """
    if age_seconds is None:
        return False
    return -stale_after_seconds <= age_seconds <= stale_after_seconds


def feed_status(conn, feed: Feed, now: datetime) -> dict:
    """One feed's entry in the `/health` payload.

    Publishes the threshold and the cadence it came from beside the verdict,
    so a 503 can be read without the source: "19 minutes old, stale after 18"
    is actionable where a bare `"fresh": false` sends you to this file.

    The timestamp is `db.iso_z`-spelled, the one UTC spelling with a `Z` that
    every other clock this app publishes uses (`web._iso_utc` lands on the
    same shape from the other direction). Age is computed from the parsed
    datetime rather than the string, and rounded only for the payload.
    """
    newest = db.newest_received_at(conn, feed.table, feed.head)
    stale_after = feed.stale_after_seconds()
    age = None if newest is None else (now - newest).total_seconds()
    return {
        "name": feed.name,
        "label": feed.label,
        "newest_reading_at": None if newest is None else _z(newest),
        "age_seconds": None if age is None else round(age, 1),
        "poll_seconds": feed.poll_seconds(),
        "stale_after_seconds": stale_after,
        "fresh": is_fresh(age, stale_after),
    }


def report(conn, now: datetime) -> dict:
    """The whole `/health` payload. `payload["stale"]` decides the status code.

    `stale` is a top-level list of names rather than something a caller has to
    reassemble from `feeds`, because it is what the check's one line of output
    should be able to say: a deploy that fails wants "basement, minisplit-
    bedroom" in the failure, not a JSON document to walk.

    Takes `now` rather than reading the clock, so every feed in one response is
    judged against one instant and a test can place that instant.
    """
    statuses = [feed_status(conn, feed, now) for feed in FEEDS]
    stale = [status["name"] for status in statuses if not status["fresh"]]
    return {
        "status": "stale" if stale else "ok",
        "checked_at": _z(now),
        "stale": stale,
        "feeds": statuses,
    }
