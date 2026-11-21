"""Dew point from a temperature and a relative humidity (#161).

**Why the dashboard needs this at all.** The wood stove lowers the great
room's relative humidity mostly by *heating the air*: warm it and the same
water vapour fills a smaller fraction of what the air could now hold, so the
percentage falls with nothing having left the room. Relative humidity
therefore cannot be compared between two places at two temperatures -- 35 %
in a 24 degree great room and 45 % in a 17 degree basement are close to the
same amount of water. Dew point can be: it is the temperature the air would
have to reach to be saturated, so it moves when the *moisture* moves and
stays put when only the heat does.

One formula for every place, deliberately. The Awair publishes its own
`dew_point` and we store it, but the basement sensor, the two mini split
heads, the humidifier and Open-Meteo do not -- and a card drawing the
device's number beside five of ours would be comparing a figure from an
unknown formula with a figure from this one. See
`tests/test_dewpoint.py::test_the_devices_own_dew_point_agrees_with_ours`,
which measures the gap against a recorded Element payload rather than
assuming it away.

Celsius in, Celsius out, like every other temperature in this project's
storage layer; the display conversion happens at the web boundary
(`awair.units`).
"""

import math

#: Magnus-Tetens coefficients, in the Alduchov & Eskridge (1996) revision.
#:
#: The constants that get written as 17.27 / 237.7 are the older Tetens pair;
#: these are the ones fitted for -40..+50 C, which is the range this house
#: actually spans once outdoors is one of the places being compared. The
#: difference between the two pairs is a few hundredths of a degree in the
#: middle of the range and about a tenth at its ends -- not visible on a
#: chart, and free to get right.
#:
#: `B` is in degrees Celsius and `A` is dimensionless, which is the whole
#: reason this module takes and returns Celsius: the formula is not
#: unit-agnostic, and converting on the way in and out would put two
#: conversions around an arithmetic nobody can check by eye.
A = 17.625
B = 243.04


def dew_point(temp_c, relative_humidity):
    """The dew point in Celsius for `temp_c` and `relative_humidity` percent.

    None for either input missing, and None for a humidity at or below zero.
    The second is not defensiveness about a number nobody sends: `ln(0)` is
    -inf and the formula would answer -243.04 for a sensor reporting 0 %,
    which is a plausible-looking dew point for a reading that means "this
    sensor has nothing to say". Every humidity column in this database is
    nullable and three of the five sources publish 0 for an unavailable
    reading at least some of the time.

    A humidity **above** 100 is computed rather than clamped, and that is the
    deliberate other half: it answers a dew point above the air temperature,
    which is exactly what an over-range or drifting sensor is claiming. Hiding
    it behind a clamp would make a miscalibrated sensor look merely humid.
    """
    if temp_c is None or relative_humidity is None:
        return None
    if relative_humidity <= 0:
        return None
    gamma = math.log(relative_humidity / 100) + (A * temp_c) / (B + temp_c)
    return round(B * gamma / (A - gamma), 2)


def series(rows):
    """`[(epoch, temp_c, humid)]` -> `[(epoch, dew_point_c)]`, nulls included.

    The rows a feed stores, mapped to the one derived quantity this module
    exists for. Rows whose dew point is None are **kept, as None**, rather
    than dropped: the callers hand this to `series.bucket` and
    `series.carry_forward`, and both read a None as "nothing landed in this
    window" -- which is what a row with no humidity means and is not the same
    as the row not existing. Dropping them here would let a readable
    temperature with an unreadable humidity be held forward as moisture
    evidence it is not.
    """
    return [(epoch, dew_point(temp, humid)) for epoch, temp, humid in rows]
