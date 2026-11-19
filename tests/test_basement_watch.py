"""The basement watch (parsons-pulse #205).

Every timestamp here is built from a *local* datetime through `.timestamp()`,
so the suite says what it means whatever zone it runs in: the round trip
through `basement_watch.local` returns the same wall clock it was written as.
Hard-coded epochs would make "02:00" mean a different hour on every machine,
and the whole rule is stated in wall-clock hours.

Calendar facts the window cases rest on, for a reader who should not have to
go and check: in 2026, 2026-11-09 is a Monday, 2026-11-13 a Friday,
2026-11-14 a Saturday and 2026-11-15 a Sunday.
"""

from datetime import datetime, timedelta

import pytest

from awair import basement, basement_watch, outdoor
from awair.basement_watch import CLEAR, ON, UNKNOWN

MONDAY = (2026, 11, 9)
FRIDAY = (2026, 11, 13)
SATURDAY = (2026, 11, 14)
SUNDAY = (2026, 11, 15)

# Above and below the 62 °F threshold, in the Celsius the rows are stored in.
WARM = basement_watch.WARM_ABOVE_C + 0.5
COOL = basement_watch.WARM_ABOVE_C - 0.5


def at(day, hour, minute=0):
    """An epoch second for a local wall-clock time on `day`."""
    return datetime(*day, hour, minute).timestamp()


def moment(day, hour, minute=0):
    return datetime(*day, hour, minute)


def outdoor_cover(until, celsius, hours=30):
    """A covered outdoor series ending at `until`, every quarter hour.

    Wider than one `ATTRIBUTION_LOOKBACK` by default so a test can move `at`
    around inside it without accidentally testing the coverage rule.
    """
    step = outdoor.SOURCE_INTERVAL_SECONDS
    start = until - hours * 3600
    stamps = [start + i * step for i in range(int(hours * 3600 / step) + 1)]
    return [(ts, celsius) for ts in stamps if ts <= until]


def basement_run(first, celsius, count, step=60):
    """`count` basement readings at `celsius`, one per `step` from `first`."""
    return [(first + i * step, celsius) for i in range(count)]


# --- the watch window -------------------------------------------------------


@pytest.mark.parametrize(
    "day, hour, expected",
    [
        (MONDAY, 23, True),
        (MONDAY, 0, True),
        (MONDAY, 16, True),
        (MONDAY, 17, False),
        (MONDAY, 18, False),
        (MONDAY, 22, False),
    ],
)
def test_the_weekday_window_wraps_midnight_and_closes_at_five_pm(day, hour, expected):
    assert basement_watch.in_watch_window(moment(day, hour)) is expected


@pytest.mark.parametrize(
    "day, hour, expected",
    [
        (SATURDAY, 0, True),
        (SATURDAY, 6, True),
        (SATURDAY, 7, False),
        (SATURDAY, 12, False),
        (SATURDAY, 23, False),
        (SUNDAY, 2, True),
        (SUNDAY, 9, False),
    ],
)
def test_the_weekend_window_is_narrowed_to_midnight_until_seven(day, hour, expected):
    assert basement_watch.in_watch_window(moment(day, hour)) is expected


def test_one_continuous_night_is_classified_by_each_halfs_own_day():
    """Friday 23:30 is a weekday hour; Saturday 02:00 is a weekend one."""
    assert basement_watch.in_watch_window(moment(FRIDAY, 23, 30)) is True
    assert basement_watch.in_watch_window(moment(SATURDAY, 2)) is True
    # And the consequence the module names: Sunday's late evening is in no
    # window, because Sunday takes the weekend rule and it has already closed.
    assert basement_watch.in_watch_window(moment(SUNDAY, 23, 30)) is False


# --- attribution ------------------------------------------------------------


def test_the_attributable_high_is_the_maximum_over_the_lookback():
    until = at(MONDAY, 2)
    rows = outdoor_cover(until, 5.0)
    rows.append((until - 3600, 18.0))
    rows.sort()
    assert basement_watch.attributable_high(rows, until) == 18.0


def test_a_high_older_than_the_lookback_does_not_account_for_now():
    until = at(MONDAY, 2)
    older = until - basement_watch.ATTRIBUTION_LOOKBACK.total_seconds() - 60
    rows = sorted([*outdoor_cover(until, 5.0), (older, 30.0)])
    assert basement_watch.attributable_high(rows, until) == 5.0


def test_no_outdoor_observation_in_the_lookback_is_no_verdict():
    assert basement_watch.attributable_high([], at(MONDAY, 2)) is None


def test_a_hole_wider_than_the_gap_bound_refuses_to_answer():
    until = at(MONDAY, 2)
    rows = outdoor_cover(until, 5.0)
    hole_opens = until - 10 * 3600
    kept = [
        (ts, c)
        for ts, c in rows
        if not hole_opens
        <= ts
        <= hole_opens + basement_watch.MAX_ATTRIBUTION_GAP.total_seconds() + 60
    ]
    assert basement_watch.attributable_high(kept, until) is None


def test_a_window_whose_observations_start_late_refuses_to_answer():
    """The edges are part of the coverage test, not only the interior."""
    until = at(MONDAY, 2)
    rows = outdoor_cover(until, 5.0, hours=4)
    assert basement_watch.attributable_high(rows, until) is None


def test_a_dead_poller_refuses_to_answer_rather_than_reporting_the_night():
    until = at(MONDAY, 2)
    rows = outdoor_cover(until - 6 * 3600, 5.0)
    assert basement_watch.attributable_high(rows, until) is None


def test_a_null_outdoor_temperature_is_skipped_not_counted():
    until = at(MONDAY, 2)
    rows = [
        (ts, None) if i % 8 == 3 else (ts, 5.0)
        for i, (ts, _) in enumerate(outdoor_cover(until, 5.0))
    ]
    assert basement_watch.attributable_high(rows, until) == 5.0


# --- the watch itself -------------------------------------------------------


def test_warm_in_the_window_with_a_cold_day_behind_it_is_a_yellow():
    now = at(SATURDAY, 3)
    state = basement_watch.watch(
        basement_run(at(SATURDAY, 2), WARM, 61), outdoor_cover(now, 5.0), now
    )
    assert state.status == ON
    assert state.outdoor_high == 5.0
    assert state.basement == WARM


def test_a_warm_spell_behind_it_accounts_for_the_warmth_and_clears():
    """The 2026-11-07 episode: a 62.4 °F basement after a 67.5 °F Friday."""
    now = at(SATURDAY, 3)
    rows = outdoor_cover(now, 5.0)
    rows.append((at(FRIDAY, 13), 19.7))  # 67.5 °F
    rows.sort()
    state = basement_watch.watch(basement_run(at(SATURDAY, 2), WARM, 61), rows, now)
    assert state.status == CLEAR
    assert state.outdoor_high is None


def test_an_outdoor_outage_does_not_manufacture_a_yellow():
    """And says so: UNKNOWN, not CLEAR.

    A warm basement we could not judge is a different fact from one the
    weather explains, and only the second is a statement about the house.
    """
    now = at(SATURDAY, 3)
    state = basement_watch.watch(basement_run(at(SATURDAY, 2), WARM, 61), [], now)
    assert state.status == UNKNOWN
    assert state.outdoor_high is None


def test_warm_outside_the_window_is_not_the_watchs_business():
    now = at(SATURDAY, 12)
    state = basement_watch.watch(
        basement_run(at(SATURDAY, 11), WARM, 61), outdoor_cover(now, 5.0), now
    )
    assert state.status == CLEAR


def test_coming_back_to_sixty_two_clears_a_watch_an_earlier_trip_raised():
    now = at(SATURDAY, 6)
    rows = [
        *basement_run(at(SATURDAY, 2), WARM, 60),
        *basement_run(at(SATURDAY, 5), COOL, 61),
    ]
    state = basement_watch.watch(rows, outdoor_cover(now, 5.0), now)
    assert state.status == CLEAR
    assert state.basement == COOL


def test_the_watch_latches_past_the_windows_close_while_the_basement_stays_warm():
    """A trip at 02:00 Saturday is still standing at noon, unweakened."""
    now = at(SATURDAY, 12)
    rows = basement_run(at(SATURDAY, 2), WARM, 601, step=60)
    state = basement_watch.watch(rows, outdoor_cover(now, 5.0, hours=40), now)
    assert state.status == ON
    assert state.since == at(SATURDAY, 2)


def test_since_names_the_first_reading_of_the_uncleared_run_not_the_newest():
    now = at(SATURDAY, 4)
    rows = [
        (at(SATURDAY, 1), COOL),
        *basement_run(at(SATURDAY, 2), WARM, 121),
    ]
    state = basement_watch.watch(rows, outdoor_cover(now, 5.0), now)
    assert state.status == ON
    assert state.since == at(SATURDAY, 2)


def test_a_trip_before_the_last_clear_cannot_raise_the_watch_again():
    now = at(SATURDAY, 6)
    rows = [
        *basement_run(at(SATURDAY, 1), WARM, 60),  # tripped, in window
        (at(SATURDAY, 2), COOL),  # and cleared
        *basement_run(at(SATURDAY, 8), WARM, 10),  # warm again, out of window
    ]
    state = basement_watch.watch(
        rows, outdoor_cover(now, 5.0, hours=40), at(SATURDAY, 8, 10)
    )
    assert state.status == CLEAR


def test_no_basement_reading_at_all_is_unknown_not_clear():
    assert (
        basement_watch.watch(
            [], outdoor_cover(at(MONDAY, 2), 5.0), at(MONDAY, 2)
        ).status
        == UNKNOWN
    )


def test_a_sensor_that_stopped_reporting_cannot_leave_a_yellow_standing():
    now = at(SATURDAY, 3)
    stale = now - basement_watch.stale_after_seconds() - 1
    rows = basement_run(stale - 3600, WARM, 61)
    state = basement_watch.watch(rows, outdoor_cover(now, 5.0), now)
    assert state.status == UNKNOWN
    assert state.since is None


def test_a_reading_inside_the_stale_bound_still_carries_a_verdict():
    now = at(SATURDAY, 3)
    fresh = now - basement_watch.stale_after_seconds() + 1
    rows = basement_run(fresh - 3600, WARM, 61)
    assert basement_watch.watch(rows, outdoor_cover(now, 5.0), now).status == ON


# --- the boundaries ---------------------------------------------------------


def test_exactly_sixty_two_in_the_basement_is_not_warm():
    now = at(SATURDAY, 3)
    rows = basement_run(at(SATURDAY, 2), basement_watch.WARM_ABOVE_C, 61)
    assert basement_watch.watch(rows, outdoor_cover(now, 5.0), now).status == CLEAR


def test_an_outdoor_high_of_exactly_sixty_two_accounts_for_the_warmth():
    now = at(SATURDAY, 3)
    rows = outdoor_cover(now, basement_watch.WARM_ABOVE_C)
    assert (
        basement_watch.watch(basement_run(at(SATURDAY, 2), WARM, 61), rows, now).status
        == CLEAR
    )


def test_the_threshold_is_sixty_two_fahrenheit_exactly():
    """Not `units.to_celsius`, which rounds the boundary to 62.006 °F."""
    assert basement_watch.WARM_ABOVE_C * 9 / 5 + 32 == pytest.approx(62.0, abs=1e-12)


# --- the derivations --------------------------------------------------------


def test_the_stale_bound_follows_the_basement_pollers_cadence(monkeypatch):
    monkeypatch.setattr(basement, "DEFAULT_POLL_SECONDS", 30)
    assert basement_watch.stale_after_seconds() == 30 * basement_watch.STALE_AFTER_POLLS


def test_the_history_lookback_covers_the_widest_window_and_one_attribution():
    opens, closes = basement_watch.WEEKDAY_WINDOW
    assert (
        basement_watch.history_lookback()
        == timedelta(hours=(24 - opens) + closes) + basement_watch.ATTRIBUTION_LOOKBACK
    )


def test_the_outdoor_lookback_reaches_past_the_oldest_trips_own_window():
    assert (
        basement_watch.attribution_lookback_seconds()
        > (
            basement_watch.history_lookback() + basement_watch.ATTRIBUTION_LOOKBACK
        ).total_seconds()
    )


# --- the attribution window's upper bound -----------------------------------


def test_warmth_arriving_after_the_trip_does_not_clear_the_latch():
    """The lookback ends AT the reading, and the latch is why that bites.

    Once a watch is standing, every later afternoon is inside some reading's
    trailing 24 hours. Without the `ts <= at` bound the trip itself would be
    re-judged against weather that had not happened when it tripped, and a
    hot Tuesday would retroactively explain a cold Monday night. Caught by a
    mutation round: dropping the bound left every other test green.
    """
    trip_at = at(SATURDAY, 2)
    now = at(SATURDAY, 16)
    rows = outdoor_cover(trip_at, COLD := 5.0, hours=30)
    rows += [(trip_at + i * 900, 25.0) for i in range(1, int(14 * 3600 / 900) + 1)]
    state = basement_watch.watch(basement_run(trip_at, WARM, 841), sorted(rows), now)
    assert state.status == ON
    assert state.outdoor_high == COLD


# --- the coverage bound, on both sides --------------------------------------


def test_a_gap_of_exactly_the_bound_is_still_believed():
    until = at(MONDAY, 2)
    gap = basement_watch.MAX_ATTRIBUTION_GAP.total_seconds()
    rows = outdoor_cover(until, 5.0)
    opens = until - basement_watch.ATTRIBUTION_LOOKBACK.total_seconds()
    hole = opens + 6 * 3600
    kept = [(ts, c) for ts, c in rows if not hole < ts < hole + gap]
    assert basement_watch.attributable_high(kept, until) == 5.0


def test_one_second_past_the_bound_is_not():
    until = at(MONDAY, 2)
    gap = basement_watch.MAX_ATTRIBUTION_GAP.total_seconds()
    rows = outdoor_cover(until, 5.0)
    opens = until - basement_watch.ATTRIBUTION_LOOKBACK.total_seconds()
    hole = opens + 6 * 3600
    kept = [(ts, c) for ts, c in rows if not hole < ts < hole + gap + 1]
    assert basement_watch.attributable_high(kept, until) is None


# --- the latch's own horizon ------------------------------------------------


def test_a_run_reaching_the_querys_first_row_is_unknown_not_clear():
    """A basement warm for longer than the evidence must not read as cooled.

    The trip is outside every row the query returns and every surviving
    reading is attributable, so there is nothing left to flag on — but no
    reading was ever at or below 62 °F either, which is the only thing #205
    says clearing means.
    """
    horizon = basement_watch.history_lookback().total_seconds()
    now = at(SATURDAY, 12)
    rows = basement_run(now - horizon, WARM, int(horizon / 60) + 1)
    state = basement_watch.watch(rows, outdoor_cover(now, 20.0, hours=70), now)
    assert state.status == UNKNOWN
    assert state.since is None


def test_a_short_record_that_never_cleared_is_judged_normally():
    """Control: 'no clear reading' is not the same as 'the run ran off the end'.

    A sensor installed two hours ago has no clear reading either, and its run
    is entirely visible. Same shape as the test above, two hours instead of
    forty-two, and it must still reach a verdict.
    """
    now = at(SATURDAY, 3)
    rows = basement_run(at(SATURDAY, 2), WARM, 61)
    assert basement_watch.watch(rows, outdoor_cover(now, 5.0), now).status == ON


def test_a_standing_watch_keeps_its_stamp_for_the_whole_horizon():
    """The behavioural claim `history_lookback`'s docstring makes.

    A trip at the weekday window's open is still reported with its own stamp
    at the window's close eighteen hours later, with the basement never
    cooling. Asserted through `watch` rather than by recomputing the formula:
    a test that rebuilds `(24 - opens) + closes` catches a constant edit and
    says nothing about the latch.
    """
    opens, _ = basement_watch.WEEKDAY_WINDOW
    trip_at = at(MONDAY, opens)
    now = trip_at + 18 * 3600
    minutes = int((now - trip_at) / 60) + 1
    state = basement_watch.watch(
        basement_run(trip_at, WARM, minutes),
        outdoor_cover(now, 5.0, hours=50),
        now,
    )
    assert state.status == ON
    assert state.since == trip_at
