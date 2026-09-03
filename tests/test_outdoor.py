"""Open-Meteo outdoor poller: parse, dedup, error and partial paths."""

import json
import logging
from urllib.error import URLError

import pytest

from awair import outdoor
from awair.outdoor import (
    AIR_QUALITY_FIELDS,
    WEATHER_FIELDS,
    _build_url,
    _require_env,
    make_fetch,
    parse_reading,
    poll_once,
)

RECEIVED = "2026-07-12T04:30:00+00:00"

WEATHER = {
    "current": {
        "time": "2026-07-12T04:30",
        "interval": 900,
        "temperature_2m": 22.4,
        "relative_humidity_2m": 68,
        "wind_speed_10m": 3.2,
        "pressure_msl": 1013.2,
        "precipitation": 0.0,
        # Requested by WEATHER_FIELDS since #71, so every real response carries
        # it. Kept on the shared fixture rather than only on the ad-hoc payload
        # in the #71 block below: a fixture that omits a field production
        # always sends is a shape production never produces, and the poll_once
        # / main end-to-end tests drive off this one.
        "weather_code": 3,
    }
}
WEATHER_TEXT = json.dumps(WEATHER)

AIR_QUALITY = {
    "current": {
        "time": "2026-07-12T04:00",
        "pm2_5": 5.6,
        "pm10": 8.1,
        "us_aqi": 32,
        "carbon_monoxide": 200,
        "ozone": 55,
    }
}
AIR_QUALITY_TEXT = json.dumps(AIR_QUALITY)


def test_parse_reading_merges_weather_and_air_quality():
    reading = parse_reading(WEATHER, AIR_QUALITY, received_at=RECEIVED)
    # Open-Meteo's minute-precision naive `current.time` is normalized to a
    # full ISO UTC string so lexicographic `ts >= ?` filters work correctly.
    assert reading["ts"] == "2026-07-12T04:30:00+00:00"
    assert reading["received_at"] == RECEIVED
    assert reading["temp"] == 22.4
    assert reading["humid"] == 68
    assert reading["wind_speed"] == 3.2
    assert reading["pressure"] == 1013.2
    assert reading["precipitation"] == 0.0
    assert reading["pm25"] == 5.6
    assert reading["pm10"] == 8.1
    assert reading["us_aqi"] == 32
    assert reading["co"] == 200
    assert reading["o3"] == 55


def test_parse_reading_tolerates_missing_air_quality_field():
    aq = {"current": dict(AIR_QUALITY["current"])}
    del aq["current"]["ozone"]
    reading = parse_reading(WEATHER, aq, received_at=RECEIVED)
    assert reading["o3"] is None
    assert reading["pm25"] == 5.6


def test_parse_reading_tolerates_missing_weather_field():
    """A weather-endpoint schema drift dropping a field falls back to NULL."""
    payload = {"current": dict(WEATHER["current"])}
    del payload["current"]["precipitation"]
    reading = parse_reading(payload, AIR_QUALITY, received_at=RECEIVED)
    assert reading["precipitation"] is None
    assert reading["temp"] == 22.4


def test_parse_reading_requires_weather_time():
    payload = {"current": dict(WEATHER["current"])}
    del payload["current"]["time"]
    with pytest.raises(KeyError):
        parse_reading(payload, AIR_QUALITY, received_at=RECEIVED)


def test_parse_reading_normalizes_naive_open_meteo_time():
    """Prod payloads carry a naive `HH:MM` string; storage needs full ISO+tz."""
    reading = parse_reading(WEATHER, AIR_QUALITY, received_at=RECEIVED)
    # Full ISO with UTC offset — sorts lexicographically alongside
    # `since.isoformat()` values from callers.
    assert reading["ts"] == "2026-07-12T04:30:00+00:00"


def test_parse_reading_null_air_quality_gives_null_aq_columns():
    """Partial-fetch path: weather succeeded, AQ endpoint failed."""
    reading = parse_reading(WEATHER, None, received_at=RECEIVED)
    assert reading["temp"] == 22.4
    assert reading["pm25"] is None
    assert reading["us_aqi"] is None


def test_poll_once_inserts_fresh_row(conn):
    status = poll_once(
        conn,
        fetch_weather=lambda: WEATHER_TEXT,
        fetch_air_quality=lambda: AIR_QUALITY_TEXT,
    )
    assert status == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


def test_poll_once_reports_duplicate_source_time(conn):
    poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT)
    assert (
        poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT) == "duplicate"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1


def test_poll_once_weather_error_returns_error_without_inserting(conn):
    def failing():
        raise URLError("weather down")

    assert poll_once(conn, failing, lambda: AIR_QUALITY_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_poll_once_air_quality_failure_still_inserts_partial(conn):
    """AQ endpoint outage must not wedge weather ingestion."""

    def failing():
        raise URLError("aq down")

    status = poll_once(conn, lambda: WEATHER_TEXT, failing)
    assert status == "partial"
    row = conn.execute("SELECT temp, pm25, us_aqi FROM outdoor_readings").fetchone()
    assert row == (22.4, None, None)


def test_poll_once_bad_json_reports_error(conn):
    assert poll_once(conn, lambda: "<html>", lambda: AIR_QUALITY_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_build_url_encodes_params():
    url = _build_url("https://example.test/x", 43.1, -70.9, WEATHER_FIELDS)
    assert url.startswith("https://example.test/x?")
    assert "latitude=43.1" in url
    assert "longitude=-70.9" in url
    assert "current=" in url
    for field in WEATHER_FIELDS:
        assert field in url


def test_build_url_carries_all_air_quality_fields():
    url = _build_url("https://example.test/aq", 43.1, -70.9, AIR_QUALITY_FIELDS)
    for field in AIR_QUALITY_FIELDS:
        assert field in url


def test_build_url_requests_source_units_and_utc():
    """`/api/outdoor-latest`'s unit labels are assertions about *this* request.

    `web.OUTDOOR_LATEST_FIELDS` publishes `C` / `hPa` / `km/h` / `mm` as fixed
    strings. Those are right only because this function sends no unit override,
    so Open-Meteo answers in its defaults -- the two files agree by coincidence
    and nothing connected them. Adding `"wind_speed_unit": "mph"` here is a
    one-line, obviously-reasonable change that would make the endpoint lie to
    the hub about a number the weather card exists to show, with every other
    test still green.

    `timezone=UTC` is pinned for the reason `latest_outdoor_reading`'s docstring
    gives: `_normalize_source_time` stamps `tzinfo` on a *naive* value but stores
    a real offset verbatim, so a non-UTC answer would sort wrongly on `ts`. This
    parameter is the only thing preventing that, and it had no test.
    """
    url = _build_url("https://example.test/x", 43.1, -70.9, WEATHER_FIELDS)
    assert "timezone=UTC" in url
    assert "_unit=" not in url
    for override in ("temperature_unit", "wind_speed_unit", "precipitation_unit"):
        assert override not in url


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_make_fetch_returns_decoded_body(monkeypatch):
    """make_fetch's closure hits urlopen with the configured timeout and decodes."""
    calls = {}

    def fake_urlopen(url, timeout):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResponse('{"ok": true}')

    monkeypatch.setattr(outdoor.urllib.request, "urlopen", fake_urlopen)
    fetch = make_fetch("https://example.test/x?foo=1")
    assert fetch() == '{"ok": true}'
    assert calls["url"] == "https://example.test/x?foo=1"
    assert calls["timeout"] == outdoor.FETCH_TIMEOUT_SECONDS


def test_poll_once_weather_missing_current_returns_error(conn):
    """parse_reading raises KeyError on `payload['current']`; poll_once swallows it."""
    status = poll_once(
        conn,
        fetch_weather=lambda: json.dumps({}),  # no 'current' key
        fetch_air_quality=lambda: AIR_QUALITY_TEXT,
    )
    assert status == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_require_env_raises_when_missing(monkeypatch):
    monkeypatch.delenv("AWAIR_LAT", raising=False)
    with pytest.raises(SystemExit) as exc:
        _require_env("AWAIR_LAT")
    assert "AWAIR_LAT" in str(exc.value)


def test_require_env_returns_value(monkeypatch):
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    assert _require_env("AWAIR_LAT") == "43.1"


def test_main_polls_once_and_exits_when_sleep_raises(monkeypatch, tmp_path):
    """Drive main() through one loop iteration by making time.sleep bail out."""
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "out.db"))
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "1")

    # Both fetchers succeed so poll_once returns 'inserted' (INFO branch).
    monkeypatch.setattr(
        outdoor,
        "make_fetch",
        lambda url: (
            (lambda: WEATHER_TEXT)
            if "air-quality" not in url
            else (lambda: AIR_QUALITY_TEXT)
        ),
    )

    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop

    monkeypatch.setattr(outdoor.time, "sleep", stop)
    with pytest.raises(Stop):
        outdoor.main()
    # One row landed — proves poll_once was invoked with a real connection.
    conn = outdoor.db.connect(str(tmp_path / "out.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1
    finally:
        conn.close()


# --- weather_code + aq_ts (#71) ---------------------------------------------
#
# Two columns that exist so the hub's weather card can say a word and can date
# its AQI. The aq_ts half is the non-obvious one: it was already being read and
# discarded, so a row stamped 14:15 could carry an AQI measured at 13:00 with
# nothing downstream able to tell.


def test_weather_code_is_requested_from_the_source():
    """It cannot be stored if it is never asked for.

    Pinned separately from `parse_reading` because the two failures are
    independent and only one of them is visible in a parsed fixture: a hand-
    written test payload carries `weather_code` whether or not the real URL
    asks Open-Meteo for it, so a mapping test alone would stay green against a
    poller that receives the field never.
    """
    assert "weather_code" in WEATHER_FIELDS
    url = _build_url("https://x/y", 1.0, 2.0, WEATHER_FIELDS)
    assert "weather_code" in url


def test_parse_reading_stores_the_weather_code():
    """Off the shared fixture, which now carries the field production sends."""
    assert parse_reading(WEATHER, AIR_QUALITY, RECEIVED)["weather_code"] == 3


def test_a_response_without_a_weather_code_degrades_to_null():
    """Upstream schema drift is a warning, not an outage -- the module rule."""
    current = {k: v for k, v in WEATHER["current"].items() if k != "weather_code"}
    reading = parse_reading({"current": current}, AIR_QUALITY, RECEIVED)
    assert reading["weather_code"] is None
    assert reading["temp"] == 22.4


def test_parse_reading_stores_the_air_qualitys_own_timestamp():
    """The whole point of #71's second column, asserted on the lag itself.

    The shared fixtures already disagree by design -- weather publishes 04:30,
    air quality 04:00 -- so this asserts the two clocks land in different
    columns rather than one overwriting the other.
    """
    reading = parse_reading(WEATHER, AIR_QUALITY, RECEIVED)
    assert reading["aq_ts"] == "2026-07-12T04:00:00+00:00"
    assert reading["ts"] == "2026-07-12T04:30:00+00:00"
    assert reading["aq_ts"] < reading["ts"]


def test_aq_ts_is_normalised_to_the_same_spelling_as_ts():
    """They are meant to be subtracted, so they must be the same kind of string.

    Open-Meteo publishes `"YYYY-MM-DDTHH:MM"` -- naive, minute precision. Two
    fields stored in two spellings compare wrongly and sort wrongly, which is
    the same hazard `_normalize_source_time`'s docstring describes for `ts`.
    """
    reading = parse_reading(WEATHER, AIR_QUALITY, RECEIVED)
    assert reading["aq_ts"].endswith("+00:00")
    assert reading["aq_ts"][:19] == "2026-07-12T04:00:00"


def test_aq_ts_is_null_when_the_air_quality_fetch_failed():
    """`poll_once` passes None for the AQ payload on a partial poll.

    NULL here is the signal the hub acts on ("no current AQI" -> yellow), so it
    has to survive the partial path rather than being backfilled from `ts`.
    """
    reading = parse_reading(WEATHER, None, RECEIVED)
    assert reading["aq_ts"] is None
    assert reading["us_aqi"] is None
    assert reading["temp"] == 22.4  # weather half still written


@pytest.mark.parametrize(
    "bad",
    [
        "",
        None,
        "not-a-timestamp",
        "2026-13-45T99:99",
        # Non-strings, which is the half `except (TypeError, ValueError)` exists
        # for. Without one here, narrowing that catch to `ValueError` alone leaves
        # the whole suite green -- measured, it is a live mutation survivor. An
        # epoch int is the most plausible real drift; the list is the shape a
        # JSON object would take.
        1752292800,
        [],
    ],
)
def test_a_bad_air_quality_time_degrades_to_null_rather_than_losing_the_row(bad):
    """Auxiliary, so it must not take the weather half down with it.

    The weather block's own `time` is the primary key and keeps raising -- that
    asymmetry is the reason `_normalize_aq_time` exists as a separate function
    instead of `parse_reading` calling `_normalize_source_time` twice.
    """
    payload = {"current": dict(AIR_QUALITY["current"], time=bad)}
    reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] is None
    assert reading["us_aqi"] == 32  # the AQ values themselves still landed


def test_an_absent_aq_time_is_silent_but_a_malformed_one_warns(caplog):
    """The falsy guard in `_normalize_aq_time` earns its place on the log, not the value.

    Both paths return None, so no assertion about `aq_ts` can tell them apart --
    deleting the guard leaves the whole suite green (measured). What differs is
    whether we shout: an absent AQ `time` is the ordinary shape of a partial
    poll and would otherwise warn every 15 minutes during a CAMS outage, drowning
    the malformed case that actually means upstream drift.
    """
    absent = {
        "current": {k: v for k, v in AIR_QUALITY["current"].items() if k != "time"}
    }
    with caplog.at_level(logging.WARNING, logger="awair.outdoor"):
        assert parse_reading(WEATHER, absent, RECEIVED)["aq_ts"] is None
    assert caplog.records == []

    malformed = {"current": dict(AIR_QUALITY["current"], time="not-a-timestamp")}
    with caplog.at_level(logging.WARNING, logger="awair.outdoor"):
        assert parse_reading(WEATHER, malformed, RECEIVED)["aq_ts"] is None
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_a_missing_air_quality_time_key_degrades_to_null():
    """Distinct from the malformed case above: the key is absent, not bad."""
    current = {k: v for k, v in AIR_QUALITY["current"].items() if k != "time"}
    reading = parse_reading(WEATHER, {"current": current}, RECEIVED)
    assert reading["aq_ts"] is None
    assert reading["us_aqi"] == 32


def test_a_bad_weather_time_still_raises():
    """The asymmetry the two normalisers exist to express, pinned.

    If this ever degrades to NULL too, `insert_outdoor_reading` starts writing
    rows with a NULL primary key and the dedup that makes the poll loop
    idempotent stops working.
    """
    payload = {"current": dict(WEATHER["current"], time="not-a-timestamp")}
    with pytest.raises(ValueError):
        parse_reading(payload, AIR_QUALITY, RECEIVED)
