"""Poll the National Weather Service active-alerts feed for the parcel (#79).

Tornado and hurricane warnings have no Open-Meteo equivalent, and they are two
of the three conditions the house hub paints its card red for. NWS publishes
them free, keyed on a point, with no API key and no account:

    GET https://api.weather.gov/alerts/active?point=<lat>,<lon>

**Why this lives in awairelement rather than in the hub.** This app already
polls the WAN every 15 minutes against `AWAIR_LAT`/`AWAIR_LON` and already owns
"outdoor conditions". The hub is a viewport with no producer and no WAN reach
by design — its ADR-007 forbids anything but plain HTTP to a LAN IP literal —
so alerts landing here means the hub needs no new process, no systemd unit and
no Ansible role. Decided with Tom, 2026-09-05 (#79).

**Naming.** `awair.alerts` is the ntfy `Notifier` and `alert_events` is this
app's own spike bookkeeping, both of which predate this module by a year. A
*weather alert* is a third thing — someone else's published warning, which we
store and republish and never raise ourselves. The module, the table and the
endpoint all spell it out for that reason; see GLOSSARY.md.

The one rule everything here is shaped around: **a failed poll must never
render as "no alerts."** That is the single direction in which a missing number
becomes a false all-clear about a tornado. So nothing in this module deletes a
row, and "currently active" is derived from the *last successful* poll's clock
rather than from the table being empty.
"""

import http.client
import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from awair import db

log = logging.getLogger("awair.weather_alerts")

FETCH_TIMEOUT_SECONDS = 10
DEFAULT_ALERTS_URL = "https://api.weather.gov/alerts/active"

# NWS policy asks every client to identify itself and offer a contact route;
# unidentified traffic is rate-limited or blocked. A project URL satisfies it
# without putting an address in a public repo. Override with
# `AWAIR_NWS_USER_AGENT` if this deployment wants to be reachable directly.
DEFAULT_USER_AGENT = "awairelement (https://github.com/tclancy/awairelement)"

# Properties a feature must carry to be storable: both are NOT NULL columns,
# and an alert with no `event` has nothing a card could say. Both are required
# by CAP, so an absent one is upstream drift rather than an ordinary shape.
REQUIRED_PROPERTIES = ("id", "event")

# Same contract as `outdoor.FETCH_FAILURES`, and the same reasoning: a bad
# upstream payload costs one poll, never the process. `http.client.HTTPException`
# is here because `IncompleteRead` and `BadStatusLine` are not `OSError`
# subclasses and urllib does not convert them (#98).
POLL_FAILURES = (
    OSError,
    http.client.HTTPException,
    KeyError,
    TypeError,
    ValueError,
)


def build_url(base: str, lat: float, lon: float) -> str:
    """One `alerts/active?point=lat,lon` request.

    The point form rather than `area=<state>`: a state-wide feed would paint
    the card red for a warning two hundred miles away. NWS resolves the point
    to the zones that cover it.
    """
    params = urllib.parse.urlencode({"point": f"{lat},{lon}"})
    return f"{base}?{params}"


def make_fetch(url: str, user_agent: str):
    """A fetcher that sends `User-Agent`, which `outdoor.make_fetch` does not.

    Separate from its outdoor sibling for exactly that reason. urllib's default
    agent is `Python-urllib/3.x`, which NWS treats as unidentified traffic; a
    header-bearing request needs a `Request` object rather than a bare URL, so
    the two cannot share one helper without the outdoor one growing a parameter
    it has no use for.
    """

    def fetch() -> str:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.read().decode()

    return fetch


def _feature_to_alert(feature) -> dict:
    """One GeoJSON feature as a `weather_alerts` row.

    Keyed on `properties.id` — the CAP urn — and not on the feature's own `id`,
    which is the api.weather.gov URL for the same alert. The urn is what NWS
    keeps stable while it revises a live alert in place.

    The `isinstance` is not belt-and-braces. `feature["properties"]` raises
    `KeyError`/`TypeError`, both of which `POLL_FAILURES` carries, but a
    *present and null* `properties` gets past that and then raises
    `AttributeError` out of `.get` — which is in neither tuple, so it would
    escape `poll_once` and unwind the `while` loop in `outdoor.main()`. Two
    accessor styles, two exception classes; the same gap `_is_readable_air_quality`
    exists to close on the Open-Meteo side (#91 review).
    """
    properties = feature["properties"]
    if not isinstance(properties, dict):
        raise TypeError(f"`properties` is a {type(properties).__name__}, not an object")
    alert = {column: properties.get(column) for column in db.WEATHER_ALERT_COLUMNS}
    for field in REQUIRED_PROPERTIES:
        if not alert[field]:
            raise ValueError(f"alert feature carries no {field}")
    return alert


def parse_alerts(payload) -> list[dict]:
    """Every feature in one `alerts/active` response, or raise.

    **One unreadable feature fails the whole poll**, deliberately, and this is
    the load-bearing choice in the module. Skipping the bad one and storing the
    rest would publish a short list under a *successful* poll clock — so a
    tornado warning whose shape we failed to read would be indistinguishable
    from a genuine all-clear at the endpoint. Failing the poll instead leaves
    `last_success_at` where it was, and the hub renders "we do not know", which
    is the true statement. It is also loud: the log names the feature and the
    condition persists until someone looks.
    """
    features = payload["features"]
    if not isinstance(features, list):
        raise TypeError(f"`features` is a {type(features).__name__}, not a list")
    return [_feature_to_alert(feature) for feature in features]


def _record(conn, attempted_at: str, succeeded: bool) -> str:
    """Stamp the poll clocks and return the poll's status.

    The write is guarded rather than trusted: `record_weather_alert_poll` is
    the last thing a poll does, and an `sqlite3.Error` escaping from here would
    unwind the `while` loop in `outdoor.main()` — the process-death shape #91
    exists to prevent, reached from a different direction.
    """
    try:
        db.record_weather_alert_poll(
            conn, attempted_at, attempted_at if succeeded else None
        )
    except sqlite3.Error as exc:
        log.warning("recording the alert poll clock failed: %s", exc)
        return "error"
    return "ok" if succeeded else "error"


def poll_once(conn, fetch) -> str:
    """One alert poll. Returns 'ok' or 'error'; never raises.

    Runs on the outdoor poller's existing timer and is deliberately *after* the
    weather row is written, so an NWS outage cannot cost a reading — the same
    separation `poll_once`'s `"partial"` status gives the air-quality half.
    """
    attempted_at = datetime.now(UTC).isoformat()
    try:
        alerts = parse_alerts(json.loads(fetch()))
    except POLL_FAILURES as exc:
        log.warning("NWS alert fetch unusable: %s: %s", type(exc).__name__, exc)
        return _record(conn, attempted_at, succeeded=False)
    try:
        db.upsert_weather_alerts(conn, alerts, attempted_at)
    except sqlite3.Error as exc:
        log.warning("storing %d NWS alert(s) failed: %s", len(alerts), exc)
        return _record(conn, attempted_at, succeeded=False)
    log.info("NWS alerts: %d active", len(alerts))
    return _record(conn, attempted_at, succeeded=True)


def _expiry(alert) -> datetime | None:
    """When this alert stops applying, or None when it does not say.

    `ends` first, `expires` second: NWS uses `expires` for the *message's* own
    validity and `ends` for the event's, and they differ on a long warning that
    is reissued. An unparseable or absent value yields None, which the caller
    reads as "keeps applying" — the safe direction, since the alternative is
    dropping a live warning over a date we could not read.
    """
    for field in ("ends", "expires"):
        value = alert.get(field)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            log.warning("alert %s has an unreadable %s (%r)", alert["id"], field, value)
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def active_alerts(conn, now) -> dict:
    """The `/api/weather-alerts` payload, minus the clock normalisation.

    `alerts` is **None until a poll has succeeded**, and that is the contract's
    whole point: an empty list means "we asked NWS and it said none", while
    None means "we have never had an answer". Collapsing those into `[]` is the
    false all-clear this module exists to prevent.

    A successful poll's own set is `last_seen_at == last_success_at` — NWS's
    active feed is authoritative, so the set it last reported *is* the set,
    with no active-ness arithmetic of ours in between. The expiry filter on top
    only matters once that poll goes stale: an hour after a successful poll, an
    alert that ended in the meantime should stop being published even though it
    was in the last answer we got.
    """
    state = db.weather_alert_poll_state(conn)
    if state["last_success_at"] is None:
        return {**state, "alerts": None}
    alerts = db.weather_alerts_seen_at(conn, state["last_success_at"])
    return {**state, "alerts": [a for a in alerts if _still_applies(a, now)]}


def _still_applies(alert, now) -> bool:
    """False only when this alert has a readable end time that has passed."""
    expiry = _expiry(alert)
    return expiry is None or expiry > now
