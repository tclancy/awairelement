"""Sunrise/sunset event generation for the dashboard's daily-cycle markers.

Computed locally from parcel coords (``AWAIR_LAT`` / ``AWAIR_LON``) and the
configured timezone (``AWAIR_TZ``, default UTC). No network fetch — astral is
pure Python, so the events materialize deterministically at request time
without adding a poll or a new column to ``outdoor_readings`` (#32).

The dashboard consumes the returned events as unix timestamps + kind, so the
plugin that draws the ☀ / 🌙 glyphs never needs to know about timezone or
astral internals.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun


def _tz() -> ZoneInfo | timezone:
    name = os.environ.get("AWAIR_TZ", "").strip()
    if not name:
        return timezone.utc
    return ZoneInfo(name)


def _coords() -> tuple[float, float] | None:
    lat = os.environ.get("AWAIR_LAT", "").strip()
    lon = os.environ.get("AWAIR_LON", "").strip()
    if not (lat and lon):
        return None
    return float(lat), float(lon)


def daily_events(since: datetime, until: datetime) -> list[dict]:
    """Return sunrise/sunset events between *since* and *until*, tz-configured.

    Both bounds are UTC-aware datetimes (mirrors the outdoor-series window).
    Returns ``[{"ts": int, "kind": "sunrise" | "sunset"}, ...]`` sorted by ts.
    Events outside the window are dropped so the strip only paints what the
    chart actually shows.

    When ``AWAIR_LAT`` / ``AWAIR_LON`` are unset the function returns an empty
    list — homelab dev environments without geo config get a clean render
    rather than a startup error.
    """
    coords = _coords()
    if coords is None:
        return []
    lat, lon = coords
    tz = _tz()
    location = LocationInfo(latitude=lat, longitude=lon, timezone=str(tz))

    # Iterate one calendar day at a time in the display tz — astral's ``sun()``
    # is keyed on a local date, and the tz determines whether "today" starts
    # 4 hours earlier or later than UTC's midnight.
    since_local = since.astimezone(tz)
    until_local = until.astimezone(tz)
    day = since_local.date()
    end_day = until_local.date()

    events: list[dict] = []
    while day <= end_day:
        try:
            s = sun(location.observer, date=day, tzinfo=tz)
        except ValueError:
            # Polar day/night — no sunrise or sunset at this latitude/date.
            day += timedelta(days=1)
            continue
        for kind in ("sunrise", "sunset"):
            moment = s[kind]
            if since <= moment.astimezone(timezone.utc) <= until:
                events.append({"ts": int(moment.timestamp()), "kind": kind})
        day += timedelta(days=1)

    events.sort(key=lambda e: e["ts"])
    return events
