"""The basement watch: warmth in the basement that the weather cannot explain.

A pure derivation over two series this database already holds — the basement
ESPHome sensor (`awair.basement`) and the Open-Meteo outdoor feed
(`awair.outdoor`). No HTTP, no SQL, no Flask: hand it two row lists and a
clock and it answers with a `Watch`. `awair.web` owns the queries and the
display-unit conversion, the same split `awair.series` keeps.

**What it is for.** parsons-pulse #205: *"If the basement is above 62 degrees
on a day where the highest outdoor temperature is below that, note that as a
yellow on temperature card."* A basement that is warm while it is cold outside
is warm for some reason inside the house — a dehumidifier that never cycles
off, a heating zone calling with nobody asking it to, a freezer dumping heat
into a closed room. The watch's whole job is to notice that the usual
explanation is unavailable.

**It reports; it does not instruct.** There is no ntfy here, no alert event, no
`fan_events` row — nothing acts on it. It is a badge on the temp card, and Tom
decides what it means. That is deliberate and is the one rule this module has
to keep: the only mechanisms in this app that *do* something (the **spike**
ceilings, **fan mitigation**) each earn it by isolating a cause, and this one
isolates nothing. It says "this is not the weather" and stops.

**Three judgment calls, each reversible by one constant.**

1. *Per reading, not per window mean* (#205 did not say). A mean over an
   18-hour window answers a question nobody asked: the window is "when the
   basement is not in use", not a measurement interval, and averaging across
   it would let two hours at 66 °F hide inside sixteen at 60 °F. There is no
   constant for this one — it is the shape of `watch`, which walks readings.
2. *The outdoor high comes from a trailing 24 hours, not from the calendar
   day* (`ATTRIBUTION_LOOKBACK`). See the module note below; this is the only
   call that changes the answer on data we actually have.
3. *The attribution window must be covered* (`MAX_ATTRIBUTION_GAP`). An
   outdoor poller outage must not manufacture a yellow — see
   `attributable_high`.

**Why a trailing window and not "today".** `/api/outdoor-today` already
publishes extremes since local midnight, so the calendar-day reading of #205
was the cheap one. It is also wrong here, and the record says so rather than
an argument. Measured on the homelab database on 2026-11-14, over the whole
basement record (2026-10-30 → 2026-11-14, 22,300 readings):

* 898 in-**watch window** readings were above 62 °F, on two days only — Fri
  2026-11-06 (479) and Sat 2026-11-07 00:00–06:59 (419).
* A calendar-day basis flags **all 419** of the Saturday readings. Saturday's
  own outdoor high was 58.5 °F, so "the warmth of the day" cannot account for
  a 62.4 °F basement.
* The three days before it ran 76.5 °F, 72.5 °F and 67.5 °F, and Friday — the
  other 479 readings — reached 67.5 °F. The basement was warm on Saturday
  morning because the house had just been through a warm spell. That is
  exactly the attribution #205 asks us to make, and a calendar-day basis
  cannot make it: at 02:00 "the day so far" is two hours of night.
* A trailing-24-hour basis flags **0 of the 898**, because the lookback from
  every Saturday-morning reading reaches back into Friday afternoon.

So on the only episode in the record the two bases disagree about, the
calendar day is confidently wrong and the trailing window is right. The cost
is named rather than hidden: the watch has therefore **never fired on real
data**, and its first firing is untested against the house. Consider the
badge's first appearance a thing to go and look at, not a thing to trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from awair import basement, outdoor

# Above this the basement is warm (#205). Fahrenheit because that is the unit
# Tom stated the rule in and the unit the dashboard renders; the comparison
# runs in Celsius because every temperature column in this database is
# Celsius.
WARM_ABOVE_F = 62.0

# Converted here rather than through `units.to_celsius`, which rounds to two
# decimals and would put the boundary at 16.67 °C = 62.006 °F. Six
# thousandths of a degree does not matter to a basement and does matter to a
# threshold: a constant that is not the number it claims to be is the kind of
# thing that gets re-derived three times and believed each time.
WARM_ABOVE_C = (WARM_ABOVE_F - 32) * 5 / 9

# How far back the watch looks for an outdoor high that could account for the
# basement's warmth. See the module docstring for the measurement behind the
# choice of a trailing window over the calendar day; 24 hours is the smallest
# window that always contains a full afternoon, whatever hour of the
# **watch window** a reading falls in.
ATTRIBUTION_LOOKBACK = timedelta(hours=24)

# The widest hole the attribution window may carry and still be believed.
#
# The failure this exists to prevent is specific: `max` over a window with the
# afternoon missing returns the night's high, which is always below the
# threshold in heating season, so an outdoor-poller outage would raise a
# yellow about the house. The direction matters — a missing observation can
# only lower the observed high, and a lower high can only *add* a flag.
#
# Three hours, which at Open-Meteo's quarter-hourly cadence is eleven
# consecutive misses: a gap of exactly 3 h spans twelve 900 s intervals and so
# has eleven absent observations between the two present ones. The bound is
# inclusive, so eleven is believed and twelve is refused. The bound is on where the peak could hide rather than on
# how much data arrived: this house's outdoor high is a broad afternoon
# plateau, not a spike, so a gap narrower than this cannot move the day's
# maximum far. A judgment, and the one to revisit if the watch ever fires
# during an outdoor outage.
MAX_ATTRIBUTION_GAP = timedelta(hours=3)

# The local clock hours the watch applies, as (from, until) in a 24-hour
# clock. `from > until` wraps midnight.
#
# Tom's own words on #205: "The basement is almost never in use and definitely
# not during that window on weekdays. Weekends we could narrow it to
# 12am-7am." So this is a statement about when the room is unoccupied, not a
# measurement interval — a person in the basement with the lights and a
# space heater on is a warm basement with a known cause, and the watch has
# nothing to say about it.
WEEKDAY_WINDOW = (23, 17)
WEEKEND_WINDOW = (0, 7)

# How many missed basement polls make the newest reading too old to carry a
# verdict. Ten, against a 60 s publish: generous enough that a restart or a
# slow fetch is not an outage, tight enough that a flag cannot stand for an
# hour on a sensor that has stopped answering.
STALE_AFTER_POLLS = 10

# What the watch can be. `UNKNOWN` is not an error and not a third colour on
# the card — the dashboard renders it as no badge, the same as `CLEAR`. It is
# here so the payload can tell "the basement is fine" apart from "we have no
# idea", which are the same silence to a reader and different facts to a test.
ON = "on"
CLEAR = "clear"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Watch:
    """The watch's state, and the two numbers behind it.

    `basement` and `outdoor_high` are Celsius, the units they are stored in;
    `awair.web` converts at the boundary like everything else. `since` is the
    epoch second of the reading that raised the watch — the *first* one of the
    uncleared run, so the badge can say how long this has been true rather
    than restating that it is true now.
    """

    status: str
    since: float | None = None
    basement: float | None = None
    outdoor_high: float | None = None


def stale_after_seconds() -> float:
    """How old the newest basement reading may be and still carry a verdict.

    Read per call rather than bound at import, for the reason
    `web._outdoor_carry_max_age_seconds` is: bound once, `10 * 60` and a
    literal `600` are the same number to every test that can be written, and
    what is being claimed is that this tracks the poller's cadence rather than
    restating it.
    """
    return STALE_AFTER_POLLS * basement.DEFAULT_POLL_SECONDS


def watch_window(moment: datetime) -> tuple[int, int]:
    """Which window applies to `moment`, by its own day of the week.

    Classified per timestamp, which is what makes Friday 23:30 a weekday hour
    and Saturday 02:00 a weekend one — the two halves of one continuous night
    under two different rules, which is what #205 asks for. It also leaves
    Sunday 23:00–24:00 outside every window, because Sunday is a weekend day
    and the weekend window has already closed. That is a consequence rather
    than an intent; it costs one hour a week and no reading in the record
    falls in it.
    """
    return WEEKEND_WINDOW if moment.weekday() >= 5 else WEEKDAY_WINDOW


def in_watch_window(moment: datetime) -> bool:
    """Whether the basement is expected to be unoccupied at `moment`."""
    opens, closes = watch_window(moment)
    if opens > closes:
        return moment.hour >= opens or moment.hour < closes
    return opens <= moment.hour < closes


def local(epoch: float) -> datetime:
    """An epoch second as naive local time.

    The web process's own zone, via `datetime.fromtimestamp` — the same clock
    `web._since_for` resolves "today" against, and deliberately not
    `AWAIR_TZ`, which only `awair.solar` reads. The homelab box is
    America/New_York and sets no `AWAIR_TZ`, so this is Tom's wall clock,
    which is the clock "11pm" was said in.
    """
    return datetime.fromtimestamp(epoch)


def _covered(stamps: list[float]) -> bool:
    """Whether consecutive stamps are close enough to trust a max over them.

    `stamps` includes the window's own two edges, so a window whose
    observations all arrived in its first hour fails here rather than
    answering about the hour it has.
    """
    gap = MAX_ATTRIBUTION_GAP.total_seconds()
    return all(b - a <= gap for a, b in pairwise(stamps))


def attributable_high(outdoor_rows, at: float) -> float | None:
    """The highest outdoor Celsius that could account for warmth at `at`.

    `outdoor_rows` is `db.outdoor_readings_since(conn, ("temp",), ...)` —
    `[(epoch_seconds, celsius)]`, ascending. Returns None when the lookback is
    not covered well enough to believe a maximum over it, which is the one
    answer that must never be confused with a low high: None means *no
    verdict*, and `watch` declines to flag on it. An outdoor outage is a fact
    about the poller, never evidence about the house.
    """
    opens = at - ATTRIBUTION_LOOKBACK.total_seconds()
    window = [(ts, c) for ts, c in outdoor_rows if opens <= ts <= at and c is not None]
    if not window:
        return None
    if not _covered([opens, *(ts for ts, _ in window), at]):
        return None
    return max(c for _, c in window)


# What one warm reading says about itself. Four outcomes rather than a
# boolean, because `watch` has to tell "the weather explains this" from "we
# cannot tell" — folding them together is how an outdoor outage ends up
# published as CLEAR, which is the opposite claim.
TRIP = "trip"
ATTRIBUTED = "attributed"
UNCERTAIN = "uncertain"
OUTSIDE = "outside"


def _verdict_of(row, outdoor_rows) -> tuple[str, float | None]:
    """What one warm reading is, and the outdoor high behind a `TRIP`.

    The float comes back with `TRIP` so the caller can publish the number that
    justified the flag rather than re-deriving it; every other outcome has no
    number to publish.

    Takes `row` from `_uncleared_run`, so its temperature is a number and it
    is already above `WARM_ABOVE_C`; there is deliberately no second check for
    that here. A guard for a condition the only caller cannot produce is a
    line no test can reach, which is worse than the precondition it documents.

    "Below that" is strict (#205's own word), so an outdoor high of exactly
    62 °F accounts for a 62.1 °F basement. The warm and clear thresholds are
    complementary — above 62 is warm, at-or-below 62 clears — so there is no
    dead band on the basement side.
    """
    at, _ = row
    if not in_watch_window(local(at)):
        return OUTSIDE, None
    high = attributable_high(outdoor_rows, at)
    if high is None:
        return UNCERTAIN, None
    if high >= WARM_ABOVE_C:
        return ATTRIBUTED, None
    return TRIP, high


def _uncleared_run(readings, horizon: float) -> tuple[list, bool]:
    """The newest readings back to the last clear one, and whether it is whole.

    This is what makes the watch a latch rather than a re-decision: #205 says
    yellow "clears once the temp drops to 62 or below", so a trip inside the
    **watch window** keeps standing through the afternoon that follows it,
    until the basement actually comes back down. Derived from the rows rather
    than stored, so there is no state to get stuck and nothing to migrate.

    **The second return value is the honesty half, and it is why `horizon` is
    an argument.** A stateless latch can only see as far back as the rows it
    was handed, so a basement that has been warm for longer than
    `history_lookback()` has its original trip outside the evidence entirely.
    Left unsaid, that reads as a watch that cleared — with no reading ever at
    or below 62 °F, which is precisely what #205 says clearing means. So a run
    that reaches the oldest row the query *could* have returned is reported as
    truncated, and `watch` declines to call it either way.

    The horizon test is "the data starts where the query did", not "no clear
    reading was found": a sensor installed two hours ago has no clear reading
    either, and its run is entirely visible. One stale-bound of slack, because
    the first row after a `since` lands a poll or two later.
    """
    run = []
    for reading in reversed(readings):
        if reading[1] <= WARM_ABOVE_C:
            return list(reversed(run)), False
        run.append(reading)
    reaches_horizon = bool(run) and readings[0][0] <= horizon + stale_after_seconds()
    return list(reversed(run)), reaches_horizon


def _first_trip(run, outdoor_rows) -> tuple[tuple | None, float | None, bool]:
    """The earliest trip in the run, its outdoor high, and any uncertainty.

    Returns at the first trip, so a long standing run costs one verdict per
    reading only while it is NOT flagging. The third value is sticky across
    the readings scanned: one warm reading we could not judge is enough to
    stop the caller publishing CLEAR, which is a claim about the house.
    """
    uncertain = False
    for reading in run:
        verdict, high = _verdict_of(reading, outdoor_rows)
        if verdict == TRIP:
            return reading, high, uncertain
        uncertain = uncertain or verdict == UNCERTAIN
    return None, None, uncertain


def watch(basement_rows, outdoor_rows, now: float) -> Watch:
    """The basement watch, from the two series and a clock.

    `basement_rows` is `db.basement_readings_since(...)` and `outdoor_rows`
    `db.outdoor_readings_since(conn, ("temp",), ...)`, both
    `[(epoch_seconds, celsius)]` ascending. `now` is an epoch second.

    Ordered cascade, first match wins: silence before colour, so a sensor that
    has stopped reporting can never leave a flag standing on its last warm
    reading.
    """
    readings = [(ts, c) for ts, c in basement_rows if c is not None]
    if not readings:
        return Watch(UNKNOWN)
    at, celsius = readings[-1]
    if now - at > stale_after_seconds():
        return Watch(UNKNOWN, basement=celsius)
    # No `if celsius <= WARM_ABOVE_C: return CLEAR` fast path here, and its
    # absence is deliberate. `_uncleared_run` breaks on exactly that
    # comparison, so a cool newest reading already yields an empty run and
    # falls through to the same `Watch(CLEAR, basement=celsius)` below. The
    # fast path was written first and a mutation round found it: flipping its
    # `<=` to `<` changed nothing observable, because the other guard covers
    # the boundary. A branch whose mutation cannot be detected is a branch no
    # test can hold, and two owners of one threshold is how they drift apart.
    run, truncated = _uncleared_run(readings, now - history_lookback().total_seconds())
    trip, high, uncertain = _first_trip(run, outdoor_rows)
    if trip is not None:
        # `since` only when the run's start is inside the evidence: on a
        # truncated run the earliest trip we can SEE marches forward one
        # reading at a time as the window slides, so publishing it would make
        # a two-day-old condition read "since 01:30" and then "since 05:30"
        # the next morning. No stamp is the true answer.
        return Watch(
            ON,
            since=None if truncated else trip[0],
            basement=celsius,
            outdoor_high=high,
        )
    if truncated or uncertain:
        # Not CLEAR. "The weather explains every warm reading" and "we could
        # not judge one of them" are different facts, and only the first is a
        # statement about the house.
        return Watch(UNKNOWN, basement=celsius)
    return Watch(CLEAR, basement=celsius)


def history_lookback() -> timedelta:
    """How much basement history `watch` reads, and so how long the latch holds.

    The longest **watch window** plus one attribution lookback — 42 hours: a
    trip can be as old as the start of the window it happened in, and the
    oldest window a reading can belong to is the weekday one, which is 18
    hours wide. Shorter than this and a flag raised at 23:30 would clear
    itself at 17:30 the next day with the basement still warm — not because it
    cooled, but because the trip fell off the back of the query.

    **It is also a hard bound on the latch, and that bound is a real cost.**
    #205 says the yellow clears when the basement comes back to 62 °F, and
    nothing else. A stateless derivation cannot honour that indefinitely: a
    basement continuously above 62 °F for longer than this has its original
    trip outside every row the query returns. The watch does not pretend
    otherwise — `_uncleared_run` reports such a run as truncated and `watch`
    answers `UNKNOWN`, which renders as no badge but publishes as a different
    fact from `CLEAR`. Raising the bound is not free either: `watch` evaluates
    `attributable_high` per in-window reading, so the work grows with the
    square of the horizon. Storing the trip is the fix if the truncated case
    turns out to be common; it has never been observed, because the watch has
    never fired.
    """
    opens, closes = WEEKDAY_WINDOW
    widest = timedelta(hours=(24 - opens) + closes)
    return widest + ATTRIBUTION_LOOKBACK


def attribution_lookback_seconds() -> float:
    """How much outdoor history `watch` needs, with room for the edge check.

    One lookback for the oldest trip the latch can still be holding, plus one
    more for that trip's own attribution window. Derived rather than written
    down so `MAX_ATTRIBUTION_GAP`'s edge test cannot start failing because the
    query stopped reaching far enough back — which would read as an outdoor
    outage rather than as a short query.
    """
    return (
        history_lookback() + ATTRIBUTION_LOOKBACK
    ).total_seconds() + outdoor.SOURCE_INTERVAL_SECONDS
