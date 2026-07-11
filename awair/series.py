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
