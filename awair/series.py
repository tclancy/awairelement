"""Bucket raw 30s readings into chart-sized avg/min/max series."""


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
    """
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
