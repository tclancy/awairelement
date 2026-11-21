"""The main zone's humidity target, and the arithmetic the card needs (#161).

The main zone is the great room: the open room the wood stove is in, which in
winter is the driest part of the house *because* of the stove. #160 wants that
room held at a target through the heating season; this module is the recording
half's share of it -- what the target is at any moment, which sensor speaks
for the zone, and how long the zone has spent below target.

Nothing here changes a humidifier setting. It is all read-side.

**Three sensors, one room.** The Awair, the humidifier and the dining mini
split head are all in the great room, and they do not agree:

- The **Awair** sits high in the room, above the fireplace. It reads warmer
  than living level, and a warmer reading at the same moisture content is a
  *lower* relative humidity -- so this sensor is the pessimistic one, by an
  amount that depends on how hard the stove is going.
- The **humidifier** reads at the floor, which is living level, and is the
  reading the device's own control loop uses.
- The **dining head** reads at head height on a wall.

Which one counts is `AWAIR_MAIN_ZONE_FEED` (see `feed`), and all three are
drawn on the card either way, with their dew points beside each other: in one
open room the three dew points should roughly agree even when the three
relative humidities do not, and a persistent gap between them is worth seeing
-- it means the air in this "one room" is not actually mixing.
"""

import logging
import os
from itertools import pairwise

from awair import db, dewpoint, feeds

log = logging.getLogger("awair.main_zone")

#: Which feed speaks for the main zone. A feed name out of `feeds.FEEDS`, not
#: a table or a column: the names are already the vocabulary `/health`
#: publishes, so "which sensor is the great room" is answered in the same
#: words on both surfaces.
FEED_ENV = "AWAIR_MAIN_ZONE_FEED"

#: The Awair, deliberately, even though it is the sensor with the known bias.
#: It is the longest-running record in the house by a year and it is the one
#: every other humidity number on this dashboard is already read against --
#: and its bias is in the safe direction: holding 40 % on a sensor that reads
#: low means the room is at 40 % or better. The variable exists because that
#: reasoning is worth being able to overturn after a winter of looking at the
#: three traces.
DEFAULT_FEED = "awair"

#: The target, in percent relative humidity, and the outdoor temperature at
#: which it drops. All three are environment-tunable with these defaults,
#: which is what #161 asks for.
#:
#: 40 % is the usual comfort-and-health figure for a heated house. The drop to
#: 35 % when it is very cold out is not a comfort judgement, it is a
#: condensation one: indoor moisture condenses on the coldest surface in the
#: room, which in this house is a window, and the colder it is outside the
#: colder that pane is. Holding 40 % through a -15 C night buys damp sills and
#: eventually mould in the frames.
#:
#: -12 C (10.4 F) for the switch. The conventional table -- it comes off the
#: same guidance that pairs 20 F with 35 % and 0 F with 25 % -- puts 35 % at
#: around 10 F, and 10 F is a temperature this parcel reaches a handful of
#: nights a winter rather than most of them. Stored and compared in CELSIUS
#: because every temperature column in this database is Celsius; the figure
#: in the environment file is a Celsius one, and the README says so.
DEFAULT_TARGET = 40.0
DEFAULT_COLD_TARGET = 35.0
DEFAULT_COLD_OUTDOOR_C = -12.0

TARGET_ENV = "AWAIR_HUMIDITY_TARGET"
COLD_TARGET_ENV = "AWAIR_HUMIDITY_TARGET_COLD"
COLD_OUTDOOR_ENV = "AWAIR_HUMIDITY_COLD_OUTDOOR_C"

#: How many days the "hours below target" figure covers. Seven, and it is
#: fixed rather than following the range buttons for `_basement_watch`'s
#: reason: it is a verdict about the past week, and a figure whose window
#: shrank when somebody pressed "Today" would be a different statistic on each
#: button while keeping the same label.
BELOW_TARGET_DAYS = 7


def _float_env(name, default):
    """A float from the environment, or `default` if unset or unreadable.

    Logs and falls back rather than raising, which is the opposite of
    `units.get_temperature_unit`'s ruling on a bad value -- and deliberately
    so. That one is read once at web startup, where a typo should stop the
    process; these are read per request by a dashboard endpoint, where a typo
    in an environment file would otherwise 500 the whole page rather than
    drawing it with the documented default.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number -- using %s", name, raw, default)
        return default


def target() -> float:
    """The warm-weather target, percent RH, per `AWAIR_HUMIDITY_TARGET`."""
    return _float_env(TARGET_ENV, DEFAULT_TARGET)


def cold_target() -> float:
    """The cold-weather target, per `AWAIR_HUMIDITY_TARGET_COLD`."""
    return _float_env(COLD_TARGET_ENV, DEFAULT_COLD_TARGET)


def cold_outdoor_temp() -> float:
    """The outdoor temperature (C) at or below which the target drops."""
    return _float_env(COLD_OUTDOOR_ENV, DEFAULT_COLD_OUTDOOR_C)


def target_for(outdoor_temp_c) -> float:
    """The target in force when it is `outdoor_temp_c` outside.

    **At or below** the threshold is cold: the threshold is the first
    temperature at which the lower target applies, which is how the guidance
    tables it comes from read.

    An unknown outdoor temperature answers the warm target, not the cold one,
    and that choice is load-bearing: a dead outdoor poller would otherwise
    silently relax the target by five points and a house that had stopped
    being humidified would read as on target. The failure direction here is
    "the chart says we are short of target", which is the one a reader
    investigates.
    """
    if outdoor_temp_c is None:
        return target()
    return cold_target() if outdoor_temp_c <= cold_outdoor_temp() else target()


def feed():
    """The `feeds.Feed` that speaks for the main zone, per `AWAIR_MAIN_ZONE_FEED`.

    An unknown name logs one line and falls back to the default rather than
    raising, for `_float_env`'s reason -- this is read by a dashboard endpoint
    -- and the line names the feeds that exist, because the fix is to pick one
    of them.
    """
    wanted = os.environ.get(FEED_ENV) or DEFAULT_FEED
    for candidate in feeds.FEEDS:
        if candidate.name == wanted:
            return candidate
    log.warning(
        "%s=%r is not a feed (have: %s) -- using %s",
        FEED_ENV,
        wanted,
        [f.name for f in feeds.FEEDS],
        DEFAULT_FEED,
    )
    return next(f for f in feeds.FEEDS if f.name == DEFAULT_FEED)


def humidity_rows(conn, one_feed, since):
    """`[(epoch, temp_c, humid)]` for one feed, at or after `since`.

    One reader for every feed, so the card's three traces and the dew point
    card's six lines are all read the same way and a seventh feed needs no
    new query. The temperature travels with the humidity because the dew point
    needs both and they are on one row -- reading them in two queries would
    scan the same minutes twice and could pair a temperature with a humidity
    from a different row.
    """
    return db.feed_readings_since(
        conn, one_feed.table, one_feed.head, ("temp", "humid"), since
    )


def dew_points(rows):
    """`dewpoint.series` over `humidity_rows` output. Nulls preserved."""
    return dewpoint.series(rows)


def latest(points):
    """The newest non-null value in `[(epoch, value)]`, or None.

    Reads backwards, so a long trace costs one comparison rather than a sort.
    `points` is in `ts` order, which is what every reader in `awair.db`
    returns.
    """
    for _epoch, value in reversed(points):
        if value is not None:
            return value
    return None


def hours_below_target(points, target_at, max_gap_seconds):
    """How many hours `points` spent below target, over its own span.

    `points` is `[(epoch, humid)]` ascending -- the main zone sensor's
    readings -- and `target_at` is called with each reading's epoch, because
    the target itself moves with the outdoor temperature and a week in January
    can contain both of its values.

    **Each reading stands for the time until the next one**, which is the only
    honest way to total a sampled quantity: the readings are 30 s apart on the
    Awair and 300 s apart on the humidifier, so counting readings rather than
    seconds would make the answer depend on which sensor was asked.

    `max_gap_seconds` is what stops an outage from being counted as dry
    hours. A gap longer than it contributes nothing at all -- not the gap, not
    the reading that opens it. The reading before a six-hour poller outage is
    evidence about the minute it was taken in and about nothing else, and a
    figure that quietly folded dead time into "hours below target" would read
    highest exactly when the recorder was most broken.

    A null humidity is dropped the same way and for the same reason: it is not
    a reading, and the row before it must not be stretched across it. So this
    returns, in words, "hours we *observed* the zone below target" -- which is
    a lower bound on the truth, and the right direction for a number a human
    reads as "how bad was last week".
    """
    seconds = 0.0
    readings = [(epoch, value) for epoch, value in points if value is not None]
    for (epoch, value), (next_epoch, _next) in pairwise(readings):
        gap = next_epoch - epoch
        if 0 < gap <= max_gap_seconds and value < target_at(epoch):
            seconds += gap
    return round(seconds / 3600, 1)


#: The device states the card shades, and the word each is shaded with.
#:
#: Two spans rather than one, because they are two different facts with two
#: different fixes: `off` is a humidifier somebody switched off (or that
#: switched itself off at its own target), `dry` is one that has run out of
#: water. A humidity trace that sags under a `dry` span is a refill; under an
#: `off` span it is a setting.
#:
#: A `switch` of 'unknown' is NOT shaded. The library publishes
#: `DeviceStatus.UNKNOWN` when it could not read the field, and shading it
#: would draw "the humidifier was off" over a gap in our knowledge.
OFF_SPAN = "off"
DRY_SPAN = "dry"


def _span_reason(switch, water_lacks, tank_lifted):
    """Which span one humidifier row belongs in, or None for a working one.

    Out-of-water wins over off, because an empty humidifier that has also
    switched itself off is one fact with one fix -- and the water is the half
    a reader can do something about. A lifted tank reads as `dry` too: it is
    the same "not humidifying, go and look at it" state, and it is usually
    the refill in progress.
    """
    if water_lacks or tank_lifted:
        return DRY_SPAN
    if switch == "off":
        return OFF_SPAN
    return None


def spans(rows, max_gap_seconds):
    """Consecutive runs of off / out-of-water rows, as shading spans.

    `rows` is `[(epoch, switch, water_lacks, water_tank_lifted)]` ascending,
    and the result is `[{"start", "end", "reason"}]`.

    **Three things end a span**, and the first of them is the one the first
    draft of this function missed: a row that reports the humidifier working
    again, a row with the other reason, and a gap to the next row longer than
    `max_gap_seconds`. Without the first, an off stretch and a later off
    stretch five minutes apart merge into one span straight through the
    humidifier running in between -- `tests/test_main_zone.py::
    test_a_working_row_between_two_off_rows_splits_the_span` is that case.

    The time bound is the honest end of the last one: an outage is not
    evidence that the humidifier stayed off through it, so shading stops at
    the last row that said so. A single row's span is widened to one poll
    (`_widen`), because a span of zero width draws as nothing and "it was off
    for one poll" is a thing the card should be able to show.
    """
    result = []
    open_span = None
    for epoch, switch, water_lacks, tank_lifted in rows:
        reason = _span_reason(switch, water_lacks, tank_lifted)
        if reason is None:
            # A working poll closes whatever was open. Not `continue` alone:
            # that leaves the span open and lets a later row join it.
            open_span = None
            continue
        if _joins(open_span, epoch, reason, max_gap_seconds):
            open_span["end"] = epoch
        else:
            open_span = {"start": epoch, "end": epoch, "reason": reason}
            result.append(open_span)
    return [_widen(span, max_gap_seconds) for span in result]


def _joins(open_span, epoch, reason, max_gap_seconds):
    """Does this row continue the span that is open, or start a new one?"""
    return (
        open_span is not None
        and open_span["reason"] == reason
        and epoch - open_span["end"] <= max_gap_seconds
    )


def _widen(span, max_gap_seconds):
    """Give a one-row span the width of the poll it stands for."""
    if span["end"] > span["start"]:
        return span
    return {**span, "end": span["start"] + max_gap_seconds}
