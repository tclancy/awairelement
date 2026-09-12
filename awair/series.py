"""Bucket raw 30s readings into chart-sized avg/min/max series."""

from itertools import pairwise


def bucket(points, bucket_seconds):
    """[(epoch_seconds, value)] → {t, avg, min, max} arrays.

    Buckets snap to bucket_seconds boundaries. Every bucket between the
    first and last datapoint is emitted; empty ones carry None so the
    chart renders a gap instead of bridging it.
    """
    if not points:
        return {"t": [], "avg": [], "min": [], "max": []}

    grouped = {}
    for t, value in points:
        grouped.setdefault(int(t // bucket_seconds) * bucket_seconds, []).append(value)

    first = min(grouped)
    last = max(grouped)
    result = {"t": [], "avg": [], "min": [], "max": []}
    for start in range(first, last + 1, bucket_seconds):
        values = grouped.get(start)
        result["t"].append(start)
        if values:
            result["avg"].append(round(sum(values) / len(values), 2))
            result["min"].append(min(values))
            result["max"].append(max(values))
        else:
            result["avg"].append(None)
            result["min"].append(None)
            result["max"].append(None)
    return result


def carry_forward(points, grid, max_age_seconds):
    """[(epoch_seconds, value)] → one value per stamp in `grid`, or None.

    Puts a coarsely-sampled series onto a finer series' x-grid so the two can
    share one chart (#109). Each grid stamp takes the newest observation at or
    before it — a *hold*, which is what "the last published value" means for a
    source that publishes every quarter hour — and None once that observation
    is more than `max_age_seconds` old.

    Three rules, each of which is a refusal to invent a reading:

    - **No back-fill.** A grid stamp earlier than every observation is None,
      not the first value. We did not know it yet.
    - **The age bound is the point.** Held without one, an outdoor poller
      outage renders as a perfectly flat trace rather than a gap — and on a
      chart asking "does indoor follow outdoor", a flat outdoor line beside a
      moving indoor one is not a missing answer, it is a wrong one.
    - **None observations are dropped, not held.** `bucket` writes None for an
      empty bucket, which is "nothing landed in this window", not "the
      temperature is unknown from here on" — and crucially it is not evidence
      of freshness either, so the age is measured from the last *real* value.

    The result is always exactly as long as `grid`: uPlot requires every series
    to match its x array, so a source that has never published still has to
    produce a full-length run of None.

    `points` may arrive in any order and is sorted. **`grid` may not** -- the
    scan below walks it once and never rewinds, so a descending or shuffled
    grid returns wrong values rather than raising. Callers pass `bucket`'s
    `t`, which is ascending by construction; the assertion makes that a
    precondition rather than a coincidence.
    """
    assert all(a <= b for a, b in pairwise(grid)), (
        "`grid` must be ascending -- the hold scan walks it once and cannot "
        "rewind, so an out-of-order stamp silently takes an earlier value"
    )
    observations = sorted((t, v) for t, v in points if v is not None)
    result = []
    index = 0
    held_at = None
    held_value = None
    for stamp in grid:
        while index < len(observations) and observations[index][0] <= stamp:
            held_at, held_value = observations[index]
            index += 1
        fresh = held_at is not None and stamp - held_at <= max_age_seconds
        result.append(held_value if fresh else None)
    return result


def peak(series):
    """The highest single reading in a bucketed `series`, or None if it has none.

    The card header's answer to "how bad did it get in this range" (#116).
    Nothing on a chart answered that before: the drawn line is `avg`, and the
    legend is uPlot's *live* legend, so its `low` and `high` are the extremes
    of the one bucket under the cursor rather than of the range. At the 60 s
    `today` bucket that is two samples of a 30 s poll, and a one-bucket spike
    is a few pixels wide — not a number a reader can get by hovering.

    Read off `max`, deliberately, and the distinction is not cosmetic:

    - **`max` is invariant under bucket size; `avg` is not.** The same
      readings re-bucketed by the range button leave every bucket maximum in
      some bucket, so their maximum does not move. The maximum bucket
      *average* shrinks as buckets widen, so a peak derived from it would
      change every time Tom pressed "7 days" — a number that moves when only
      the drawing changed is the defect this function exists to answer.
    - **It is the top of the band, not the top of the line.** The drawn `avg`
      line necessarily tops out at or below this, and the y-axis is already
      autoscaled to `max`, so this is the reading the axis was sized for.

    Empty buckets carry None ("nothing landed in this window"), which is not a
    value and must not reach `max()`.
    """
    values = [value for value in series["max"] if value is not None]
    return max(values) if values else None
