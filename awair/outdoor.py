"""Poll Open-Meteo for outdoor weather + air quality and store readings.

Run as: python -m awair.outdoor
Config via environment:
  AWAIR_LAT, AWAIR_LON              — required, parcel coords (Ansible-templated)
  AWAIR_DB                          — shared with the indoor poller
  AWAIR_OUTDOOR_POLL_SECONDS        — default 900 (15 min, the native cadence)
  AWAIR_OUTDOOR_WEATHER_URL         — override for test/staging (see DEFAULT_WEATHER_URL)
  AWAIR_OUTDOOR_AIR_QUALITY_URL     — override for test/staging (see DEFAULT_AIR_QUALITY_URL)
  AWAIR_OUTDOOR_HEALTH_POLLS        — default 4 (~1h at the default cadence); see OutdoorHealth
  AWAIR_NTFY_URL, AWAIR_NTFY_TOPIC, AWAIR_NTFY_TOKEN
                                    — shared with the indoor poller; a sustained failure notifies

Weather refreshes every 15 min at the source; air quality (CAMS-backed) is
hourly. Both are fetched every 15 min and merged into one row keyed on the
weather endpoint's `current.time` — inserts are idempotent via INSERT OR
IGNORE, so a re-poll before Open-Meteo refreshes writes nothing.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime

from awair import db
from awair.alerts import Notifier
from awair.monitor import OutdoorHealth
from awair.shutdown import install_handler

# A trailing ISO zone designator, stripped before the date-only test in
# `_is_date_only`. Two digits are required before the optional colon so this
# cannot match the `-1` of an ISO week date like "2026-W28-1".
_ZONE_SUFFIX = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

log = logging.getLogger("awair.outdoor")

FETCH_TIMEOUT_SECONDS = 10
DEFAULT_POLL_SECONDS = 900
DEFAULT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WEATHER_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "pressure_msl",
    "precipitation",
    # WMO interpretation code (#71) -- the only field here that can say
    # "Overcast" rather than a number. Requested and stored as the source's
    # integer; the word is the hub's job (GLOSSARY.md: `weather_code`).
    "weather_code",
)
AIR_QUALITY_FIELDS = (
    "pm2_5",
    "pm10",
    "us_aqi",
    "carbon_monoxide",
    "ozone",
)

# Map Open-Meteo `current.*` keys onto our column names. Kept explicit so a
# rename on either side is a one-line change and reviewers can see the mapping.
WEATHER_TO_COLUMN = {
    "temperature_2m": "temp",
    "relative_humidity_2m": "humid",
    "wind_speed_10m": "wind_speed",
    "pressure_msl": "pressure",
    "precipitation": "precipitation",
    "weather_code": "weather_code",
}
AIR_QUALITY_TO_COLUMN = {
    "pm2_5": "pm25",
    "pm10": "pm10",
    "us_aqi": "us_aqi",
    "carbon_monoxide": "co",
    "ozone": "o3",
}


def _is_date_only(source_time) -> bool:
    """True when the value is a complete ISO *date* carrying no time of day.

    `date.fromisoformat` is the discriminator rather than a `"T" in s` character
    test, because it is the stdlib's own definition of the shape and so tracks
    it. #77 proposed rejecting any string without a `T` or a `:`; that rule is
    *almost* right and has one false reject -- `"20260712 0400"`, ISO basic date
    with a space separator, is a real 04:00 and carries neither character.

    **The zone designator has to come off first, and this is not hypothetical
    tidying.** `datetime.fromisoformat` accepts *any single character* as the
    date/time separator, so it reads `"2026-07-12+05:00"` as the 12th at 05:00
    -- while `date.fromisoformat` rejects the same string. Without the strip
    that pair slips the guard and stores an observation time of 05:00 that the
    source never published: the same defect as the midnight case, one hour
    further from the truth and with no warning at all. `"-05:00"` behaves
    identically. Found in review on #77, not by the original measurement, which
    is why the sweep below names its classes rather than claiming a universal.

    The pattern needs two digits before the optional colon, so it cannot eat
    the `-1` of the ISO week date `"2026-W28-1"`, and it needs a leading `+`,
    `-` or `Z`, so it cannot eat the `0400` of `"20260712 0400"`. Measured over
    9 date-only and 12 clocked spellings: zero misses, zero false rejects.

    Non-strings raise TypeError here and are reported as such, which keeps them
    on the existing `_normalize_source_time` error path rather than being
    mislabelled as date-only.
    """
    if not isinstance(source_time, str):
        return False
    try:
        date.fromisoformat(_ZONE_SUFFIX.sub("", source_time))
    except ValueError:
        return False
    return True


def _normalize_source_time(source_time: str) -> str:
    """Canonicalize Open-Meteo's `current.time` to a full ISO UTC string.

    Open-Meteo returns `"YYYY-MM-DDTHH:MM"` when polled with `timezone=UTC` —
    minute precision, naive. Storing that verbatim breaks lexicographic
    `WHERE ts >= ?` filters because the short form sorts *before* the full
    ISO strings that callers pass in via `since.isoformat()`. Normalize
    both sides to `"YYYY-MM-DDTHH:MM:00+00:00"` so string comparison equals
    time comparison.

    **This only stamps `tzinfo` on a *naive* value.** A source time arriving
    with a real non-UTC offset is stored verbatim as e.g. `...+05:00`, which
    sorts and range-filters wrongly on `ts`, while `web._iso_utc` (which does
    call `astimezone`) would still *publish* it correctly -- so the split is
    silent in both directions. `timezone=UTC` in `_build_url` is the only
    thing keeping it from arising, which is why
    `test_build_url_requests_source_units_and_utc` pins that parameter.
    (This paragraph lived on `db.latest_outdoor_reading` until #77. It
    describes this function's behaviour, and that one can neither cause nor
    fix it.)

    **A value carrying no time of day raises `ValueError` rather than being
    silently clocked at midnight** (#91). `datetime.fromisoformat` accepts
    `"2026-07-12"`, `"20260712"`, the ISO week date `"2026-W28-1"` and even
    `"2026-07-12+05:00"`, defaulting the clock to `00:00` (or, for the last,
    reading the offset as the time) -- so without this the caller stores an
    observation instant the source never published. #77 put that guard on the
    auxiliary clock only, and said why: `poll_once` caught `KeyError` alone, so
    raising here would have killed the poller process instead of the row. That
    constraint is gone -- `poll_once` now catches `TypeError` and `ValueError`
    too -- so the guard belongs here, where it covers both clocks rather than
    one.
    """
    if _is_date_only(source_time):
        raise ValueError(f"source time carries no time of day: {source_time!r}")
    parsed = datetime.fromisoformat(source_time)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _normalize_aq_time(source_time) -> str | None:
    """`_normalize_source_time` for the auxiliary air-quality clock, or None.

    Same canonicalisation, different failure policy. The weather block's time
    is the row's primary key, so a bad value there must stop the row. This one
    only annotates the AQ columns, so a bad or absent value degrades to NULL —
    which is already the exact signal the consumer is told to act on ("no
    current AQI"), so nothing has to be invented to represent it.

    Normalising rather than storing verbatim matters for the same reason it
    does for `ts`: the two are meant to be *subtracted*, and Open-Meteo's
    minute-precision naive form would compare as a different kind of thing.

    A **date-only** value degrades to NULL too, rather than to midnight (#77).
    `datetime.fromisoformat` accepts `"2026-07-12"`, `"20260712"` and the ISO
    week date `"2026-W28-1"` and silently defaults the clock to `00:00`, which
    would store an observation time the source never published -- the exact
    thing GLOSSARY says this column exists to prevent. An explicitly published
    midnight (`"2026-07-12T00:00"`) is a real reading and still stored, so the
    test is structural, not a comparison against the parsed value.
    """
    # Behaviourally redundant -- `_normalize_source_time(None)` raises TypeError
    # and `("")` raises ValueError, both caught below. It is kept for the log:
    # an *absent* AQ time is the ordinary shape of a partial poll and should not
    # warn every 15 minutes, while a *malformed* one is upstream drift worth
    # seeing. Deleting it is a live mutation survivor against the assertions
    # alone, so the distinction is pinned on the log instead
    # (`test_an_absent_aq_time_is_silent_but_a_malformed_one_warns`).
    if not source_time:
        return None
    # Also behaviourally redundant since #91 -- `_normalize_source_time` raises
    # `ValueError` on a clockless value now, and the `except` below would
    # degrade it to NULL anyway. Kept for the same reason as the branch above:
    # the message. "carries no time of day" is upstream publishing a shape we
    # refuse on purpose; "unparseable" is upstream being broken, and a reader
    # scanning a 15-minute warning cadence needs to know which.
    if _is_date_only(source_time):
        log.warning(
            "air-quality current.time carries no time of day (%r); storing NULL "
            "rather than inventing midnight",
            source_time,
        )
        return None
    try:
        return _normalize_source_time(source_time)
    except (TypeError, ValueError) as exc:
        log.warning("air-quality current.time unparseable (%r): %s", source_time, exc)
        return None


def _is_readable_air_quality(payload) -> bool:
    """True when the AQ block is an object whose `current` block is one too.

    Anything else -- a JSON list, string or number, or a `current` that is one
    -- raises `AttributeError` out of `.get`. That is the same process-death
    class as #91's weather clock (it unwinds the `while` loop in `main()` and
    systemd restarts into the same upstream value) and the #91 fix did not
    cover it, because `AttributeError` is neither of the two exception types
    the weather side raises. Found in review on #91, not by its measurement.

    Read as an unusable *fetch* rather than a bad row: the weather half is
    still worth writing, which is precisely what `"partial"` means. `None`
    -- the shape `poll_once` uses for a failed AQ fetch -- is unreadable by
    the same token, so this one predicate covers both callers.
    """
    if not isinstance(payload, dict):
        return False
    return isinstance(payload.get("current", {}), dict)


def _build_url(base: str, lat: float, lon: float, fields: tuple) -> str:
    """Build one Open-Meteo `current=` request.

    **Do not add a unit override here.** `web.OUTDOOR_LATEST_FIELDS` publishes
    `C` / `hPa` / `km/h` / `mm` to the hub as fixed strings, and those are
    correct only because this request asks for Open-Meteo's defaults. A
    `wind_speed_unit` or `temperature_unit` parameter would change the stored
    values and leave the published labels behind, silently. `timezone=UTC` is
    load-bearing for the same reason -- see `_normalize_source_time`.
    Pinned by `test_build_url_requests_source_units_and_utc`.
    """
    params = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": ",".join(fields),
            "timezone": "UTC",
        }
    )
    return f"{base}?{params}"


def make_fetch(url: str):
    def fetch() -> str:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def parse_reading(
    weather_payload: dict, air_quality_payload: dict, received_at: str
) -> dict:
    """Merge one weather + one air-quality payload into an outdoor_readings row.

    The weather endpoint's `current.time` is the row's dedup key (`ts`); the
    air-quality endpoint has its own `current.time` (hourly) and is treated
    as auxiliary — its values are stitched in but its timestamp is not the
    primary key. Missing fields (either endpoint) degrade to NULL rather
    than halting; upstream schema drift is a warning, not an outage.

    That auxiliary timestamp is **stored** as `aq_ts` (#71) rather than read
    and dropped. Both blocks are fetched every 15 min but air quality is
    CAMS-backed and hourly, so a row stamped 14:15 routinely carries a `us_aqi`
    measured at 13:00. Without `aq_ts` nothing downstream can tell, and the
    hub's weather verdict is driven mostly by AQI — the one number in this
    payload that can genuinely be bad. `received_at` does not substitute: that
    is our poll time, not anyone's observation time.

    A malformed `current.time` on the air-quality block degrades to NULL rather
    than raising, because it is auxiliary — the weather half of the row is
    still worth writing. The weather block's own `time` keeps raising, since
    without it there is no primary key and no row at all.
    """
    weather_current = weather_payload["current"]
    reading = {col: None for col in db.OUTDOOR_COLUMNS}
    reading["ts"] = _normalize_source_time(weather_current["time"])
    reading["received_at"] = received_at
    for source_field, column in WEATHER_TO_COLUMN.items():
        reading[column] = weather_current.get(source_field)
    if _is_readable_air_quality(air_quality_payload):
        aq_current = air_quality_payload.get("current", {})
        for source_field, column in AIR_QUALITY_TO_COLUMN.items():
            reading[column] = aq_current.get(source_field)
        reading["aq_ts"] = _normalize_aq_time(aq_current.get("time"))
    return reading


def poll_once(conn, fetch_weather, fetch_air_quality) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'error', 'partial'.
    'partial' = weather succeeded but air quality failed; the row is
    still inserted with AQ columns NULL because trend data on the
    weather side is more valuable than "all or nothing" here.
    """
    try:
        weather_payload = json.loads(fetch_weather())
    except (OSError, TypeError, ValueError, KeyError) as exc:
        # TypeError: a fetcher returning None is `json.loads(None)`. Same
        # escaping-and-exiting shape as the clock defect, same slot in the
        # tuple (#91 review).
        log.warning("weather fetch failed: %s", exc)
        return "error"
    try:
        air_quality_payload = json.loads(fetch_air_quality())
        if not _is_readable_air_quality(air_quality_payload):
            raise TypeError(
                f"payload is a {type(air_quality_payload).__name__}, "
                "or its `current` block is"
            )
        status = "ok"
    except (OSError, TypeError, ValueError, KeyError) as exc:
        # Raising to this handler rather than branching around it is deliberate:
        # an unreadable AQ block and a failed AQ fetch have the same remedy
        # (drop the block, keep the weather row, report "partial"), so they
        # should not have two code paths that can drift apart.
        log.warning("air-quality block unusable: %s", exc)
        air_quality_payload = None
        status = "partial"
    try:
        reading = parse_reading(
            weather_payload,
            air_quality_payload,
            received_at=datetime.now(UTC).isoformat(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # KeyError: `current` or `time` absent. ValueError: `time` unparseable,
        # or clockless and refused by `_normalize_source_time`. TypeError: a
        # JSON `null` time reaching `fromisoformat`, or a non-dict `current`.
        #
        # All three used to escape and unwind the `while` loop in `main()`, so
        # a single bad upstream clock exited the process; systemd restarted it
        # into the same value (#91). Returning "error" costs one poll instead,
        # and matches the documented contract for a fetch failure or bad JSON.
        #
        # TypeError is not optional. #91 prescribed `(KeyError, ValueError)`,
        # which leaves a `"time": null` payload killing the poller exactly as
        # before -- pinned by `test_poll_once_survives_a_bad_weather_time`.
        log.warning("unusable weather payload: %s: %s", type(exc).__name__, exc)
        return "error"
    inserted = db.insert_outdoor_reading(conn, reading)
    if not inserted:
        return "duplicate"
    return "inserted" if status == "ok" else "partial"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"{name} is required (parcel coordinates are templated from Ansible)"
        )
    return value


def handle_outdoor_health(conn, notifier, health, status, now, interval) -> None:
    """Map an `OutdoorHealth` verdict onto an alert event + notification.

    Sibling of `poller.handle_device_health`, and it opens its event under
    `metric="outdoor"` rather than reusing `"device"` for a reason that is a
    correctness bug rather than tidiness: `db.get_open_events` returns at most
    one open event **per metric**, and the two pollers are separate processes
    against one DB (two systemd units, one `AWAIR_DB`). Sharing the key means an
    outdoor recovery closes the indoor poller's open `unreachable` row and pages
    "Awair Element recovered" while the Element is still down (reproduced #94).

    `web._NON_MEASUREMENT_METRICS` carries `"outdoor"`, but **not** for the
    reason it carries `"device"`. That one is filtered because the hub sees the
    same outage sooner in `received_at`; that argument does not transfer, since
    `/api/latest` publishes the *indoor* `received_at` and a `"partial"` poll
    writes a row anyway. The real reason is narrower: `/api/latest` is the
    indoor contract, so an outdoor transport fact there is a category error.
    A broken AQ endpoint is already visible to the hub on `/api/outdoor-latest`
    as a NULL `aq_ts`, which #71 renders as "no current AQI" — so nothing is
    lost by keeping this event off the indoor endpoint. Whether it should be
    published on `/api/outdoor-latest` too is deliberately left open.

    Only `unreachable` pages at high priority. `degraded` and `stale` are
    Open-Meteo publishing badly; they are worth a notification so a sustained
    outage is not silent, but waking someone for a fault they cannot fix is how
    a channel gets muted.
    """
    verdict = health.observe(status)
    if verdict in health.TIERS.values():
        notified = notifier.send(
            f"Outdoor poller {verdict} (~{_health_window(health, interval)} of polls)",
            title=f"Outdoor {verdict}",
            priority="high" if verdict == "unreachable" else "default",
        )
        db.open_event(
            conn,
            metric="outdoor",
            tier=verdict,
            opened_at=now,
            value=None,
            baseline=None,
            threshold=None,
            notified=notified,
        )
    elif verdict == "recovered":
        event = db.get_open_events(conn).get("outdoor")
        notified = notifier.send("Outdoor poller recovered", title="Outdoor recovered")
        if event:
            db.close_event(conn, event["id"], closed_at=now, notified=notified)


def _health_window(health, interval) -> str:
    """The alert threshold as wall-clock, e.g. "1h".

    The poll count on its own is meaningless without the cadence beside it, and
    the cadence differs from the indoor poller's by 30x — so the message says
    the duration, as `poller.handle_device_health`'s does.

    Takes `interval` rather than re-reading `AWAIR_OUTDOOR_POLL_SECONDS`: a pure
    formatter reaching into the environment is a second source of truth for a
    value `main()` has already resolved. Rounds rather than truncating, so a
    window under a minute does not render as "0 min".
    """
    seconds = health.threshold * interval
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds}s"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    lat = float(_require_env("AWAIR_LAT"))
    lon = float(_require_env("AWAIR_LON"))
    db_path = os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    interval = int(os.environ.get("AWAIR_OUTDOOR_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    weather_base = os.environ.get("AWAIR_OUTDOOR_WEATHER_URL", DEFAULT_WEATHER_URL)
    air_quality_base = os.environ.get(
        "AWAIR_OUTDOOR_AIR_QUALITY_URL", DEFAULT_AIR_QUALITY_URL
    )

    notifier = Notifier(
        base_url=os.environ.get(
            "AWAIR_NTFY_URL", "https://notifications.tomclancy.info"
        ),
        topic=os.environ.get("AWAIR_NTFY_TOPIC", "awair"),
        token=os.environ.get("AWAIR_NTFY_TOKEN", ""),
    )
    health = OutdoorHealth(
        threshold=int(
            os.environ.get(
                "AWAIR_OUTDOOR_HEALTH_POLLS", OutdoorHealth.DEFAULT_THRESHOLD
            )
        )
    )

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)

    fetch_weather = make_fetch(_build_url(weather_base, lat, lon, WEATHER_FIELDS))
    fetch_air_quality = make_fetch(
        _build_url(air_quality_base, lat, lon, AIR_QUALITY_FIELDS)
    )
    log.info(
        "polling Open-Meteo every %ss for (%s, %s) into %s", interval, lat, lon, db_path
    )

    # Same clean-shutdown contract as the indoor poller (#83).
    stop = install_handler()
    try:
        while not stop.is_set():
            status = poll_once(conn, fetch_weather, fetch_air_quality)
            log.log(
                logging.INFO
                if status in ("inserted", "duplicate")
                else logging.WARNING,
                "outdoor poll: %s",
                status,
            )
            handle_outdoor_health(
                conn, notifier, health, status, datetime.now(UTC), interval
            )
            if stop.wait(interval):
                break
    finally:
        conn.close()
    log.info("outdoor poller stopped cleanly")


if __name__ == "__main__":
    main()
