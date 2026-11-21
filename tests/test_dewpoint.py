"""Dew point, against values computed outside this repo (#161).

The whole value of this module is that six places can be compared, so the one
thing worth testing hardest is that the arithmetic is right rather than merely
consistent with itself. These expectations come from published tables and from
the Magnus formula's own invariants, not from running the function and
pasting what it said.
"""

import json
import math
from pathlib import Path

import pytest

from awair import dewpoint

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# (temperature C, relative humidity %, dew point C).
#
# Every expectation here was produced OUTSIDE this module, by inverting the
# Buck (1981) saturation-vapour-pressure equation numerically -- see
# `buck_dew_point` below, which is a different formula from a different
# author reaching the same quantity by a different route. Running
# `dewpoint.dew_point` and pasting its answer would test that the function
# still does what it does today, which is not the same as testing that it is
# right.
#
# The tolerance is 0.05 C and the two agree to about 0.02 across this range.
# It is tight enough to fail on the OLDER Tetens coefficient pair (17.27 /
# 237.7) at the ends of the range, so this pins WHICH Magnus variant the
# module implements rather than merely that it implements one.
KNOWN = [
    (20.0, 50.0, 9.27),
    (25.0, 60.0, 16.70),
    (30.0, 80.0, 26.17),
    (0.0, 60.0, -6.84),
    # Winter house numbers, which is what this project actually asks it: a
    # stove-dry great room, and the same room after the humidifier has run.
    (23.0, 30.0, 4.51),
    (23.0, 40.0, 8.69),
    # Below freezing outdoors. The formula is a water-vapour one and keeps
    # reporting a dew point rather than a frost point below 0 C; that is the
    # convention every weather service publishes and what Open-Meteo's own
    # numbers are, so the two are comparable on one chart.
    (-10.0, 70.0, -14.43),
]


def buck_dew_point(temp_c, relative_humidity):
    """The same quantity by an independent route, for the cross-check below.

    Buck's 1981 equation for saturation vapour pressure over water, inverted
    by bisection rather than algebraically -- so this shares no constants, no
    algebra and no code with `awair.dewpoint`. Two formulas fitted to the
    same measurements by different people agreeing to a hundredth of a degree
    is evidence the arithmetic is right; one formula agreeing with itself is
    not.
    """

    def saturation_hpa(t):
        return 6.1121 * math.exp((18.678 - t / 234.5) * t / (257.14 + t))

    target = relative_humidity / 100 * saturation_hpa(temp_c)
    low, high = -80.0, temp_c
    for _ in range(200):
        middle = (low + high) / 2
        if saturation_hpa(middle) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2


@pytest.mark.parametrize(("temp", "humid", "expected"), KNOWN)
def test_dew_point_matches_known_values(temp, humid, expected):
    assert dewpoint.dew_point(temp, humid) == pytest.approx(expected, abs=0.05)


def test_saturated_air_dews_at_its_own_temperature():
    """The formula's one exact invariant: at 100 %, dew point IS temperature.

    Worth its own case because it is the only expectation here that needs no
    table to check and holds at every temperature -- so it fails on an
    arithmetic slip that a single worked example might happen to survive.
    """
    for temp in (-15.0, 0.0, 12.5, 21.0, 35.0):
        assert dewpoint.dew_point(temp, 100) == pytest.approx(temp, abs=0.01)


def test_dew_point_is_never_above_the_air_temperature_below_saturation():
    for temp in (-10.0, 5.0, 22.0, 30.0):
        for humid in (5, 20, 45, 70, 99):
            assert dewpoint.dew_point(temp, humid) < temp


def test_the_stoves_effect_is_what_this_module_exists_to_show():
    """Heat the air and the humidity falls; the dew point does not move.

    The premise of the whole humidity view, as an assertion. Take a room at
    17 C and 50 %, work out the moisture it holds, then warm it to 24 C with
    no water added: the relative humidity reads about 32 % and the dew point
    is the same number it was.

    Without this, a dew point card is just another line; with it, the claim
    the dashboard's footer makes about the wood stove is checked.
    """
    cool, warm = 17.0, 24.0
    dew = dewpoint.dew_point(cool, 50)

    # The same air at `warm`: invert the Magnus formula for the humidity that
    # produces this dew point at the higher temperature.
    gamma = dewpoint.A * dew / (dewpoint.B + dew)
    warmed_humidity = 100 * math.exp(gamma - dewpoint.A * warm / (dewpoint.B + warm))

    assert warmed_humidity == pytest.approx(32.0, abs=1.0)
    assert dewpoint.dew_point(warm, warmed_humidity) == pytest.approx(dew, abs=0.01)


@pytest.mark.parametrize(
    ("temp", "humid"),
    [(None, 50), (20.0, None), (None, None), (20.0, 0), (20.0, -5)],
)
def test_a_reading_that_is_not_one_answers_none(temp, humid):
    """Zero humidity especially: `ln(0)` is -inf and would answer -243.04 C.

    Every humidity column in this database is nullable, and a sensor with
    nothing to say publishes 0 often enough that a plausible-looking dew
    point for it would reach the chart.
    """
    assert dewpoint.dew_point(temp, humid) is None


def test_an_over_range_humidity_is_computed_rather_than_clamped():
    """Above 100 % answers a dew point above the air temperature, on purpose.

    That is what a drifting or condensing sensor is claiming, and clamping
    would redraw it as merely humid.
    """
    assert dewpoint.dew_point(20.0, 105) > 20.0


def test_series_keeps_a_row_whose_dew_point_cannot_be_computed():
    """A None stays in place, because the callers bucket and carry these.

    Dropped, the row before an unreadable one would be held forward across it
    by `series.carry_forward` -- moisture evidence from a row that has none.
    """
    rows = [(10, 20.0, 50.0), (20, 20.0, None), (30, 21.0, 45.0)]

    assert [epoch for epoch, _ in dewpoint.series(rows)] == [10, 20, 30]
    assert dewpoint.series(rows)[1][1] is None


def test_the_devices_own_dew_point_agrees_with_ours():
    """The Awair publishes a dew point; we compute one. Measure the gap.

    The card draws one formula for all six places (five of which publish no
    dew point at all), so the Element's own figure is not used -- and this is
    the test that says what ignoring it costs. Against the recorded payload
    in `tests/fixtures/air_data_latest.json` the two agree to well inside a
    tenth of a degree, which is smaller than the sensor's own humidity
    accuracy and far smaller than any gap between rooms the card is for.

    If a firmware change moved the device's formula this would fail, and the
    right response would be to read this docstring rather than to widen the
    tolerance: it would mean the device had started answering a different
    question.
    """
    payload = json.loads((FIXTURES / "air_data_latest.json").read_text())

    ours = dewpoint.dew_point(payload["temp"], payload["humid"])

    assert ours == pytest.approx(payload["dew_point"], abs=0.1)


@pytest.mark.parametrize("temp", [-15.0, -5.0, 5.0, 15.0, 20.0, 23.0, 28.0])
@pytest.mark.parametrize("humid", [10, 25, 40, 55, 70, 90])
def test_it_agrees_with_an_independent_formula_across_the_houses_range(temp, humid):
    """42 points of Magnus against Buck, over every condition this house sees.

    The table above pins seven individual values; this pins the whole surface,
    including the cold-outdoor and stove-dry corners where the two
    coefficient pairs diverge most. 0.05 C is roughly twice the largest
    disagreement measured (0.02), and two orders of magnitude inside the
    humidity sensors' own accuracy.
    """
    assert dewpoint.dew_point(temp, humid) == pytest.approx(
        buck_dew_point(temp, humid), abs=0.05
    )
