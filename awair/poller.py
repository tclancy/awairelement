"""Poll the Awair Element Local API and store readings.

Run as: python -m awair.poller
       python -m awair.poller --test   (fan/ntfy smoke test, then exit)
Config via environment: AWAIR_URL, AWAIR_DB, AWAIR_POLL_SECONDS.
"""

import argparse
import http.client
import json
import logging
import os
import sqlite3
import urllib.request
from datetime import UTC, datetime

from awair import db, fans
from awair.alerts import Notifier
from awair.fans import (
    check_fans,
    config_from_env as fans_config_from_env,
    run_fan_test,
)
from awair.monitor import DeviceHealth, check_metrics
from awair.shutdown import install_handler

log = logging.getLogger("awair.poller")

DEVICE_FIELDS = (
    "score",
    "temp",
    "humid",
    "abs_humid",
    "dew_point",
    "co2",
    "co2_est",
    "co2_est_baseline",
    "voc",
    "voc_baseline",
    "voc_h2_raw",
    "voc_ethanol_raw",
    "pm25",
    "pm10_est",
)

FETCH_TIMEOUT_SECONDS = 5


def parse_reading(payload: dict, received_at: str) -> dict:
    """Map one /air-data/latest payload to a readings row.

    The device timestamp is required (it is the dedup key); sensor fields
    are optional so a firmware change dropping one field degrades to NULL
    instead of halting ingestion.

    A non-string timestamp is rejected along with an empty one, and that is a
    deliberate narrowing: TEXT affinity used to let an integer epoch through,
    so it stored and alerted. Nothing downstream could read it -- every
    consumer calls `fromisoformat` on `ts` -- so ingesting it only moved the
    failure somewhere quieter. The device publishes ISO-8601 strings.

    A *present but empty* timestamp is rejected here rather than passed on,
    and that is the half of #95 that actually restores alerting's failure
    signal. `readings.ts` is `TEXT NOT NULL`, so handing SQLite a null used to
    come back as rowcount 0 -- indistinguishable from a dedup hit -- and the
    poll logged `poll: duplicate` while storing nothing and running no spike
    check. Raising is what turns that into a logged `"error"` with a reason.
    """
    timestamp = payload["timestamp"]
    if not timestamp or not isinstance(timestamp, str):
        raise ValueError(f"device timestamp is not a usable string: {timestamp!r}")
    reading = {"ts": timestamp, "received_at": received_at}
    for field in DEVICE_FIELDS:
        reading[field] = payload.get(field)
    return reading


# What a bad upstream payload is allowed to cost: one poll, never the service
# (#95, extending the ruling PR #93 made for the outdoor poller).
#
# The contract in that sentence is the point, so this tuple is written to make
# it TRUE rather than to enumerate the shapes we happened to reproduce:
#
# - TypeError is the one that was missing and the one that mattered:
#   `parse_reading` subscripts the payload, so a list, a string or a number
#   raises TypeError rather than KeyError, and `json.loads(None)` raises it
#   too. All four escaped and unwound `main()`, which has no `except` of its
#   own (its `try` is a `finally` that closes the connection).
# - sqlite3.Error, not sqlite3.IntegrityError. `parse_reading` validates `ts`
#   but hands the 14 sensor fields to the driver unchecked, so a nested object
#   in any of them is a ProgrammingError at bind time -- a natural caller the
#   narrower class does not cover. sqlite3.Error also picks up
#   OperationalError, which is "disk full" and "database is locked": a full SD
#   card should cost polls, not the service.
# - http.client.HTTPException for a truncated or malformed response from the
#   device. IncompleteRead and BadStatusLine are not OSError subclasses and
#   urllib does not convert them, so they escaped too.
#
# This DIVERGES from the outdoor poller on purpose. #91 ruled that a
# sqlite3.Error there should propagate, "a local fault a restart can clear",
# and left `insert_outdoor_reading` outside the guard. That reasoning holds for
# OperationalError and not for ProgrammingError, which is an *upstream payload*
# fault that has merely travelled as far as the bind: a restart puts the poller
# straight back into the same value, which is the crash loop #91 and #95 both
# exist to prevent. Nor does exiting clear a full disk. What the divergence
# costs is that a local fault is now a logged "error" instead of a dead unit --
# and DeviceHealth escalates a sustained run of those to "unreachable", so it is
# still reported, just not by dying. See #98 for the outdoor side.
POLL_FAILURES = (
    OSError,
    http.client.HTTPException,
    KeyError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


def poll_once(conn, fetch) -> str:
    """One poll iteration: 'inserted', 'duplicate', or 'error'.

    The insert sits INSIDE the guard, unlike before #95. `insert_reading` can
    raise now that it no longer swallows constraint violations, and an
    exception from there would unwind `main()` exactly as the parse failures
    did.
    """
    try:
        payload = json.loads(fetch())
        reading = parse_reading(payload, received_at=datetime.now(UTC).isoformat())
        inserted = db.insert_reading(conn, reading)
    except POLL_FAILURES as exc:
        log.warning("poll failed: %s", exc)
        return "error"
    return "inserted" if inserted else "duplicate"


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
            title=f"Awair device {verdict}",
            priority="high",
        )
        db.open_event(
            conn,
            metric="device",
            tier=verdict,
            opened_at=now,
            value=None,
            baseline=None,
            threshold=None,
            notified=notified,
        )
    elif verdict == "recovered":
        event = db.get_open_events(conn).get("device")
        notified = notifier.send(
            "Awair Element recovered", title="Awair device recovered"
        )
        if event:
            db.close_event(conn, event["id"], closed_at=now, notified=notified)


def _fan_mitigation_status(fans_config) -> str:
    """One word for the startup banner: on, off, or disabled-in-code.

    The third state survives ADR-002 (which turned mitigation back on) because the
    kill switch survives: "disabled in code" and "off" have different fixes, and
    a banner that conflated them would send you to the Ansible variable when the
    answer is a constant in `awair.fans`.
    """
    if fans.MITIGATION_RETIRED:
        return "disabled in code"
    return "on" if fans_config.enabled else "off"


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="python -m awair.poller")
    parser.add_argument(
        "--test",
        action="store_true",
        help="turn the fans on, send a 'Fan test' ntfy notification, and exit",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    url = os.environ.get("AWAIR_URL", "http://192.168.68.51/air-data/latest")
    db_path = os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    interval = int(os.environ.get("AWAIR_POLL_SECONDS", "30"))
    notifier = Notifier(
        base_url=os.environ.get(
            "AWAIR_NTFY_URL", "https://notifications.tomclancy.info"
        ),
        topic=os.environ.get("AWAIR_NTFY_TOPIC", "awair"),
        token=os.environ.get("AWAIR_NTFY_TOKEN", ""),
    )
    health = DeviceHealth()
    fans_config = fans_config_from_env()

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)

    if args.test:
        try:
            run_fan_test(conn, notifier, fans_config, datetime.now(UTC))
        finally:
            conn.close()
        return

    fetch = make_fetch(url)
    log.info(
        "polling %s every %ss into %s (fan mitigation: %s)",
        url,
        interval,
        db_path,
        # "disabled in code" is a third state, not a synonym for "off": the
        # warning in config_from_env only fires when the env still asks for
        # fans, so this line is the only thing that tells a code-level kill
        # switch apart from someone having simply left the Ansible flag false.
        _fan_mitigation_status(fans_config),
    )

    # Stop on SIGTERM rather than being killed mid-loop (#83): being killed
    # exits non-zero, which systemd reports as a failure on every restart.
    stop = install_handler()
    try:
        while not stop.is_set():
            status = poll_once(conn, fetch)
            log.log(
                logging.INFO if status == "inserted" else logging.WARNING,
                "poll: %s",
                status,
            )
            now = datetime.now(UTC)
            if status == "inserted":
                check_metrics(conn, notifier, now)
                check_fans(conn, notifier, fans_config, now)
            handle_device_health(conn, notifier, health, status, now)
            # Returns True the moment a signal lands, so a deploy does not wait
            # out the rest of the interval. A poll already under way finishes.
            if stop.wait(interval):
                break
    finally:
        conn.close()
    log.info("poller stopped cleanly")


if __name__ == "__main__":
    main()
