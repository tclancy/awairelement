"""Poll the basement ESPHome sensor and store its temperature and humidity.

Run as: python -m awair.basement
Config via environment:
  AWAIR_BASEMENT_URL           — default DEFAULT_SENSOR_URL, the sensor's
                                 ESPHome web-server entity endpoint
  AWAIR_BASEMENT_HUMIDITY_URL  — default `humidity_url(AWAIR_BASEMENT_URL)`,
                                 the humidity entity beside it
  AWAIR_BASEMENT_POLL_SECONDS  — default 60 (the module's publish interval)
  AWAIR_DB                     — shared with the other pollers

The endpoint serves the entity's last *published* value as
`{"id": "sensor/Temperature", "value": 15.1268, "state": "15.13 °C"}` —
Celsius, and **no timestamp**. So, unlike every other feed here, a row is keyed
on our own poll clock, **truncated to the minute**: the module publishes every
60 s and we poll every 60 s plus fetch time, so a raw poll stamp would be unique
on every poll and the dedup could never fire. One row per poll-minute; a second
poll inside one minute writes nothing.

What this cannot detect: a stalled component that keeps serving a finite stale
value. The payload has no clock to compare, so gaps appear on fetch failure and
on an unavailable reading only.

The path is keyed on the ESPHome *entity* name (`Temperature`), not the device,
so renaming the device is safe and renaming the entity is a 404.

**The humidity is a second GET, and it is subordinate to the first** (#156).
The module is an AHT-family temperature/humidity part, so the reading is there,
but the entity's name is a guess derived from the temperature one and a wrong
guess is a 404 — so nothing about the humidity fetch may cost the temperature.
It is read only once the temperature has parsed, every failure it can have
answers None, and the row is written either way. One row still carries both:
the two entities are the same part read a moment apart, and splitting them into
two tables would key the same observation on two clocks.
"""

import http.client
import json
import logging
import math
import os
import sqlite3
import urllib.request
from datetime import UTC, datetime

from awair import db
from awair.shutdown import install_handler

log = logging.getLogger("awair.basement")

FETCH_TIMEOUT_SECONDS = 10

# ESPHome's default `update_interval`. Polling faster buys nothing: the
# endpoint republishes the same value until the module's next update.
DEFAULT_POLL_SECONDS = 60


def poll_seconds() -> int:
    """Seconds between basement polls, per `AWAIR_BASEMENT_POLL_SECONDS`.

    The one reader of that name in this repo, so `/health` can judge this
    feed's staleness against the same cadence `main()` polls at instead of
    restating it (#158). Read per call rather than bound at import, for the
    reason `poller.poll_seconds` explains.
    """
    return int(os.environ.get("AWAIR_BASEMENT_POLL_SECONDS", DEFAULT_POLL_SECONDS))


# Reserved in the Deco by MAC, so an IP literal is stable and needs no mDNS.
DEFAULT_SENSOR_URL = "http://192.168.68.50/sensor/Temperature"

# The two ESPHome entity names this module reads. Written down because the
# humidity endpoint is *derived* from the configured temperature one rather
# than configured beside it (see `humidity_url`): one address for one device,
# so moving the sensor is still a single environment variable.
TEMPERATURE_ENTITY = "Temperature"
HUMIDITY_ENTITY = "Humidity"

# The unit suffix on each entity's `state`. The `/sensor/...` endpoint carries
# no `uom` field (only the `/events` stream does), so `state` is the one place
# the payload names its unit.
SENSOR_UNIT_SUFFIX = " °C"
HUMIDITY_UNIT_SUFFIX = " %"

# One poll's worth of bad input must cost one poll, never the service — the
# same tuple and reasons as `outdoor.FETCH_FAILURES`.
FETCH_FAILURES = (
    OSError,
    http.client.HTTPException,
    KeyError,
    TypeError,
    ValueError,
)

POLL_FAILURES = (*FETCH_FAILURES, sqlite3.Error)


def make_fetch(url: str):
    def fetch() -> str:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def _finite(raw) -> float | None:
    """A finite float, or None. `json.loads` accepts a bare `NaN`."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def poll_minute(polled_at: datetime) -> str:
    """The row key: the poll clock in UTC, truncated to the minute."""
    return polled_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def humidity_url(temperature_url: str) -> str | None:
    """The humidity entity's endpoint beside `temperature_url`, or None.

    Derived rather than configured separately so one device has one address: a
    sensor that moves is `AWAIR_BASEMENT_URL` and nothing else, where two
    independent variables would let the humidity keep reading the old box.
    `AWAIR_BASEMENT_HUMIDITY_URL` overrides this, which is the escape hatch for
    the one fact here nobody has verified — the entity's name.

    None when the configured URL does not end in the temperature entity, and
    that is a refusal rather than a guess: the path is keyed on the entity, so
    appending `Humidity` to an unrecognised address would poll something whose
    shape we know nothing about. With no humidity URL the poller stores the
    temperature exactly as it did before this feed.
    """
    head, separator, entity = temperature_url.rpartition("/")
    if entity != TEMPERATURE_ENTITY:
        return None
    return f"{head}{separator}{HUMIDITY_ENTITY}"


def entity_value(payload, unit_suffix: str, quantity: str) -> float | None:
    """One ESPHome entity payload's reading, or None if it has none.

    None means the sensor is unavailable — a null, missing or non-finite
    `value`, which ESPHome is expected to publish as `"state": "NA"`. That is
    checked **before** the unit, so an `"NA"` state is not mistaken for a unit
    error. A finite value whose `state` carries another unit raises instead:
    that is a reconfigured entity, and storing it would be a silent
    30-degree (or, for humidity, a silently fractional) error.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"expected an object, got {type(payload).__name__}")
    value = _finite(payload.get("value"))
    if value is None:
        return None
    state = payload.get("state")
    if not isinstance(state, str) or not state.endswith(unit_suffix):
        raise ValueError(f"expected a {quantity} state, got {state!r}")
    return value


def read_humidity(fetch) -> float | None:
    """The humidity entity's reading, or None. **Never raises** (#156).

    The deliberate inverse of the temperature path, which reports a bad payload
    as an error and a wrong unit as an exception. Every failure this second GET
    can have — no URL to poll, a 404 from a renamed entity, a timeout, a body
    that is not JSON, a state in some other unit — comes back None and logs,
    because the row's reason to exist is the temperature and a humidity we
    cannot read must not take it down with it. A None here is stored as NULL,
    which is already what "not reported" means in this column.
    """
    if fetch is None:
        return None
    try:
        return entity_value(json.loads(fetch()), HUMIDITY_UNIT_SUFFIX, "percent")
    except FETCH_FAILURES as exc:
        log.warning("humidity read failed: %s: %s", type(exc).__name__, exc)
        return None


def parse_reading(payload, polled_at: datetime) -> dict | None:
    """One sensor payload as a `basement_readings` row, or None if no reading.

    None means the temperature entity is unavailable — see `entity_value`,
    which owns both that refusal and the unit check.

    `humid` comes back None and is filled in by `poll_once` once the
    temperature has parsed, so an unavailable sensor costs no second fetch and
    the humidity is never read for a row that will not be written.
    """
    temp = entity_value(payload, SENSOR_UNIT_SUFFIX, "Celsius")
    if temp is None:
        return None
    return {
        "ts": poll_minute(polled_at),
        "received_at": polled_at.astimezone(UTC).isoformat(),
        "temp": temp,
        "humid": None,
    }


def poll_once(conn, fetch, now=lambda: datetime.now(UTC), fetch_humidity=None) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'no_reading', 'error'. Those are
    the *temperature's* statuses and they stay that way: `fetch_humidity` is
    None when no humidity entity is configured, and a humidity that cannot be
    read is a NULL column rather than a fourth status, because a reader who
    saw 'error' here would go looking for a dead sensor.
    'no_reading' is the sensor saying it has nothing, not a fault.
    """
    try:
        payload = json.loads(fetch())
    except FETCH_FAILURES as exc:
        log.warning("sensor fetch failed: %s: %s", type(exc).__name__, exc)
        return "error"
    try:
        reading = parse_reading(payload, now())
        if reading is None:
            return "no_reading"
        reading["humid"] = read_humidity(fetch_humidity)
        inserted = db.insert_basement_reading(conn, reading)
    except POLL_FAILURES as exc:
        log.warning("payload or insert failed: %s: %s", type(exc).__name__, exc)
        return "error"
    return "inserted" if inserted else "duplicate"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    db_path = os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    sensor_url = os.environ.get("AWAIR_BASEMENT_URL", DEFAULT_SENSOR_URL)
    humidity = os.environ.get("AWAIR_BASEMENT_HUMIDITY_URL") or humidity_url(sensor_url)
    interval = poll_seconds()

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)
    fetch = make_fetch(sensor_url)
    fetch_humidity = None if humidity is None else make_fetch(humidity)
    log.info("polling %s every %ss into %s", sensor_url, interval, db_path)
    # At INFO even when there is nothing to say, because the alternative is a
    # silently temperature-only poller that looks identical to a working one.
    log.info(
        "humidity entity: %s", humidity or f"none (not a /{TEMPERATURE_ENTITY} URL)"
    )

    # Same clean-shutdown contract as the other pollers (#83).
    stop = install_handler()
    try:
        while not stop.is_set():
            status = poll_once(conn, fetch, fetch_humidity=fetch_humidity)
            log.log(
                logging.WARNING if status == "error" else logging.INFO,
                "basement poll: %s",
                status,
            )
            if stop.wait(interval):
                break
    finally:
        conn.close()
    log.info("basement poller stopped cleanly")


if __name__ == "__main__":
    main()
