"""Open-Meteo outdoor poller: parse, dedup, error and partial paths."""

import http.client
import json
import logging
import os
import signal
import sqlite3
import time
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

    `timezone=UTC` is pinned for the reason `_normalize_source_time`'s docstring
    gives: it stamps `tzinfo` on a *naive* value but stores a real offset
    verbatim, so a non-UTC answer would sort wrongly on `ts`. This parameter is
    the only thing preventing that, and it had no test. (That paragraph lived on
    `db.latest_outdoor_reading` until #77; this citation moved with it.)
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


def test_main_polls_once_then_exits_cleanly_on_sigterm(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """A SIGTERM finishes the poll in flight and returns — it does not raise (#83).

    Outdoor logged four of these false failures in a week against the indoor
    poller's three, and for the same reason: no handler, so systemd's stop
    signal killed it mid-loop. The previous version of this test broke the loop
    by making `time.sleep` raise, which is that exact non-zero exit.
    """
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "out.db"))
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "900")

    # Both fetchers succeed so poll_once returns 'inserted' (INFO branch); the
    # weather one also asks to stop, standing in for systemd mid-poll.
    def weather_then_sigterm():
        os.kill(os.getpid(), signal.SIGTERM)
        return WEATHER_TEXT

    monkeypatch.setattr(
        outdoor,
        "make_fetch",
        lambda url: (
            weather_then_sigterm
            if "air-quality" not in url
            else (lambda: AIR_QUALITY_TEXT)
        ),
    )

    started = time.monotonic()
    outdoor.main()  # returns normally; must not raise
    # The interval is 900 s. Returning promptly proves the wait was interrupted.
    assert time.monotonic() - started < 10

    # One row landed — proves poll_once was invoked with a real connection and
    # the signal did not abandon the poll half-done.
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

    Membership in `WEATHER_FIELDS` is the whole assertion (#77). The `in url`
    check this used to also make was redundant *by composition*:
    `test_build_url_encodes_params` asserts every member of `WEATHER_FIELDS`
    reaches the URL, so that test plus this one already covers it. Named here
    rather than left implicit, because the composition is what makes the
    deletion safe -- if that loop ever stops covering all of `WEATHER_FIELDS`,
    this coverage goes with it.
    """
    assert "weather_code" in WEATHER_FIELDS


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


@pytest.mark.parametrize(
    ("date_only", "midnight_it_would_have_invented"),
    [
        ("2026-07-12", "2026-07-12T00:00:00+00:00"),
        # ISO basic. `datetime.fromisoformat` has accepted this since 3.11.
        ("20260712", "2026-07-12T00:00:00+00:00"),
        # ISO *week* date, which does not even resolve to the date it looks
        # like -- 2026-W28-1 is the 6th, not the 28th of anything.
        ("2026-W28-1", "2026-07-06T00:00:00+00:00"),
    ],
)
def test_a_date_only_aq_time_degrades_to_null_rather_than_midnight(
    date_only, midnight_it_would_have_invented
):
    """The failure `aq_ts` exists to prevent, arriving through the parser (#77).

    These three parse cleanly -- that is the whole problem. `fromisoformat`
    defaults the clock to `00:00`, so the row would carry an observation time
    Open-Meteo never published, and the hub would render an AQI up to a day old
    as current. NULL is the honest answer and is already the signal the hub
    acts on.

    The second parameter is asserted, not decoration: it pins what the old
    behaviour *was*, so a regression that reinstates midnight fails here rather
    than merely failing an `is None`.
    """
    from datetime import UTC, datetime

    assert (
        datetime.fromisoformat(date_only).replace(tzinfo=UTC).isoformat()
        == midnight_it_would_have_invented
    ), "fixture no longer reproduces the fabricating shape it was written for"

    payload = {"current": dict(AIR_QUALITY["current"], time=date_only)}
    reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] is None
    assert reading["us_aqi"] == 32  # the AQ values themselves still landed


def test_an_explicit_midnight_aq_time_is_kept():
    """The reachability control on the test above, and the reason it is structural.

    Midnight is a real instant that Open-Meteo really publishes once a day. A
    guard written as "reject if the parsed value is 00:00" would pass every
    assertion in the parametrised test above while silently dropping one poll
    in ninety-six. The guard has to test the *string's shape*, not the parsed
    value, and this is what separates the two implementations.
    """
    payload = {"current": dict(AIR_QUALITY["current"], time="2026-07-12T00:00")}
    reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] == "2026-07-12T00:00:00+00:00"


def test_a_space_separated_basic_time_is_kept():
    """The false reject in the fix #77 proposed, pinned so it cannot come back.

    The ticket prescribed rejecting any string carrying neither `T` nor `:`.
    `"20260712 0400"` is a real 04:00 and carries neither, so that rule drops
    it. `date.fromisoformat` -- which is the stdlib's own definition of
    "date-only" -- keeps it. This test is the difference between the two rules
    and would fail against the prescribed one.
    """
    payload = {"current": dict(AIR_QUALITY["current"], time="20260712 0400")}
    reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] == "2026-07-12T04:00:00+00:00"


def test_a_date_only_aq_time_warns_rather_than_passing_silently(caplog):
    """Upstream drift worth seeing, so it must not share the absent case's silence.

    `_normalize_aq_time` already distinguishes an *absent* AQ time (ordinary,
    silent) from a *malformed* one (drift, warned). A date-only value is the
    third case and belongs with the second: it means Open-Meteo changed the
    shape of `current.time`, which nothing else would report.
    """
    payload = {"current": dict(AIR_QUALITY["current"], time="2026-07-12")}
    with caplog.at_level(logging.WARNING, logger="awair.outdoor"):
        parse_reading(WEATHER, payload, RECEIVED)
    assert any("no time of day" in r.message for r in caplog.records)


def test_a_non_string_aq_time_is_reported_as_unparseable_not_as_date_only(caplog):
    """`date.fromisoformat(1752292800)` raises TypeError, and the two paths differ.

    An epoch int is the most plausible real drift. It must land on the
    `unparseable` branch -- if `_is_date_only` swallowed TypeError as "yes,
    date-only", every non-string would be mislabelled and the existing
    TypeError coverage would go quiet while still returning None.
    """
    payload = {"current": dict(AIR_QUALITY["current"], time=1752292800)}
    with caplog.at_level(logging.WARNING, logger="awair.outdoor"):
        reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] is None
    messages = [r.message for r in caplog.records]
    assert any("unparseable" in m for m in messages)
    assert not any("no time of day" in m for m in messages)


@pytest.mark.parametrize(
    ("clockless", "midnight_it_used_to_invent"),
    [
        ("2026-07-12", "2026-07-12T00:00:00+00:00"),
        ("20260712", "2026-07-12T00:00:00+00:00"),
        ("2026-W28-1", "2026-07-06T00:00:00+00:00"),
        # Zone-only: `datetime.fromisoformat` takes any single character as the
        # date/time separator, so these fabricate an *hour*, not a midnight.
        ("2026-07-12+05:00", "2026-07-12T05:00:00+00:00"),
        ("2026-07-12-05:00", "2026-07-12T05:00:00+00:00"),
        ("20260712+0500", "2026-07-12T05:00:00+00:00"),
    ],
)
def test_a_date_only_weather_time_raises_rather_than_inventing_a_clock(
    clockless, midnight_it_used_to_invent
):
    """`ts` gets #77's guard too, now that `poll_once` survives the raise (#91).

    This replaces `test_a_date_only_weather_time_is_deliberately_unchanged`,
    which pinned the *old* behaviour and said explicitly why: tightening the
    weather clock while `poll_once` caught `KeyError` alone would have converted
    "stores a wrong midnight" into "the poller process exits". `poll_once` now
    catches the raise and returns `"error"`, so the constraint is gone and the
    guard moves into `_normalize_source_time`, where it covers both clocks.

    The second parameter is asserted, not decoration: it pins the value the old
    code stored, so a regression that reinstates the fabrication fails here on
    the fabricated instant rather than merely on a missing raise.
    """
    from datetime import UTC, datetime

    assert (
        datetime.fromisoformat(clockless).replace(tzinfo=UTC).isoformat()
        == midnight_it_used_to_invent
    ), "fixture no longer reproduces the fabricating shape it was written for"

    payload = {"current": dict(WEATHER["current"], time=clockless)}
    with pytest.raises(ValueError):
        parse_reading(payload, AIR_QUALITY, RECEIVED)


def test_a_date_only_weather_time_says_so_rather_than_just_failing_to_parse():
    """The two rejections are indistinguishable to a reader without this.

    `"not-a-timestamp"` and `"2026-07-12"` both raise `ValueError` now, but only
    one of them is upstream publishing a *shape* we refuse on purpose. The
    message is what tells whoever reads the log at 15-minute intervals which
    one they have.
    """
    payload = {"current": dict(WEATHER["current"], time="2026-07-12")}
    with pytest.raises(ValueError) as raised:
        parse_reading(payload, AIR_QUALITY, RECEIVED)
    assert "no time of day" in str(raised.value)
    assert "2026-07-12" in str(raised.value)


# --- poll_once survives a bad weather clock (#91) ----------------------------
#
# `parse_reading` raising is the correct contract and was already tested. What
# was untested -- and is the whole of #91 -- is that `poll_once` *catches* it.
# It caught `KeyError` alone, so an unparseable `current.time` unwound the
# `while` loop in `main()` and the process exited; systemd restarted it and it
# died again for as long as the source kept publishing the bad value.


@pytest.mark.parametrize(
    ("bad_time", "escaping_exception"),
    [
        # ValueError out of `datetime.fromisoformat`.
        ("not-a-timestamp", "ValueError"),
        ("", "ValueError"),
        # Date-only, newly rejected by `_normalize_source_time` above.
        ("2026-07-12", "ValueError"),
        ("2026-07-12+05:00", "ValueError"),
        # TypeError, NOT ValueError: a JSON `null` reaches `fromisoformat` as
        # None. The ticket prescribed `except (KeyError, ValueError)`, which
        # leaves this one killing the poller exactly as before.
        (None, "TypeError"),
        (1752300000, "TypeError"),
    ],
)
def test_poll_once_survives_a_bad_weather_time(conn, bad_time, escaping_exception):
    payload = {"current": dict(WEATHER["current"], time=bad_time)}

    with pytest.raises((ValueError, TypeError)) as raised:
        parse_reading(payload, AIR_QUALITY, RECEIVED)
    assert type(raised.value).__name__ == escaping_exception, (
        "fixture no longer reproduces the exception class it was written for"
    )

    assert poll_once(conn, lambda: json.dumps(payload), lambda: AIR_QUALITY_TEXT) == (
        "error"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_poll_once_survives_a_non_dict_current_block(conn):
    """A separate TypeError path: `current` is a list, so `["time"]` raises.

    Not reachable through the `time` parametrize above, and not a `KeyError`, so
    the pre-#91 catch missed it too.
    """
    payload = {"current": []}
    with pytest.raises(TypeError):
        parse_reading(payload, AIR_QUALITY, RECEIVED)
    assert poll_once(conn, lambda: json.dumps(payload), lambda: AIR_QUALITY_TEXT) == (
        "error"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_a_date_only_weather_time_no_longer_locks_out_the_rest_of_the_day(conn):
    """The measured half of #91: one row per *day*, silently, at INFO.

    A date-only time made `ts` midnight, and `ts` is the PRIMARY KEY under
    `INSERT OR IGNORE`, so the first poll of the day inserted the fabricated
    midnight row and every later poll that day returned `"duplicate"` and wrote
    nothing -- logged at INFO, which is the level a healthy dedup uses.

    Now every affected poll is an `"error"` (logged at WARNING by `main`), no
    fabricated row is stored, and the moment upstream publishes a real clock the
    reading lands.
    """
    date_only = json.dumps({"current": dict(WEATHER["current"], time="2026-07-12")})
    statuses = [
        poll_once(conn, lambda: date_only, lambda: AIR_QUALITY_TEXT) for _ in range(3)
    ]
    assert statuses == ["error", "error", "error"]
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0

    # Upstream recovers mid-day: the real reading is not shut out by a
    # fabricated midnight row already holding the primary key.
    assert poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT) == "inserted"
    assert conn.execute("SELECT ts FROM outdoor_readings").fetchone()[0] == (
        "2026-07-12T04:30:00+00:00"
    )


def test_main_keeps_polling_through_a_bad_weather_time(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """The process-level claim in #91, pinned where it actually bit.

    Every assertion above is on `poll_once`'s return value, and a `poll_once`
    that returns `"error"` is only useful if the loop that calls it is still
    running. Before the fix this test raised `ValueError` out of `main()` --
    which is the non-zero exit systemd sees, and then restarts into.

    The first poll publishes an unparseable clock; the second publishes a good
    one and asks to stop. A row from the *second* poll is the proof that the
    loop survived the first.
    """
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "out.db"))
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "0")

    bad = json.dumps({"current": dict(WEATHER["current"], time="not-a-timestamp")})
    polls = iter([bad, WEATHER_TEXT])

    def weather():
        payload = next(polls)
        if payload is WEATHER_TEXT:
            os.kill(os.getpid(), signal.SIGTERM)
        return payload

    monkeypatch.setattr(
        outdoor,
        "make_fetch",
        lambda url: weather if "air-quality" not in url else (lambda: AIR_QUALITY_TEXT),
    )

    outdoor.main()  # must not raise -- that is the whole bug

    assert next(polls, None) is None, "the second poll never ran"
    conn = outdoor.db.connect(str(tmp_path / "out.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("zone_only", "hour_it_would_have_invented"),
    [
        ("2026-07-12+05:00", "2026-07-12T05:00:00+00:00"),
        ("2026-07-12-05:00", "2026-07-12T05:00:00+00:00"),
        ("20260712+0500", "2026-07-12T05:00:00+00:00"),
    ],
)
def test_a_date_with_only_a_zone_designator_degrades_to_null(
    zone_only, hour_it_would_have_invented
):
    """The hole the first #77 fix left, found in review rather than by measurement.

    `datetime.fromisoformat` takes *any single character* as the date/time
    separator, so it reads `"2026-07-12+05:00"` as the 12th at 05:00 -- while
    `date.fromisoformat` rejects that string outright. A guard built on
    `date.fromisoformat` alone therefore waves it through, and the row stores an
    observation time an hour further from the truth than the midnight case, with
    no warning at all. Stripping the zone designator first is what closes it.

    The second parameter asserts the fabricated value, so a regression that
    reinstates it fails here on the hour rather than merely on an `is None`.
    """
    from datetime import UTC, datetime

    assert (
        datetime.fromisoformat(zone_only).replace(tzinfo=UTC).isoformat()
        == hour_it_would_have_invented
    ), "fixture no longer reproduces the fabricating shape it was written for"

    payload = {"current": dict(AIR_QUALITY["current"], time=zone_only)}
    reading = parse_reading(WEATHER, payload, RECEIVED)
    assert reading["aq_ts"] is None
    assert reading["us_aqi"] == 32


def test_the_zone_strip_does_not_eat_a_real_offset_or_a_week_date():
    """The two things `_ZONE_SUFFIX` must not match, pinned as behaviour.

    A pattern loose enough to strip `"+05:00"` is one edit away from eating the
    `-1` of the ISO week date `"2026-W28-1"` (which would then read as the
    date-only `"2026-W28"` -- still rejected, so that failure is invisible here)
    or the `0400` of `"20260712 0400"` (which would read as the date-only
    `"20260712"` and *silently drop a real reading*). The second is the
    dangerous one, so both directions are asserted.
    """
    kept = {"current": dict(AIR_QUALITY["current"], time="2026-07-12T04:00+05:00")}
    assert (
        parse_reading(WEATHER, kept, RECEIVED)["aq_ts"] == "2026-07-12T04:00:00+05:00"
    )

    basic = {"current": dict(AIR_QUALITY["current"], time="20260712 0400")}
    assert (
        parse_reading(WEATHER, basic, RECEIVED)["aq_ts"] == "2026-07-12T04:00:00+00:00"
    )

    week = {"current": dict(AIR_QUALITY["current"], time="2026-W28-1T04:00")}
    assert (
        parse_reading(WEATHER, week, RECEIVED)["aq_ts"] == "2026-07-06T04:00:00+00:00"
    )


# --- the same outage class on the air-quality side (#91 review) --------------
#
# `#91` was written about the weather clock and its fix covered `KeyError`,
# `TypeError` and `ValueError`. The AQ block reaches its fields through
# `.get`, so an unreadable *shape* there raises `AttributeError` -- not in that
# tuple, and so still a process death. Symmetric to the non-dict `current`
# case above, which is the weather side of the identical mistake.


@pytest.mark.parametrize(
    ("aq_body", "shape"),
    [
        ("[]", "payload is a list"),
        ('"nope"', "payload is a string"),
        ("7", "payload is a number"),
        ('{"current": []}', "current is a list"),
        ('{"current": "nope"}', "current is a string"),
    ],
)
def test_an_unreadable_air_quality_block_is_partial_not_a_crash(conn, aq_body, shape):
    """The weather row survives and the poll says so, rather than the process dying.

    `"partial"` rather than `"error"` because the remedy is the one the status
    already documents: an AQ problem must not wedge the weather write. All five
    of these raised `AttributeError` out of `poll_once` before the review fix.
    """
    assert poll_once(conn, lambda: WEATHER_TEXT, lambda: aq_body) == "partial", shape
    row = conn.execute("SELECT temp, us_aqi, aq_ts FROM outdoor_readings").fetchone()
    assert row == (22.4, None, None)


def test_an_unreadable_air_quality_block_does_not_crash_parse_reading_either(conn):
    """`parse_reading` has direct callers, so the guard cannot live only in `poll_once`.

    Its documented policy is that a missing AQ field degrades to NULL; a
    `current` block of the wrong *type* is the same situation arriving through
    the shape rather than through a key, and used to raise instead.
    """
    reading = parse_reading(WEATHER, {"current": []}, RECEIVED)
    assert reading["us_aqi"] is None
    assert reading["aq_ts"] is None
    assert reading["temp"] == 22.4


@pytest.mark.parametrize("failing_side", ["weather", "air-quality"])
def test_a_fetcher_returning_none_does_not_kill_the_poller(conn, failing_side):
    """`json.loads(None)` is a TypeError, and neither `except` used to catch it.

    Not a hypothetical fetcher: it is what any `make_fetch` replacement that
    forgets a `return` produces, and both call sites were one line from the
    clock defect #91 is about.
    """
    none_fetch = lambda: None  # noqa: E731 -- the shape under test is its return
    if failing_side == "weather":
        assert poll_once(conn, none_fetch, lambda: AIR_QUALITY_TEXT) == "error"
        assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0
    else:
        assert poll_once(conn, lambda: WEATHER_TEXT, none_fetch) == "partial"
        assert conn.execute("SELECT us_aqi FROM outdoor_readings").fetchone()[0] is None


def test_an_explicitly_published_midnight_weather_time_is_kept(conn):
    """The reachability control for the `ts` guard (#91 review).

    Every other assertion on the new guard is that it *rejects*. If the guard
    ever became value-based -- `parsed.hour == 0 and parsed.minute == 0` rather
    than "the string carries no time of day" -- it would start refusing this,
    a real reading Open-Meteo publishes once a day, and the rejection tests
    would all still pass. The AQ side has the same control at
    `test_an_explicit_midnight_aq_time_is_kept`; this makes the `ts` path's own
    local rather than inherited through the shared normaliser.
    """
    payload = {"current": dict(WEATHER["current"], time="2026-07-12T00:00")}
    assert parse_reading(payload, AIR_QUALITY, RECEIVED)["ts"] == (
        "2026-07-12T00:00:00+00:00"
    )
    assert poll_once(conn, lambda: json.dumps(payload), lambda: AIR_QUALITY_TEXT) == (
        "inserted"
    )


def test_main_logs_an_unusable_payload_at_warning_not_info(
    monkeypatch, tmp_path, restore_signal_handlers, caplog
):
    """After #91 the log level is the *only* remaining signal, so it is pinned.

    Before this change a bad weather clock announced itself two ways: a
    crash-loop in the journal, or a fabricated midnight row in the database.
    Both are now gone by design -- nothing is written and the process survives
    -- which leaves `main`'s WARNING as the whole of what a human can notice.
    `GLOSSARY.md`'s **outdoor poll** entry asserts that level, and two test
    docstrings lean on it.

    Measured in review: deleting `poll_once`'s `log.warning` and adding
    `"error"` to `main`'s INFO branch each left all 354 tests green.
    """
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "out.db"))
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "0")

    bad = json.dumps({"current": dict(WEATHER["current"], time="not-a-timestamp")})

    def weather():
        os.kill(os.getpid(), signal.SIGTERM)
        return bad

    monkeypatch.setattr(
        outdoor,
        "make_fetch",
        lambda url: weather if "air-quality" not in url else (lambda: AIR_QUALITY_TEXT),
    )

    with caplog.at_level(logging.INFO, logger="awair.outdoor"):
        outdoor.main()

    # The trailing colon matters -- `main`'s own "outdoor poller stopped
    # cleanly" line starts with "outdoor poll" too, and matching it here made
    # the first draft of this assertion fail for the wrong reason.
    poll_lines = [
        r for r in caplog.records if r.getMessage().startswith("outdoor poll:")
    ]
    assert [(r.levelno, r.getMessage()) for r in poll_lines] == [
        (logging.WARNING, "outdoor poll: error")
    ]
    # And the reason, not just the verdict -- "error" alone does not say which
    # of the unusable-payload shapes arrived. The handler covers `sqlite3.Error`
    # since #98, so the message is no longer allowed to blame the payload
    # unconditionally; what it must still carry is the exception class, which is
    # what tells a reader whether to look at Open-Meteo or at their own disk.
    assert any(
        r.levelno == logging.WARNING
        and "payload or insert failed" in r.getMessage()
        and "ValueError" in r.getMessage()
        for r in caplog.records
    )


# --- the insert inside the guard, and the fetch gap (#98) -------------------


def test_poll_once_survives_an_unbindable_sensor_value(conn):
    """The shape that made the insert's position matter (#98).

    `parse_reading` validates `current.time` and hands the other twelve columns
    to the driver unchecked, so a nested object in any of them is a bind-time
    `sqlite3.ProgrammingError`. With the insert outside the guard that escaped
    `poll_once` and unwound `main()`, and systemd restarted the poller straight
    back into the same upstream value -- the crash loop #91 exists to prevent,
    arriving one layer lower down.
    """
    payload = {"current": dict(WEATHER["current"], temperature_2m={"nested": 1})}

    # Control: the shape really does reach the driver as an error.
    with pytest.raises(sqlite3.ProgrammingError):
        from awair import db as _db

        _db.insert_outdoor_reading(conn, parse_reading(payload, AIR_QUALITY, RECEIVED))

    assert poll_once(conn, lambda: json.dumps(payload), lambda: AIR_QUALITY_TEXT) == (
        "error"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0
    # And the write lock is not left held -- both pollers share one AWAIR_DB.
    assert conn.in_transaction is False


def test_poll_once_survives_a_truncated_weather_response(conn):
    """`IncompleteRead` is not an `OSError` and urllib does not convert it.

    So before #98 a truncated response from Open-Meteo -- a real thing for an
    HTTP fetch over a home connection -- escaped the fetch guard entirely.
    """
    assert not issubclass(http.client.IncompleteRead, OSError), (
        "if this ever becomes an OSError the test no longer covers what it says"
    )

    def truncated():
        raise http.client.IncompleteRead(b'{"cur')

    assert poll_once(conn, truncated, lambda: AIR_QUALITY_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0


def test_poll_once_downgrades_a_truncated_air_quality_response_to_partial(conn):
    """The AQ half of the same gap: the weather row is still worth writing."""

    def truncated():
        raise http.client.BadStatusLine("garbage")

    assert poll_once(conn, lambda: WEATHER_TEXT, truncated) == "partial"
    stored = conn.execute("SELECT temp, us_aqi FROM outdoor_readings").fetchone()
    assert stored == (22.4, None)


def test_a_disk_fault_costs_a_poll_rather_than_the_process(conn, monkeypatch):
    """The half of the divergence from #91 that is NOT about payloads.

    #91 ruled a `sqlite3.Error` should propagate here because it is "a local
    fault a restart can clear". Exiting does not clear a full disk, and
    `OutdoorHealth` escalates a sustained run of errors to "unreachable" (#94),
    so the fault is still reported -- just not by dying.
    """
    from awair import db as _db

    def full_disk(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(_db, "insert_outdoor_reading", full_disk)
    assert poll_once(conn, lambda: WEATHER_TEXT, lambda: AIR_QUALITY_TEXT) == "error"


@pytest.mark.parametrize("bad_time", [None, "", 0, 123, [], {}])
def test_parse_reading_refuses_a_ts_that_is_not_a_usable_string(bad_time):
    """#98 item 4, which the outdoor poller already satisfied -- now pinned.

    The ticket prescribed copying PR #97's explicit `not timestamp or not
    isinstance(timestamp, str)` check across from the indoor poller. It is a
    no-op here: indoor's `parse_reading` stores `payload["timestamp"]` verbatim,
    whereas this one runs every value through `_normalize_source_time`, which
    raises `TypeError` on a non-string and `ValueError` on an unparseable one.
    Both are in `POLL_FAILURES`, so the poll is already a logged `"error"`.

    Nothing was added for this. The test exists because the behaviour was
    incidental -- a future refactor that normalized lazily, or accepted an
    integer epoch, would reopen the hole with every other test still green.
    """
    with pytest.raises((TypeError, ValueError)):
        parse_reading({"current": {"time": bad_time}}, {}, received_at=RECEIVED)


def test_a_null_ts_can_no_longer_reach_the_table_as_a_silent_duplicate(conn):
    """End-to-end for the regression the NOT NULL migration could have caused.

    Under `INSERT OR IGNORE` a null `ts` against a NOT NULL column comes back
    rowcount 0, which `poll_once` renders as `"duplicate"` -- the one status
    meaning nothing is wrong, and exactly #95's silent half. It must be an
    `"error"`, and the row must not be there.
    """
    payload = {"current": dict(WEATHER["current"], time=None)}
    assert poll_once(conn, lambda: json.dumps(payload), lambda: AIR_QUALITY_TEXT) == (
        "error"
    )
    assert conn.execute("SELECT COUNT(*) FROM outdoor_readings").fetchone()[0] == 0
