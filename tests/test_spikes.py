"""Spike detection: baseline math, tiers, hysteresis, re-arm, cold start.

History shapes are synthetic 30s-cadence series; every scenario here maps to
a rule in SCOPE.md's Spike Detection section.
"""

from datetime import datetime, timedelta, timezone

from awair.spikes import METRICS, Decision, baseline_spread, evaluate

CO2 = METRICS["co2"]
PM25 = METRICS["pm25"]

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def history(values, end=NOW, step_seconds=30):
    """Build (ts, value) pairs ending at `end`, spaced step_seconds apart."""
    n = len(values)
    return [
        (end - timedelta(seconds=step_seconds * (n - 1 - i)), float(v))
        for i, v in enumerate(values)
    ]


def hours_of(value, hours, end=NOW):
    return history([value] * int(hours * 3600 / 30), end=end)


def open_event(tier="relative", opened_at=NOW - timedelta(hours=1), renotified_at=None):
    return {
        "id": 1,
        "metric": "co2",
        "tier": tier,
        "opened_at": opened_at,
        "renotified_at": renotified_at,
    }


# --- baseline & spread ---


def test_baseline_is_median_and_spread_uses_mad():
    values = [400.0, 410.0, 420.0, 430.0, 800.0]  # outlier must not drag baseline
    baseline, spread = baseline_spread(values, floor=1.0)
    assert baseline == 420.0
    assert spread == 10.0  # median absolute deviation


def test_spread_floor_applies_when_mad_collapses():
    baseline, spread = baseline_spread([500.0] * 100, floor=CO2.floor)
    assert baseline == 500.0
    assert spread == CO2.floor  # flatline: MAD=0 must not mean hair-trigger


# --- tier 1: relative spikes ---


def test_flatline_never_opens_even_with_small_noise():
    # Overnight CO2: dead flat with ±1 ppm noise. 6×floor(50)=300 over baseline
    # is required, so noise must not alert.
    h = hours_of(500, 8)
    h = h[:-4] + history([501, 502, 501, 502], end=NOW)
    assert evaluate(CO2, h, None, NOW) is None


def test_sustained_relative_spike_opens_after_m_consecutive():
    h = hours_of(500, 8)[: -CO2.m] + history([900] * CO2.m, end=NOW)
    decision = evaluate(CO2, h, None, NOW)
    assert decision == Decision(
        action="open", tier="relative", value=900.0,
        baseline=500.0, threshold=500.0 + CO2.k * CO2.floor,
    )


def test_relative_spike_needs_all_m_polls_above():
    # M-1 high readings then the latest back at baseline: no event.
    h = hours_of(500, 8)[: -CO2.m] + history([900] * (CO2.m - 1) + [500], end=NOW)
    assert evaluate(CO2, h, None, NOW) is None


def test_single_sample_blip_does_not_open():
    h = hours_of(500, 8)[:-1] + history([2000], end=NOW)
    assert evaluate(CO2, h, None, NOW) is None


def test_cold_start_disables_tier1():
    # Only 2h of history: relative detection off.
    h = hours_of(500, 2)[: -CO2.m] + history([900] * CO2.m, end=NOW)
    assert evaluate(CO2, h, None, NOW) is None


# --- tier 2: absolute ceilings ---


def test_ceiling_opens_after_two_consecutive_even_cold( ):
    # Ceilings are live from the first readings, even without 6h history.
    h = history([400, 1300, 1350], end=NOW)
    decision = evaluate(CO2, h, None, NOW)
    assert decision.action == "open"
    assert decision.tier == "ceiling"
    assert decision.threshold == CO2.ceiling


def test_ceiling_single_sample_does_not_open():
    # A dust puff: one PM2.5 reading over 35 must not page.
    h = hours_of(5, 8)[:-1] + history([80], end=NOW)
    assert evaluate(PM25, h, None, NOW) is None


# --- close: hysteresis needs BOTH conditions sustained ---


def test_closes_after_sustained_recovery():
    calm = 500.0
    h = hours_of(calm, 8)  # last 10+ minutes all well below both thresholds
    decision = evaluate(CO2, h, open_event(), NOW)
    assert decision.action == "close"


def test_does_not_close_while_still_above_relative_threshold():
    # Under the 1200 ceiling but still 6+ MAD above baseline: stays open.
    h = hours_of(500, 8)[:-40] + history([1100] * 40, end=NOW)
    assert evaluate(CO2, h, open_event(), NOW) is None


def test_does_not_close_until_recovery_spans_close_window():
    # Recovered for only ~2 minutes: too soon.
    h = hours_of(500, 8)[:-60] + history([1300] * 56 + [500] * 4, end=NOW)
    assert evaluate(CO2, h, open_event(), NOW) is None


# --- re-arm: long-lived events send one reminder per 12h ---


def test_still_elevated_renotify_after_12h():
    h = hours_of(500, 8)[:-40] + history([1100] * 40, end=NOW)
    event = open_event(opened_at=NOW - timedelta(hours=13))
    decision = evaluate(CO2, h, event, NOW)
    assert decision.action == "renotify"


def test_no_second_renotify_within_12h():
    h = hours_of(500, 8)[:-40] + history([1100] * 40, end=NOW)
    event = open_event(
        opened_at=NOW - timedelta(hours=20),
        renotified_at=NOW - timedelta(hours=2),
    )
    assert evaluate(CO2, h, event, NOW) is None


def test_no_renotify_before_12h():
    h = hours_of(500, 8)[:-40] + history([1100] * 40, end=NOW)
    assert evaluate(CO2, h, open_event(opened_at=NOW - timedelta(hours=3)), NOW) is None
