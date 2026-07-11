"""Poll the Awair Element Local API and store readings.

Run as: python -m awair.poller
Config via environment: AWAIR_URL, AWAIR_DB, AWAIR_POLL_SECONDS.
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone

from awair import db
from awair.alerts import Notifier
from awair.monitor import DeviceHealth, check_metrics

log = logging.getLogger("awair.poller")

DEVICE_FIELDS = (
    "score", "temp", "humid", "abs_humid", "dew_point",
    "co2", "co2_est", "co2_est_baseline",
    "voc", "voc_baseline", "voc_h2_raw", "voc_ethanol_raw",
    "pm25", "pm10_est",
)

FETCH_TIMEOUT_SECONDS = 5


def parse_reading(payload: dict, received_at: str) -> dict:
    """Map one /air-data/latest payload to a readings row.

    The device timestamp is required (it is the dedup key); sensor fields
    are optional so a firmware change dropping one field degrades to NULL
    instead of halting ingestion.
    """
    reading = {"ts": payload["timestamp"], "received_at": received_at}
    for field in DEVICE_FIELDS:
        reading[field] = payload.get(field)
    return reading


def poll_once(conn, fetch) -> str:
    """One poll iteration: 'inserted', 'duplicate', or 'error'."""
    try:
        payload = json.loads(fetch())
        reading = parse_reading(
            payload, received_at=datetime.now(timezone.utc).isoformat()
        )
    except (OSError, ValueError, KeyError) as exc:
        log.warning("poll failed: %s", exc)
        return "error"
    if db.insert_reading(conn, reading):
        return "inserted"
    return "duplicate"


def make_fetch(url: str):
    def fetch() -> str:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def handle_device_health(conn, notifier, health, status, now) -> None:
    """Map a DeviceHealth verdict onto an alert event + notification."""
    verdict = health.observe(status)
    if verdict in ("unreachable", "stale"):
        notified = notifier.send(
            f"Awair Element {verdict} (~5 min of polls)",
            title=f"Awair device {verdict}", priority="high",
        )
        db.open_event(
            conn, metric="device", tier=verdict, opened_at=now,
            value=None, baseline=None, threshold=None, notified=notified,
        )
    elif verdict == "recovered":
        event = db.get_open_events(conn).get("device")
        notified = notifier.send("Awair Element recovered", title="Awair device recovered")
        if event:
            db.close_event(conn, event["id"], closed_at=now, notified=notified)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = os.environ.get("AWAIR_URL", "http://192.168.68.51/air-data/latest")
    db_path = os.environ.get("AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db"))
    interval = int(os.environ.get("AWAIR_POLL_SECONDS", "30"))
    notifier = Notifier(
        base_url=os.environ.get("AWAIR_NTFY_URL", "https://notifications.tomclancy.info"),
        topic=os.environ.get("AWAIR_NTFY_TOPIC", "awair"),
        token=os.environ.get("AWAIR_NTFY_TOKEN", ""),
    )
    health = DeviceHealth()

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)
    fetch = make_fetch(url)
    log.info("polling %s every %ss into %s", url, interval, db_path)

    while True:
        status = poll_once(conn, fetch)
        log.log(logging.INFO if status == "inserted" else logging.WARNING,
                "poll: %s", status)
        now = datetime.now(timezone.utc)
        if status == "inserted":
            check_metrics(conn, notifier, now)
        handle_device_health(conn, notifier, health, status, now)
        time.sleep(interval)


if __name__ == "__main__":
    main()
