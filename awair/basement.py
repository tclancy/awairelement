"""Poll the basement ESPHome sensor and store its temperature.

Run as: python -m awair.basement
Config via environment:
  AWAIR_BASEMENT_URL           — default DEFAULT_SENSOR_URL, the sensor's
                                 ESPHome web-server entity endpoint
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

# Reserved in the Deco by MAC, so an IP literal is stable and needs no mDNS.
DEFAULT_SENSOR_URL = "http://192.168.68.50/sensor/Temperature"

# The unit suffix on the entity's `state`. The `/sensor/...` endpoint carries
# no `uom` field (only the `/events` stream does), so `state` is the one place
# the payload names its unit.
SENSOR_UNIT_SUFFIX = " °C"

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


def parse_reading(payload, polled_at: datetime) -> dict | None:
    """One sensor payload as a `basement_readings` row, or None if no reading.

    None means the sensor is unavailable — a null, missing or non-finite
    `value`, which ESPHome is expected to publish as `"state": "NA"`. That is
    checked **before** the unit, so an `"NA"` state is not mistaken for a unit
    error. A finite value whose `state` is not Celsius raises instead: that is
    a reconfigured entity, and storing it would be a silent 30-degree error.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"expected an object, got {type(payload).__name__}")
    temp = _finite(payload.get("value"))
    if temp is None:
        return None
    state = payload.get("state")
    if not isinstance(state, str) or not state.endswith(SENSOR_UNIT_SUFFIX):
        raise ValueError(f"expected a Celsius state, got {state!r}")
    return {
        "ts": poll_minute(polled_at),
        "received_at": polled_at.astimezone(UTC).isoformat(),
        "temp": temp,
    }


def poll_once(conn, fetch, now=lambda: datetime.now(UTC)) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'no_reading', 'error'.
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
    interval = int(os.environ.get("AWAIR_BASEMENT_POLL_SECONDS", DEFAULT_POLL_SECONDS))

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)
    fetch = make_fetch(sensor_url)
    log.info("polling %s every %ss into %s", sensor_url, interval, db_path)

    # Same clean-shutdown contract as the other pollers (#83).
    stop = install_handler()
    try:
        while not stop.is_set():
            status = poll_once(conn, fetch)
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
