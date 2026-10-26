"""Poll the house climate bridge for the dining mini split and store readings.

Run as: python -m awair.minisplit
Config via environment:
  AWAIR_MINISPLIT_URL           — default DEFAULT_STATUS_URL, the bridge's
                                  loopback `/status` on the homelab
  AWAIR_MINISPLIT_POLL_SECONDS  — default 300 (the bridge's own control cycle)
  AWAIR_DB                      — shared with the indoor and outdoor pollers

The readings come from parsons-pulse's climate sidecar, not from SmartThings:
there is one SmartThings grant for the house, its refresh token is single-use,
and a second copy of it breaks the first (parsons-pulse ADR-022). The bridge
already fetches the head's status every cycle, so reading its `/status` costs no
API call and no new credential — and it is loopback-only, so this poller must
run on the same box as the bridge.

**Every row is keyed on the head's own power-report window close**, never on our
poll clock. The device reports on its own cadence — windows of 220 s and 800 s
were measured against the bridge's 300 s cycle on 2026-10-26 — so most polls
re-read a window that is already stored, and `ON CONFLICT(ts) DO NOTHING` makes
that write nothing. A payload whose window cannot be dated is **unkeyed**: it is
dropped with a warning rather than stored under a clock we made up, which is the
same refusal `aq_ts` records for an undateable AQI.
"""

import http.client
import json
import logging
import math
import os
import sqlite3
import urllib.request
from datetime import UTC, datetime

from awair import db, units
from awair.shutdown import install_handler

log = logging.getLogger("awair.minisplit")

FETCH_TIMEOUT_SECONDS = 10

# The bridge's control cycle, which is the fastest rate at which a new report
# window can reach us. Polling faster buys nothing: `/status` republishes the
# same window until the bridge's next cycle reads a new one.
DEFAULT_POLL_SECONDS = 300

# Loopback, deliberately — the bridge binds `127.0.0.1:8100` and exposes no
# authentication, so it is reachable only from the box it runs on.
DEFAULT_STATUS_URL = "http://127.0.0.1:8100/status"

# The unit the bridge works in: `climatesidecar.devices.BRIDGE_UNIT` is `"F"`,
# and the bridge refuses to range-check a setpoint for a head reporting anything
# else rather than guessing. Named here rather than inlined because it is the
# input to the one conversion in this project that runs *into* storage.
BRIDGE_UNIT = "F"

# The longest power-report window observed on the dining head: 800 s, against
# the bridge's 300 s cycle (#139, three consecutive windows on 2026-10-26).
# `web` derives the chart's carry-forward bound from this; see
# `web._minisplit_carry_max_age_seconds`.
MAX_REPORT_WINDOW_SECONDS = 800

# One poll's worth of bad input must cost one poll, never the service — the
# contract `outdoor.FETCH_FAILURES` states and the same tuple for the same
# reasons. `http.client.HTTPException` is not an `OSError` subclass and urllib
# does not convert it, so a truncated response escapes an `OSError`-only guard.
FETCH_FAILURES = (
    OSError,
    http.client.HTTPException,
    KeyError,
    TypeError,
    ValueError,
)

# The insert sits inside the same guard as the parse, so the insert's own
# failures belong in the tuple: `parse_reading` validates the one field it
# requires and hands six optional ones to the driver, making a nested object or
# array in any of them a `sqlite3.ProgrammingError` at bind time.
POLL_FAILURES = (*FETCH_FAILURES, sqlite3.Error)


def make_fetch(url: str):
    def fetch() -> str:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def _moment(raw) -> datetime | None:
    """One bridge timestamp as an aware UTC datetime, or None.

    The bridge publishes these through its own `_iso()`, which is
    `astimezone(UTC).isoformat()`, so a well-formed stamp always carries an
    offset. **Requiring the offset is what refuses a date-only string**, and
    that is the whole check: measured on 3.13.7, `fromisoformat` reads the
    `+05:00` of `"2026-10-26+05:00"` as a *time* rather than an offset, so
    every date-only shape comes back naive and is refused here. A separate
    "does it carry a time separator" test would be dead code — the bridge found
    exactly that with a mutation round and removed it (parsons-pulse #182).

    `isinstance` is load-bearing and separate: `fromisoformat` raises
    `TypeError` rather than `ValueError` for a non-string.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _finite(raw) -> float | None:
    """A finite float, or None. Applied to every number this poller stores.

    The bridge finite-checks its two draw quantities and does **not** check
    `temperature` — it says so, and calls that a pre-existing defect of its
    own rather than a safe field. Since `json.loads` accepts a bare `NaN` and
    `Infinity` by default, an unchecked value would reach the driver: SQLite
    stores a NaN as NULL (indistinguishable from "not reported") but binds an
    infinity as a real, which would then be carried onto the chart and
    autoscale the whole axis to it.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if math.isfinite(value) else None


def _text(raw) -> str | None:
    return raw if isinstance(raw, str) else None


def metered_watts(delta_wh, window_start, window_end) -> float | None:
    """Mean draw in watts over one report window, or None when undefined.

    This is the number worth plotting, and the spot reading beside it is not:
    `watts` read **30.0 every time** across three consecutive windows on
    2026-10-26 while the energy the device metered over those same windows came
    to 45, 54 and 65 W. A chart of the spot value alone is a flat line.

    None rather than a fallback whenever the window cannot be measured — no
    energy, no start, or a start at-or-after the close. The observed windows
    differ by a factor of nearly four, so there is no default length close
    enough to be harmless, and a fabricated mean is a wrong answer where None
    is a missing one.

    `window_start` and `window_end` are aware datetimes; `delta_wh` is the
    device's `deltaEnergy` in watt-hours over exactly that window.
    """
    if delta_wh is None or window_start is None or window_end is None:
        return None
    seconds = (window_end - window_start).total_seconds()
    if seconds <= 0:
        return None
    return round(delta_wh * 3600 / seconds, 2)


def parse_reading(payload, received_at: str) -> dict | None:
    """One `/status` payload as a `minisplit_readings` row, or None if unkeyed.

    None means the payload carries no datable report window — `head` is null
    because the bridge has not completed a cycle, or the bridge predates the
    draw fields, or the head reported a draw this bridge could not date. All
    three are ordinary states rather than errors, and all three are refusals to
    store: the row's whole identity is the window's close, so there is nothing
    to key the reading on and our poll clock is not a substitute.

    **A payload that is not this contract raises instead**, so `poll_once`
    reports it as an error. `payload["head"]` rather than `.get("head")` is
    what draws that line: a body with no `head` key at all, or one that is
    neither an object nor null, is a different endpoint or a broken bridge, and
    answering it with the one status that means "nothing is wrong yet" would
    hide a fault behind the state this feed expects to be in for now.

    Note what is *not* required: a window with a close but no energy still
    produces a row. The close is what makes the reading identifiable, and the
    head's own temperature — the half of #139 that works today — travels on
    the same row.

    Temperature is converted Fahrenheit → Celsius **here**, which makes this
    the one conversion in this project that runs into storage rather than out
    of it. The alternative is a single Fahrenheit column in a database whose
    every other temperature is Celsius, which is the silent 30-degree error
    `web` names as its reason for publishing units as literals.
    """
    head = payload["head"]
    if head is None:
        return None
    if not isinstance(head, dict):
        raise TypeError(f"`head` is a {type(head).__name__}, not an object or null")
    window_end = _moment(head.get("watts_at"))
    if window_end is None:
        return None
    window_start = _moment(head.get("watts_from"))
    return {
        "ts": window_end.isoformat(),
        "received_at": received_at,
        "window_start": None if window_start is None else window_start.isoformat(),
        "delta_wh": _finite(head.get("delta_wh")),
        "watts_spot": _finite(head.get("watts")),
        "temp": units.to_celsius(_finite(head.get("temperature")), BRIDGE_UNIT),
        "mode": _text(head.get("mode")),
        "switch": _text(head.get("power")),
    }


def poll_once(conn, fetch) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'unkeyed', 'error'.

    'unkeyed' is the status that matters here, and it is deliberately not
    'error': until parsons-pulse #182 is merged **and** deployed, the bridge on
    the box publishes no draw fields at all, so every poll is legitimately
    unkeyed and a run of them says the dependency has not landed yet — not that
    anything is broken. 'error' is reserved for a fetch, a payload or an insert
    that failed.
    """
    try:
        payload = json.loads(fetch())
    except FETCH_FAILURES as exc:
        # TypeError covers a fetcher returning None, which is `json.loads(None)`.
        log.warning("bridge fetch failed: %s: %s", type(exc).__name__, exc)
        return "error"
    try:
        reading = parse_reading(payload, received_at=datetime.now(UTC).isoformat())
        if reading is None:
            return "unkeyed"
        inserted = db.insert_minisplit_reading(conn, reading)
    except POLL_FAILURES as exc:
        # The insert is inside the guard with the parse: a bind-time
        # `ProgrammingError` from an unchecked optional column, or an
        # `OperationalError` from a full disk, would otherwise unwind `main`'s
        # loop and systemd would restart straight back into the same payload.
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
    status_url = os.environ.get("AWAIR_MINISPLIT_URL", DEFAULT_STATUS_URL)
    interval = int(os.environ.get("AWAIR_MINISPLIT_POLL_SECONDS", DEFAULT_POLL_SECONDS))

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)
    fetch = make_fetch(status_url)
    log.info("polling %s every %ss into %s", status_url, interval, db_path)

    # Same clean-shutdown contract as the other two pollers (#83).
    stop = install_handler()
    try:
        while not stop.is_set():
            status = poll_once(conn, fetch)
            # 'unkeyed' logs at INFO alongside the two success statuses, not at
            # WARNING: it is the expected state on every box whose bridge
            # predates the draw fields, and a warning per 300 s would train
            # whoever reads this journal to ignore the level.
            log.log(
                logging.WARNING if status == "error" else logging.INFO,
                "mini split poll: %s",
                status,
            )
            if stop.wait(interval):
                break
    finally:
        conn.close()
    log.info("mini split poller stopped cleanly")


if __name__ == "__main__":
    main()
