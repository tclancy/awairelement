"""Server-side bucketing: avg/min/max per bucket, explicit gaps."""

from typing import ClassVar

from awair.series import bucket, carry_forward

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


class TestCarryForward:
    """`carry_forward` puts a coarse series onto a finer series' x-grid (#109).

    The combined indoor/outdoor temperature chart needs one shared x-axis, and
    the two sides do not share one: indoor buckets at 60 s on `today` against a
    30 s poll, outdoor at 900 s against a 15-minute publish cadence. uPlot takes
    exactly one `t` array per chart, so something has to resample — and the
    honest resampling of a 15-minute observation is to *hold* it, because that
    is what the last published value means, up until it goes stale.
    """

    GRID: ClassVar[list[int]] = [0, 60, 120, 180, 240, 300]

    def test_a_value_is_held_across_the_grid_stamps_that_follow_it(self):
        # One observation at t=60 holds through 240 with MAX_AGE 180.
        assert carry_forward([(60, 7.0)], self.GRID, 180) == [
            None,
            7.0,
            7.0,
            7.0,
            7.0,
            None,
        ]

    def test_a_grid_stamp_before_the_first_observation_is_none(self):
        """Not back-filled: we did not know the outdoor temperature yet.

        Back-filling would paint a line to the left of any observation
        supporting it — the same fabrication `aq_ts` and `unkeyed row` refuse.
        """
        assert carry_forward([(120, 5.0)], self.GRID, 600)[:2] == [None, None]

    def test_the_newest_observation_at_or_before_the_stamp_wins(self):
        # Two observations inside one gap: the later one takes over at its own
        # stamp, and the earlier one is not resurrected afterwards.
        assert carry_forward([(60, 1.0), (180, 2.0)], self.GRID, 600) == [
            None,
            1.0,
            1.0,
            2.0,
            2.0,
            2.0,
        ]

    def test_an_observation_exactly_on_a_grid_stamp_applies_to_it(self):
        """The boundary is `<=`, not `<`.

        Off by one here and every value lands one bucket late — a shift small
        enough to look like lag in the data rather than a bug in the chart,
        which is why it gets its own test rather than riding on the hold test.
        """
        assert carry_forward([(120, 9.0)], self.GRID, 600)[2] == 9.0

    def test_a_stale_observation_becomes_a_gap_rather_than_a_flat_line(self):
        """The whole reason for the age bound.

        An outdoor poller outage must read as *absence*. Held indefinitely it
        would render as a perfectly flat outdoor trace — which on a chart whose
        entire purpose is "does indoor follow outdoor" is not a missing line,
        it is a wrong answer: a flat outdoor line beside a moving indoor one
        says the house is drifting on its own.
        """
        held = carry_forward([(0, 4.0)], self.GRID, 120)
        assert held == [4.0, 4.0, 4.0, None, None, None]

    def test_the_age_bound_is_inclusive_at_its_edge(self):
        # age == max_age is still fresh; one second past it is not.
        assert carry_forward([(0, 4.0)], [120], 120) == [4.0]
        assert carry_forward([(0, 4.0)], [121], 120) == [None]

    def test_a_none_valued_observation_does_not_become_a_held_value(self):
        """`bucket` emits None for an empty bucket, and those arrive here.

        A None must neither be held forward as a value nor mask the real
        observation before it — it is "this bucket was empty", not "the
        temperature is unknown from here on".
        """
        assert carry_forward([(0, 4.0), (60, None), (120, 6.0)], self.GRID, 600) == [
            4.0,
            4.0,
            6.0,
            6.0,
            6.0,
            6.0,
        ]

    def test_unsorted_observations_are_ordered_before_holding(self):
        """`outdoor_readings_since` is ordered, but this is a public helper.

        Fed out of order without a sort, the scan below would hold whichever
        value happened to come last rather than the newest one.
        """
        assert carry_forward([(180, 2.0), (60, 1.0)], self.GRID, 600) == [
            None,
            1.0,
            1.0,
            2.0,
            2.0,
            2.0,
        ]

    def test_an_empty_grid_is_an_empty_result(self):
        assert carry_forward([(0, 1.0)], [], 600) == []

    def test_no_observations_is_a_grid_shaped_run_of_none(self):
        """Length must track the GRID, never the observations.

        This is the one that keeps the chart from throwing: uPlot requires
        every series to be exactly as long as its x array, so an outdoor
        poller that has never run has to produce a full-length run of None.
        """
        assert carry_forward([], self.GRID, 600) == [None] * len(self.GRID)
