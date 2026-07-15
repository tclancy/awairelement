"""Sunrise/sunset event generation (#32)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from awair import solar


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AWAIR_LAT", "AWAIR_LON", "AWAIR_TZ"):
        monkeypatch.delenv(var, raising=False)


def test_daily_events_empty_when_coords_missing():
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = since + timedelta(days=7)
    assert solar.daily_events(since, until) == []


def test_daily_events_seven_days_two_events_per_day(monkeypatch):
    # 22 Parsons parcel — Portsmouth, NH area
    monkeypatch.setenv("AWAIR_LAT", "43.0")
    monkeypatch.setenv("AWAIR_LON", "-70.8")
    monkeypatch.setenv("AWAIR_TZ", "America/New_York")
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = since + timedelta(days=7)
    events = solar.daily_events(since, until)
    # 7-day window, two events per day → 14; boundary trimming may drop one
    # sunset near `until` if it falls after the cutoff, so allow a small slack.
    assert 12 <= len(events) <= 14
    kinds = {e["kind"] for e in events}
    assert kinds == {"sunrise", "sunset"}


def test_daily_events_sorted_and_within_window(monkeypatch):
    monkeypatch.setenv("AWAIR_LAT", "43.0")
    monkeypatch.setenv("AWAIR_LON", "-70.8")
    monkeypatch.setenv("AWAIR_TZ", "America/New_York")
    since = datetime(2026, 6, 15, tzinfo=timezone.utc)
    until = since + timedelta(days=3)
    events = solar.daily_events(since, until)
    since_ts = int(since.timestamp())
    until_ts = int(until.timestamp())
    for ev in events:
        assert since_ts <= ev["ts"] <= until_ts
        assert ev["kind"] in ("sunrise", "sunset")
    ts_values = [e["ts"] for e in events]
    assert ts_values == sorted(ts_values)


def test_daily_events_kind_alternates(monkeypatch):
    """Portsmouth in June: sunrise precedes sunset within each day."""
    monkeypatch.setenv("AWAIR_LAT", "43.0")
    monkeypatch.setenv("AWAIR_LON", "-70.8")
    monkeypatch.setenv("AWAIR_TZ", "America/New_York")
    since = datetime(2026, 6, 15, tzinfo=timezone.utc)
    until = since + timedelta(days=2)
    events = solar.daily_events(since, until)
    kinds = [e["kind"] for e in events]
    # No two consecutive sunrises or sunsets — the pattern alternates.
    for a, b in zip(kinds, kinds[1:]):
        assert a != b


def test_coords_malformed_env(monkeypatch, caplog):
    """Malformed AWAIR_LAT/AWAIR_LON returns [] instead of a ValueError.

    Bot review on PR #37 flagged the failure chain: `_coords()` propagates
    ValueError → `/api/outdoor-series` 500 → dashboard `load()` aborts →
    all 8 charts blank on every 5-min refresh. Same tolerance as the unset
    case: no markers is fine, blank dashboard is not.
    """
    monkeypatch.setenv("AWAIR_TZ", "America/New_York")
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = since + timedelta(days=2)

    for lat, lon in [
        ("43,0", "-70,8"),  # comma-decimal (European locale typo)
        ("not-a-number", "-70.8"),
        ("43.0", "seventy west"),
        ("43.0.0", "-70.8"),  # extra dot
    ]:
        monkeypatch.setenv("AWAIR_LAT", lat)
        monkeypatch.setenv("AWAIR_LON", lon)
        with caplog.at_level("WARNING", logger="awair.solar"):
            events = solar.daily_events(since, until)
        assert events == []
        assert any("malformed" in rec.message for rec in caplog.records), (
            f"expected a malformed-coords warning for ({lat!r}, {lon!r})"
        )
        caplog.clear()


def test_daily_events_utc_default_when_tz_unset(monkeypatch):
    """Missing AWAIR_TZ falls back to UTC — no crash, still emits events."""
    monkeypatch.setenv("AWAIR_LAT", "43.0")
    monkeypatch.setenv("AWAIR_LON", "-70.8")
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = since + timedelta(days=2)
    events = solar.daily_events(since, until)
    assert len(events) >= 2
