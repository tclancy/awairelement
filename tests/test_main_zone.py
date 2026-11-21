"""The main zone's target, its sensor, and the two figures the card needs (#161).

Pure functions over sequences, like `awair.spikes` -- so the rules that decide
what the dashboard claims (when the target drops, what counts as an hour below
it, what gets shaded) are tested without a database or a request.
"""

import logging

import pytest

from awair import feeds, main_zone

HOUR = 3600


# --- the target ------------------------------------------------------------


def test_the_defaults_are_the_numbers_the_issue_names():
    assert (main_zone.target(), main_zone.cold_target()) == (40.0, 35.0)
    assert main_zone.cold_outdoor_temp() == -12.0


def test_a_mild_day_gets_the_warm_target():
    assert main_zone.target_for(0.0) == 40.0
    assert main_zone.target_for(-11.9) == 40.0


def test_a_very_cold_day_gets_the_cold_target():
    """At or below the threshold, because that is how the guidance reads."""
    assert main_zone.target_for(-12.0) == 35.0
    assert main_zone.target_for(-20.0) == 35.0


def test_an_unknown_outdoor_temperature_gets_the_WARM_target():
    """A dead outdoor poller must not quietly relax the target by five points.

    The failure direction matters more than the value: with the warm target
    the chart says "we are short", which a reader investigates. With the cold
    one, a house that had stopped being humidified would read as on target.
    """
    assert main_zone.target_for(None) == 40.0


@pytest.mark.parametrize(
    ("env", "value", "expected"),
    [
        (main_zone.TARGET_ENV, "45", (45.0, 35.0, -12.0)),
        (main_zone.COLD_TARGET_ENV, "30", (40.0, 30.0, -12.0)),
        (main_zone.COLD_OUTDOOR_ENV, "-18", (40.0, 35.0, -18.0)),
    ],
)
def test_all_three_numbers_are_environment_tunable(env, value, expected, monkeypatch):
    monkeypatch.setenv(env, value)

    assert (
        main_zone.target(),
        main_zone.cold_target(),
        main_zone.cold_outdoor_temp(),
    ) == expected


def test_the_switch_follows_its_own_variable(monkeypatch):
    """Move the threshold and the same outdoor temperature changes target."""
    monkeypatch.setenv(main_zone.COLD_OUTDOOR_ENV, "-5")

    assert main_zone.target_for(-6.0) == 35.0
    assert main_zone.target_for(-4.0) == 40.0


def test_an_unreadable_target_falls_back_and_says_so(monkeypatch, caplog):
    """A typo in an environment file must not 500 the dashboard.

    The opposite ruling to `units.get_temperature_unit`, which raises -- that
    one is read once at startup, where a typo should stop the process. These
    are read per request by a page, where raising would take the whole
    dashboard down over a number that has a documented default.
    """
    monkeypatch.setenv(main_zone.TARGET_ENV, "forty")

    with caplog.at_level(logging.WARNING, logger="awair.main_zone"):
        assert main_zone.target() == 40.0

    assert main_zone.TARGET_ENV in caplog.text


# --- which sensor is the zone ---------------------------------------------


def test_the_default_zone_is_the_awair():
    assert main_zone.feed().name == "awair"


def test_the_zone_follows_its_environment_variable(monkeypatch):
    monkeypatch.setenv(main_zone.FEED_ENV, "humidifier")

    assert main_zone.feed().name == "humidifier"


def test_every_feed_is_a_legal_zone(monkeypatch):
    """The variable names a FEED, so every feed has to be nameable.

    Which also pins the vocabulary: `/health` publishes these names, so "which
    sensor is the great room" is answered in the same words on both surfaces.
    """
    for feed in feeds.FEEDS:
        monkeypatch.setenv(main_zone.FEED_ENV, feed.name)
        assert main_zone.feed() is feed


def test_an_unknown_zone_falls_back_and_lists_the_real_ones(monkeypatch, caplog):
    monkeypatch.setenv(main_zone.FEED_ENV, "kitchen")

    with caplog.at_level(logging.WARNING, logger="awair.main_zone"):
        assert main_zone.feed().name == main_zone.DEFAULT_FEED

    assert "kitchen" in caplog.text
    assert "humidifier" in caplog.text


# --- hours below target ----------------------------------------------------


def warm_target(_epoch):
    return 40.0


def test_each_reading_stands_for_the_time_until_the_next_one():
    """Two hours of readings an hour apart, all below target: two hours.

    Counted in seconds rather than in readings, because the sensors sample 30
    s to 300 s apart and a count would make the answer depend on which one
    was asked.
    """
    points = [(0, 30.0), (HOUR, 31.0), (2 * HOUR, 32.0)]

    assert main_zone.hours_below_target(points, warm_target, 2 * HOUR) == 2.0


def test_readings_above_target_contribute_nothing():
    points = [(0, 45.0), (HOUR, 46.0), (2 * HOUR, 30.0)]

    assert main_zone.hours_below_target(points, warm_target, 2 * HOUR) == 0.0


def test_only_the_stretch_below_target_counts():
    points = [(0, 45.0), (HOUR, 30.0), (2 * HOUR, 44.0), (3 * HOUR, 30.0)]

    assert main_zone.hours_below_target(points, warm_target, 2 * HOUR) == 1.0


def test_an_outage_is_not_counted_as_dry_hours():
    """The reading before a six-hour gap is evidence about its own minute.

    Without the bound, the figure reads highest exactly when the recorder is
    most broken -- and "hours below target" would quietly become "hours we
    were not looking".
    """
    points = [(0, 30.0), (6 * HOUR, 30.0), (7 * HOUR, 30.0)]

    assert main_zone.hours_below_target(points, warm_target, 2 * HOUR) == 1.0


def test_a_null_humidity_is_not_a_reading():
    """And the row before it is not stretched across it either."""
    points = [(0, 30.0), (HOUR, None), (2 * HOUR, 30.0), (3 * HOUR, 30.0)]

    assert main_zone.hours_below_target(points, warm_target, 90 * 60) == 1.0


def test_the_target_is_evaluated_at_each_readings_own_moment():
    """A week in January contains both targets, so one number will not do.

    35 % at the first reading and 40 % at the second: the same 37 % humidity
    is above target in the cold hour and below it in the mild one.
    """
    points = [(0, 37.0), (HOUR, 37.0), (2 * HOUR, 37.0)]
    targets = {0: 35.0, HOUR: 40.0, 2 * HOUR: 40.0}

    hours = main_zone.hours_below_target(points, targets.get, 2 * HOUR)

    assert hours == 1.0


def test_no_readings_at_all_is_zero_rather_than_an_error():
    assert main_zone.hours_below_target([], warm_target, HOUR) == 0.0


def test_latest_reads_the_newest_value_there_is():
    assert main_zone.latest([(1, 10.0), (2, None), (3, 12.0)]) == 12.0
    assert main_zone.latest([(1, 10.0), (2, None)]) == 10.0
    assert main_zone.latest([(1, None)]) is None
    assert main_zone.latest([]) is None


# --- the shading spans -----------------------------------------------------


def row(epoch, switch="on", water_lacks=0, lifted=0):
    return (epoch, switch, water_lacks, lifted)


def test_consecutive_off_rows_are_one_span():
    rows = [row(0), row(300, "off"), row(600, "off"), row(900)]

    assert main_zone.spans(rows, 600) == [{"start": 300, "end": 600, "reason": "off"}]


def test_a_working_row_between_two_off_rows_splits_the_span():
    """Two separate stretches of being off are two separate facts."""
    rows = [row(0, "off"), row(300), row(600, "off")]

    spans = main_zone.spans(rows, 600)

    assert [s["start"] for s in spans] == [0, 600]


def test_out_of_water_outranks_off():
    """An empty humidifier that has switched itself off is one fact: refill it."""
    rows = [row(0, "off", water_lacks=1)]

    assert main_zone.spans(rows, 600)[0]["reason"] == "dry"


def test_a_lifted_tank_shades_as_dry():
    rows = [row(0, "on", lifted=1)]

    assert main_zone.spans(rows, 600)[0]["reason"] == "dry"


def test_an_unknown_switch_is_not_shaded():
    """`DeviceStatus.UNKNOWN` means the field could not be read.

    Shading it would draw "the humidifier was off" over a gap in our
    knowledge -- the same refusal `carry_forward` makes about a null.
    """
    assert main_zone.spans([row(0, "unknown")], 600) == []


def test_a_gap_longer_than_the_bound_ends_the_span():
    """An outage is not evidence that the humidifier stayed off through it."""
    rows = [row(0, "off"), row(5000, "off")]

    spans = main_zone.spans(rows, 600)

    assert len(spans) == 2


def test_one_row_shades_the_poll_it_stands_for():
    """Otherwise a single off poll draws as a span of zero width: invisible."""
    (span,) = main_zone.spans([row(1000, "off")], 600)

    assert span == {"start": 1000, "end": 1600, "reason": "off"}


def test_a_humidifier_that_never_stopped_shades_nothing():
    assert main_zone.spans([row(0), row(300), row(600)], 600) == []
