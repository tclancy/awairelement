"""The basement feed: parsing, the no-reading refusals, and the per-minute key.

The one rule everything here serves: **the sensor's payload carries no clock**,
so a row's identity is our poll clock truncated to the minute. The module
publishes every 60 s and we poll every 60 s plus fetch time, so a raw poll
stamp would be unique on every poll and the dedup could never fire.
"""

import json
import os
import signal
import time
from datetime import UTC, datetime

import pytest

from awair import basement, db

# What `GET /sensor/Temperature` returned on 2026-10-28.
PAYLOAD = {"id": "sensor/Temperature", "value": 15.1268, "state": "15.13 °C"}

POLLED_AT = datetime(2026, 10, 30, 12, 34, 56, 789000, tzinfo=UTC)


def test_a_reading_is_keyed_on_the_poll_minute_and_keeps_the_exact_poll_time():
    row = basement.parse_reading(PAYLOAD, POLLED_AT)
    assert row == {
        "ts": "2026-10-30T12:34:00+00:00",
        "received_at": "2026-10-30T12:34:56.789000+00:00",
        "temp": 15.1268,
        # Always None out of `parse_reading`: the humidity is a second GET that
        # `poll_once` makes only once the temperature has parsed (#156).
        "humid": None,
    }


def test_a_poll_clock_in_another_zone_is_keyed_in_utc():
    from datetime import timedelta, timezone

    eastern = POLLED_AT.astimezone(timezone(timedelta(hours=-4)))
    assert basement.parse_reading(PAYLOAD, eastern)["ts"] == (
        "2026-10-30T12:34:00+00:00"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "sensor/Temperature", "value": None, "state": "NA"},
        {"id": "sensor/Temperature", "state": "NA"},
        {"id": "sensor/Temperature", "value": float("nan"), "state": "NA"},
        {"id": "sensor/Temperature", "value": float("nan"), "state": "nan °C"},
        {"id": "sensor/Temperature", "value": float("inf"), "state": "inf °C"},
        {"id": "sensor/Temperature", "value": "15.1", "state": "15.10 °C"},
        {"id": "sensor/Temperature", "value": True, "state": "NA"},
    ],
    ids=["null", "missing", "nan-NA", "nan-unit", "inf", "string", "bool"],
)
def test_an_unavailable_sensor_is_no_reading_rather_than_a_row(payload):
    """Checked before the unit, so an `"NA"` state never reads as a unit error."""
    assert basement.parse_reading(payload, POLLED_AT) is None


@pytest.mark.parametrize("state", ["59.23 °F", "15.13", None, 15.13])
def test_a_finite_value_in_another_unit_raises_rather_than_storing(state):
    payload = {**PAYLOAD, "state": state}
    with pytest.raises(ValueError):
        basement.parse_reading(payload, POLLED_AT)


@pytest.mark.parametrize("payload", [None, [], "15.13 °C", 15.13])
def test_a_body_that_is_not_an_object_raises(payload):
    with pytest.raises(TypeError):
        basement.parse_reading(payload, POLLED_AT)


def test_an_integer_value_is_stored_as_a_float():
    row = basement.parse_reading({**PAYLOAD, "value": 15}, POLLED_AT)
    assert row["temp"] == 15.0 and isinstance(row["temp"], float)


def fixed_clock(*moments):
    stamps = iter(moments)
    return lambda: next(stamps)


def test_a_second_poll_in_the_same_minute_is_a_duplicate(conn):
    fetch = lambda: json.dumps(PAYLOAD)  # noqa: E731
    clock = fixed_clock(POLLED_AT, POLLED_AT.replace(second=59))
    assert basement.poll_once(conn, fetch, clock) == "inserted"
    assert basement.poll_once(conn, fetch, clock) == "duplicate"
    assert conn.execute("SELECT count(*) FROM basement_readings").fetchone() == (1,)


def test_the_next_minute_inserts_beside_the_first(conn):
    fetch = lambda: json.dumps(PAYLOAD)  # noqa: E731
    clock = fixed_clock(POLLED_AT, POLLED_AT.replace(minute=35, second=1))
    assert basement.poll_once(conn, fetch, clock) == "inserted"
    assert basement.poll_once(conn, fetch, clock) == "inserted"
    assert conn.execute("SELECT count(*) FROM basement_readings").fetchone() == (2,)


def test_an_unavailable_sensor_reports_no_reading_and_stores_nothing(conn):
    fetch = lambda: json.dumps({"id": "sensor/Temperature", "state": "NA"})  # noqa: E731
    assert basement.poll_once(conn, fetch, fixed_clock(POLLED_AT)) == "no_reading"
    assert conn.execute("SELECT count(*) FROM basement_readings").fetchone() == (0,)


def raises(exc):
    def fetch():
        raise exc

    return fetch


@pytest.mark.parametrize(
    "fetch",
    [
        raises(OSError("unreachable")),
        raises(TimeoutError("timed out")),
        lambda: "<html>not json</html>",
        lambda: None,
        lambda: json.dumps({**PAYLOAD, "state": "59.23 °F"}),
        lambda: json.dumps([1, 2]),
    ],
    ids=["oserror", "timeout", "not-json", "none", "wrong-unit", "array"],
)
def test_one_bad_poll_costs_one_poll_and_never_the_service(conn, fetch):
    assert basement.poll_once(conn, fetch, fixed_clock(POLLED_AT)) == "error"


def test_an_insert_failure_is_reported_rather_than_unwinding_the_loop(
    conn, monkeypatch
):
    import sqlite3

    def broken_insert(conn, reading):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "insert_basement_reading", broken_insert)
    fetch = lambda: json.dumps(PAYLOAD)  # noqa: E731
    assert basement.poll_once(conn, fetch, fixed_clock(POLLED_AT)) == "error"


def test_the_default_url_is_the_sensors_reserved_address():
    assert basement.DEFAULT_SENSOR_URL == "http://192.168.68.50/sensor/Temperature"


def test_the_poll_cadence_matches_the_modules_publish_interval():
    assert basement.DEFAULT_POLL_SECONDS == 60


# --- the humidity entity (#156) ---

# What `GET /sensor/Humidity` is expected to return: the same shape as the
# temperature entity, in percent. The entity NAME is a derivation rather than
# an observation -- see `basement.humidity_url` -- which is why it is
# overridable by environment and why every failure it can have is survivable.
HUMIDITY_PAYLOAD = {"id": "sensor/Humidity", "value": 48.2412, "state": "48.2 %"}


def test_the_humidity_entity_is_derived_from_the_configured_temperature_url():
    """One device, one configured address; the second entity follows it."""
    assert basement.humidity_url("http://192.168.68.50/sensor/Temperature") == (
        "http://192.168.68.50/sensor/Humidity"
    )
    assert basement.humidity_url("http://sensor.test:8080/sensor/Temperature") == (
        "http://sensor.test:8080/sensor/Humidity"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.68.50/sensor/BasementTemperature",
        "http://192.168.68.50/sensor/temperature",
        "http://192.168.68.50/",
        "http://192.168.68.50",
    ],
    ids=["renamed", "case", "no-entity", "bare-host"],
)
def test_an_unrecognised_entity_url_gets_no_humidity_rather_than_a_guess(url):
    """The path is keyed on the entity, so appending to an unknown one guesses.

    A None here is the poller this repo shipped before #156: the temperature,
    stored, and no second GET.
    """
    assert basement.humidity_url(url) is None


def test_a_poll_stores_the_humidity_beside_the_temperature(conn):
    fetch = lambda: json.dumps(PAYLOAD)  # noqa: E731
    humidity = lambda: json.dumps(HUMIDITY_PAYLOAD)  # noqa: E731
    assert (
        basement.poll_once(conn, fetch, fixed_clock(POLLED_AT), fetch_humidity=humidity)
        == "inserted"
    )
    assert conn.execute("SELECT temp, humid FROM basement_readings").fetchall() == [
        (15.1268, 48.2412)
    ]


def test_an_integer_humidity_is_stored_as_a_float(conn):
    humidity = lambda: json.dumps({**HUMIDITY_PAYLOAD, "value": 48, "state": "48 %"})  # noqa: E731
    basement.poll_once(
        conn,
        lambda: json.dumps(PAYLOAD),
        fixed_clock(POLLED_AT),
        fetch_humidity=humidity,
    )
    stored = conn.execute("SELECT humid FROM basement_readings").fetchone()[0]
    assert stored == 48.0 and isinstance(stored, float)


@pytest.mark.parametrize(
    "humidity",
    [
        None,
        raises(OSError("404 Not Found")),
        raises(TimeoutError("timed out")),
        lambda: "<html>not json</html>",
        lambda: None,
        lambda: json.dumps({"id": "sensor/Humidity", "value": 0.482, "state": "0.48"}),
        lambda: json.dumps({**HUMIDITY_PAYLOAD, "state": "48.2 g/m³"}),
        lambda: json.dumps({"id": "sensor/Humidity", "state": "NA"}),
        lambda: json.dumps([48.2]),
    ],
    ids=[
        "not-configured",
        "renamed-entity-404",
        "timeout",
        "not-json",
        "none",
        "no-unit",
        "wrong-unit",
        "unavailable",
        "array",
    ],
)
def test_a_humidity_that_cannot_be_read_costs_the_humidity_and_nothing_else(
    conn, humidity
):
    """The temperature is the row's reason to exist, so it is never collateral.

    Every one of these is a real possibility for an entity whose name nobody
    has verified, and the status the poller reports is the temperature's:
    'inserted', with a NULL humidity, which is already this column's spelling
    for "not reported".
    """
    assert (
        basement.poll_once(
            conn, lambda: json.dumps(PAYLOAD), fixed_clock(POLLED_AT), humidity
        )
        == "inserted"
    )
    assert conn.execute("SELECT temp, humid FROM basement_readings").fetchall() == [
        (15.1268, None)
    ]


def test_the_humidity_is_not_fetched_for_a_row_that_will_not_be_written(conn):
    """An unavailable sensor costs one GET, not two."""
    calls = []

    def humidity():
        calls.append(1)
        return json.dumps(HUMIDITY_PAYLOAD)

    unavailable = lambda: json.dumps({"id": "sensor/Temperature", "state": "NA"})  # noqa: E731
    assert (
        basement.poll_once(conn, unavailable, fixed_clock(POLLED_AT), humidity)
        == "no_reading"
    )
    assert calls == []


def test_main_polls_once_then_exits_cleanly_on_sigterm(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """Also the only proof `main` reads the env names the README documents."""
    db_path = tmp_path / "basement.db"
    monkeypatch.setenv("AWAIR_DB", str(db_path))
    monkeypatch.setenv("AWAIR_BASEMENT_URL", "http://sensor.test/sensor/Temperature")
    monkeypatch.setenv("AWAIR_BASEMENT_POLL_SECONDS", "900")
    monkeypatch.delenv("AWAIR_BASEMENT_HUMIDITY_URL", raising=False)
    urls = []

    def fake_make_fetch(url):
        urls.append(url)
        body = HUMIDITY_PAYLOAD if url.endswith("Humidity") else PAYLOAD

        def fetch():
            # Only the temperature fetch ends the loop: the humidity one runs
            # inside the same iteration and a signal from it would prove
            # nothing about the poll completing.
            if body is PAYLOAD:
                os.kill(os.getpid(), signal.SIGTERM)
            return json.dumps(body)

        return fetch

    monkeypatch.setattr(basement, "make_fetch", fake_make_fetch)

    started = time.monotonic()
    basement.main()  # returns normally; must not raise
    assert time.monotonic() - started < 10
    # Both entities, and the humidity one derived from the configured
    # temperature URL rather than from `DEFAULT_SENSOR_URL`.
    assert urls == [
        "http://sensor.test/sensor/Temperature",
        "http://sensor.test/sensor/Humidity",
    ]

    conn = db.connect(db_path)
    try:
        stored = conn.execute("SELECT temp, humid FROM basement_readings").fetchall()
    finally:
        conn.close()
    assert stored == [(15.1268, 48.2412)]


def test_the_humidity_url_can_be_overridden_by_environment(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """The escape hatch for the one fact in this feed nobody has verified.

    If the ESPHome entity is not called `Humidity`, this is the fix -- a
    homelab env change rather than a release.
    """
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "basement.db"))
    monkeypatch.setenv("AWAIR_BASEMENT_URL", "http://sensor.test/sensor/Temperature")
    monkeypatch.setenv(
        "AWAIR_BASEMENT_HUMIDITY_URL", "http://sensor.test/sensor/Basement%20Humidity"
    )
    urls = []

    def fake_make_fetch(url):
        urls.append(url)

        def fetch():
            os.kill(os.getpid(), signal.SIGTERM)
            return json.dumps(PAYLOAD)

        return fetch

    monkeypatch.setattr(basement, "make_fetch", fake_make_fetch)
    basement.main()
    assert urls == [
        "http://sensor.test/sensor/Temperature",
        "http://sensor.test/sensor/Basement%20Humidity",
    ]
