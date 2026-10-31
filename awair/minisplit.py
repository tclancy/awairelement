"""Poll the house climate bridge for both mini split heads and store readings.

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

**Every row is keyed on the bridge cycle's own clock** (`last_decision.at`),
never on our poll clock and never on the device's power-report window. One row
per bridge cycle, so a re-poll inside one cycle writes nothing — that is what
the dedup is for — while the head's room temperature, which the bridge reads
fresh every cycle, is never suppressed by a power-report window that has not
moved. The window's own bounds ride along in nullable columns, which is the
shape `outdoor_readings` and `aq_ts` already use for a slow auxiliary beside a
fast key. A payload with no datable cycle is **unkeyed**: dropped rather than
stored under a clock we made up, the same refusal `aq_ts` records for an
undateable AQI.

The practical half of that choice: `last_decision.at` is published by the
bridge **already on `origin/main`**, so this feed starts recording the dining
head's temperature on the bridge deployed today and the draw columns fill in
when parsons-pulse #182 lands.
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

# The unit the bridge works in: `climatesidecar.devices.BRIDGE_UNIT` is `"F"`.
# Since parsons-pulse#182 the bridge enforces it on the head's temperature: a
# head reporting any other unit (or none) publishes `temperature: null`, never
# a number in the wrong scale. It does NOT gate the setpoint's unit, which is
# why `_setpoint_celsius` reads the unit from the raw status itself. The
# schedule targets are the bridge's own configuration, in F. So this constant is
# a contract the bridge keeps rather than an assumption. Named here rather than
# inlined because it is the input to the one conversion in this project that
# runs *into* storage.
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


# The heads, as this table names them. Dining is the top level of `/status`
# and the bedroom is its `bedroom` key (parsons-pulse ADR-025), null when that
# zone is configured off.
DINING = "dining"
BEDROOM = "bedroom"


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
    energy, a negative energy, no start, or a start at-or-after the close. The observed windows
    differ by a factor of nearly four, so there is no default length close
    enough to be harmless, and a fabricated mean is a wrong answer where None
    is a missing one.

    `window_start` and `window_end` are aware datetimes; `delta_wh` is the
    device's `deltaEnergy` in watt-hours over exactly that window.
    """
    if delta_wh is None or window_start is None or window_end is None:
        return None
    # A head cannot draw negative power, so a negative `deltaEnergy` is
    # upstream garbage or a counter rollover. Plotted, it would autoscale the
    # whole axis to a number that never happened -- the same harm `_finite`
    # exists to prevent one function up, arriving as a finite value.
    if delta_wh < 0:
        return None
    seconds = (window_end - window_start).total_seconds()
    if seconds <= 0:
        return None
    return round(delta_wh * 3600 / seconds, 2)


def zones(payload) -> list:
    """[(head, zone block or None)] for each head, from one `/status` body.

    Dining's block is the top level **without** `bedroom`, so dining's row never
    carries the other head. A body with no `bedroom` key at all (a bridge older
    than ADR-025) reads as the zone being off.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"`/status` must be an object, got {type(payload).__name__}")
    dining = {key: value for key, value in payload.items() if key != BEDROOM}
    return [(DINING, dining), (BEDROOM, payload.get(BEDROOM))]


def _setpoint_celsius(head: dict) -> float | None:
    """The head's setpoint in °C, only when its own status says it is in F.

    The bridge publishes `setpoint` without checking the unit, so the unit is
    read from the raw SmartThings attribute beside it. No raw status (a bridge
    predating it) or any other unit is null, never a guess.
    """
    raw = head.get("raw")
    try:
        unit = raw["components"]["main"]["thermostatCoolingSetpoint"][
            "coolingSetpoint"
        ]["unit"]
    except (KeyError, TypeError):
        return None
    if unit != BRIDGE_UNIT:
        return None
    return units.to_celsius(_finite(head.get("setpoint")), BRIDGE_UNIT)


def _cycle_target(decision: dict) -> tuple[float | None, str | None]:
    """The target the cycle steered to, in °C, and where it came from.

    `last_decision.target`, not the top-level `target`: the bridge computes
    that one when `/status` is requested, so across a schedule boundary it is
    the next step's, not this row's.
    """
    target = decision.get("target")
    if not isinstance(target, dict):
        return None, None
    value = units.to_celsius(_finite(target.get("value")), BRIDGE_UNIT)
    return value, _text(target.get("source"))


def _raw_json(head: dict) -> str | None:
    raw = head.get("raw")
    return json.dumps(raw, sort_keys=True) if isinstance(raw, dict) else None


def _without_timestamps(node):
    """`node` with every `timestamp` key removed, at any depth.

    SmartThings stamps each attribute when it was last *reported*, so two
    bodies with identical readings differ in their stamps alone.
    """
    if isinstance(node, dict):
        return {k: _without_timestamps(v) for k, v in node.items() if k != "timestamp"}
    if isinstance(node, list):
        return [_without_timestamps(item) for item in node]
    return node


def raw_to_store(raw: str | None, previous: str | None) -> str | None:
    """`raw` when it says something `previous` did not, else None.

    A head's status body is about 13.7 KB (measured 2026-10-31) and most of it
    is static -- model, firmware, filter settings -- so it is stored only when
    a value changes. A null `raw` on a row therefore means "the same as the
    last stored one", not "not reported".
    """
    if raw is None or previous is None:
        return raw
    if _without_timestamps(json.loads(raw)) == _without_timestamps(
        json.loads(previous)
    ):
        return None
    return raw


def parse_reading(payload, received_at: str, head_name: str = DINING) -> dict | None:
    """One `/status` payload as a `minisplit_readings` row, or None if unkeyed.

    None means the bridge has not completed a cycle, so there is no observation
    clock and no head reading to attach to one. That is an ordinary state
    rather than an error, and it is a refusal to store: the row's identity is
    the cycle's own clock, and our poll clock is not a substitute.

    **A payload that is not this contract raises instead**, so `poll_once`
    reports it as an error. `payload["last_decision"]` rather than
    `.get(...)` is what draws that line: a body with no `last_decision` key at
    all, or a `head` that is neither an object nor null, is a different
    endpoint or a broken bridge, and answering it with the one status that
    means "nothing is wrong yet" would hide a fault.

    Note what is *not* required: the power-report window. A cycle with a
    temperature and no datable window still produces a row, which is the whole
    reason the key is the cycle rather than the window — on the bridge
    deployed today that is *every* row, and keyed the other way this feed would
    store nothing at all.

    Temperature is converted Fahrenheit → Celsius **here**, which makes this
    the one conversion in this project that runs into storage rather than out
    of it. The alternative is a single Fahrenheit column in a database whose
    every other temperature is Celsius, which is the silent 30-degree error
    `web` names as its reason for publishing units as literals.
    """
    decision = payload["last_decision"]
    head = payload["head"]
    if decision is None or head is None:
        # Both are `None` exactly when the bridge has not completed a cycle --
        # `status()` derives them from the same `self._last`. Either one alone
        # would do; both are read so a bridge that ever decouples them is
        # unkeyed rather than half-stored.
        return None
    if not isinstance(decision, dict) or not isinstance(head, dict):
        raise TypeError(
            "`last_decision` and `head` must each be an object or null, got "
            f"{type(decision).__name__} and {type(head).__name__}"
        )
    cycle_at = _moment(decision["at"])
    if cycle_at is None:
        return None
    window_start = _moment(head.get("watts_from"))
    window_close = _moment(head.get("watts_at"))
    temperature_at = _moment(head.get("temperature_at"))
    target, target_source = _cycle_target(decision)
    return {
        "head": head_name,
        "ts": cycle_at.isoformat(),
        "received_at": received_at,
        "window_start": None if window_start is None else window_start.isoformat(),
        "window_close": None if window_close is None else window_close.isoformat(),
        "delta_wh": _finite(head.get("delta_wh")),
        "watts_spot": _finite(head.get("watts")),
        "temp": units.to_celsius(_finite(head.get("temperature")), BRIDGE_UNIT),
        "mode": _text(head.get("mode")),
        "switch": _text(head.get("power")),
        "setpoint": _setpoint_celsius(head),
        "target": target,
        "target_source": target_source,
        "decision_reason": _text(decision.get("reason")),
        "temperature_at": None
        if temperature_at is None
        else temperature_at.isoformat(),
        "raw": _raw_json(head),
    }


def poll_once(conn, fetch) -> dict:
    """One poll iteration: one fetch, then each head on its own.

    Returns {head: status}, each one of 'inserted', 'duplicate', 'unkeyed',
    'error'. A fetch that fails is 'error' for every head; after that each head
    is parsed and inserted inside its own guard, so a bad bedroom block costs
    the bedroom row and never dining's.

    'unkeyed' is deliberately not 'error': a bridge that has not completed a
    cycle yet -- just restarted, or waiting out a SmartThings 429 -- publishes
    `last_decision: null`, and a bedroom zone configured off publishes
    `bedroom: null`. Both are states to wait through rather than faults.
    """
    try:
        heads = zones(json.loads(fetch()))
    except FETCH_FAILURES as exc:
        # TypeError covers a fetcher returning None, which is `json.loads(None)`.
        log.warning("bridge fetch failed: %s: %s", type(exc).__name__, exc)
        return {DINING: "error", BEDROOM: "error"}
    received_at = datetime.now(UTC).isoformat()
    return {name: _poll_head(conn, name, zone, received_at) for name, zone in heads}


def _poll_head(conn, name: str, zone, received_at: str) -> str:
    if zone is None:
        return "unkeyed"
    try:
        reading = parse_reading(zone, received_at=received_at, head_name=name)
        if reading is None:
            return "unkeyed"
        reading["raw"] = raw_to_store(
            reading["raw"], db.latest_minisplit_raw(conn, name)
        )
        inserted = db.insert_minisplit_reading(conn, reading)
    except POLL_FAILURES as exc:
        # The insert is inside the guard with the parse: a bind-time
        # `ProgrammingError` from an unchecked optional column, or an
        # `OperationalError` from a full disk, would otherwise unwind `main`'s
        # loop and systemd would restart straight back into the same payload.
        log.warning(
            "%s payload or insert failed: %s: %s", name, type(exc).__name__, exc
        )
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
            # 'unkeyed' logs at INFO alongside the two success statuses, not at
            # WARNING: a bedroom zone configured off is unkeyed on every poll,
            # and a warning per 300 s would train whoever reads this journal to
            # ignore the level.
            for name, status in poll_once(conn, fetch).items():
                log.log(
                    logging.WARNING if status == "error" else logging.INFO,
                    "mini split poll: %s %s",
                    name,
                    status,
                )
            if stop.wait(interval):
                break
    finally:
        conn.close()
    log.info("mini split poller stopped cleanly")


if __name__ == "__main__":
    main()
