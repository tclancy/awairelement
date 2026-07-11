"""Server-side bucketing: avg/min/max per bucket, explicit gaps."""

from awair.series import bucket

BUCKET = 300  # 5 min


def points(start, values, step=30):
    return [(start + i * step, float(v)) for i, v in enumerate(values)]


def test_bucket_computes_avg_min_max():
    # Two full 5-min buckets of 30s data (10 points each).
    result = bucket(points(600, [10] * 10 + [20] * 9 + [80]), BUCKET)
    assert result["t"] == [600, 900]
    assert result["avg"] == [10.0, 26.0]
    assert result["min"] == [10.0, 20.0]
    assert result["max"] == [10.0, 80.0]


def test_bucket_alignment_snaps_to_bucket_boundaries():
    # Points starting mid-bucket land in the right bucket.
    result = bucket([(750, 5.0), (890, 7.0)], BUCKET)
    assert result["t"] == [600]
    assert result["avg"] == [6.0]


def test_bucket_emits_null_gaps_between_data():
    # Data in bucket 0 and bucket 2, nothing in bucket 1: the gap must be
    # an explicit null so the chart shows a break, not a bridge.
    result = bucket([(600, 1.0), (1230, 3.0)], BUCKET)
    assert result["t"] == [600, 900, 1200]
    assert result["avg"] == [1.0, None, 3.0]
    assert result["min"] == [1.0, None, 3.0]
    assert result["max"] == [1.0, None, 3.0]


def test_bucket_empty_input():
    assert bucket([], BUCKET) == {"t": [], "avg": [], "min": [], "max": []}
