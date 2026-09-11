"""NWS active alerts: storage, staleness, and what a failed poll must not look like.

The whole module exists to keep one thing true (#79): **a failed alerts poll
must never render as "no alerts."** Most of what follows is that sentence in
different positions — a fetch that fails, a payload that will not parse, a
database that will not write, and a poll that has never succeeded at all.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from awair import db, weather_alerts
from awair.weather_alerts import (
    DEFAULT_ALERTS_URL,
    active_alerts,
    build_url,
    make_fetch,
    parse_alerts,
    poll_once,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _feature(alert_id="urn:oid:2.49.0.1.840.0.abc.001.1", **properties):
    """One GeoJSON feature in the shape api.weather.gov actually returns.

    Both `id`s are present and *different* on purpose: the feature's own `id`
    is the api.weather.gov URL and `properties.id` is the CAP urn, and the
    storage key has to be the second one. A fixture carrying only one of them
    would make that assertion vacuous.
    """
    return {
        "id": f"https://api.weather.gov/alerts/{alert_id}",
        "type": "Feature",
        "properties": {
            "id": alert_id,
            "event": "Tornado Warning",
            "severity": "Extreme",
            "certainty": "Observed",
            "urgency": "Immediate",
            "headline": "Tornado Warning issued September 11 at 8:00AM EDT",
            "onset": "2026-09-11T08:00:00-04:00",
            "ends": "2026-09-11T09:00:00-04:00",
            "expires": "2026-09-11T09:00:00-04:00",
            **properties,
        },
    }


def _payload(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


def _fetcher(text):
    def fetch():
        return text

    return fetch


def _failing_fetcher(exc=None):
    """A fetcher that raises. The default stands in for "NWS is unreachable"."""
    failure = exc or OSError("connection refused")

    def fetch():
        raise failure

    return fetch


# --- the feed URL and the identifying User-Agent ----------------------------


def test_build_url_asks_for_the_parcel_point_not_a_whole_state():
    url = build_url(DEFAULT_ALERTS_URL, 42.36, -71.06)
    assert url.startswith(DEFAULT_ALERTS_URL + "?")
    assert "point=42.36%2C-71.06" in url
    # A state-wide feed would paint the card red for a warning 200 miles away.
    assert "area=" not in url


def test_the_fetcher_identifies_itself_to_nws(monkeypatch):
    """NWS policy: unidentified clients get rate-limited or blocked.

    Asserted on the outgoing `Request`, not on the helper's arguments — the
    default `Python-urllib/3.x` agent is what this exists to replace, and only
    the header proves it did.
    """
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["agent"] = request.get_header("User-agent")
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(weather_alerts.urllib.request, "urlopen", fake_urlopen)
    make_fetch("https://example.invalid/alerts", "awairelement (test)")()

    assert captured["agent"] == "awairelement (test)"
    assert captured["url"] == "https://example.invalid/alerts"


# --- parsing ---------------------------------------------------------------


def test_parse_keys_on_the_cap_urn_not_the_feature_url():
    (alert,) = parse_alerts(json.loads(_payload(_feature())))
    assert alert["id"] == "urn:oid:2.49.0.1.840.0.abc.001.1"
    assert not alert["id"].startswith("https://")
    assert alert["event"] == "Tornado Warning"
    assert alert["severity"] == "Extreme"
    assert alert["ends"] == "2026-09-11T09:00:00-04:00"


def test_an_empty_feature_collection_parses_to_an_empty_list():
    """The ordinary shape: nothing is happening. Distinct from a failure."""
    assert parse_alerts(json.loads(_payload())) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"features": "not a list"},
        {"features": [{"properties": {"id": "x"}}]},  # no `event`
        {"features": [{"properties": {"event": "Tornado Warning"}}]},  # no `id`
        {"features": [{"properties": {"id": "", "event": "Tornado Warning"}}]},
        {"features": [{"no_properties": True}]},
        # Present and NULL, which is the one that gets past `feature["properties"]`
        # and then raises `AttributeError` out of `.get` -- an exception class
        # neither failure tuple carries. Found by this test, not by review.
        {"features": [{"properties": None}]},
        {"features": [{"properties": "a string"}]},
        {},  # no `features` at all
    ],
)
def test_one_unreadable_feature_fails_the_whole_parse(payload):
    """Deliberate, and it is the load-bearing choice in the module.

    Storing the readable subset under a *successful* poll clock would make a
    tornado warning we failed to read indistinguishable from a genuine
    all-clear at the endpoint. Failing leaves `last_success_at` where it was.
    """
    with pytest.raises(weather_alerts.POLL_FAILURES):
        parse_alerts(payload)


# --- polling ---------------------------------------------------------------


def test_a_successful_poll_stores_the_alerts_and_stamps_both_clocks(conn):
    assert poll_once(conn, _fetcher(_payload(_feature()))) == "ok"
    state = db.weather_alert_poll_state(conn)
    assert state["last_attempt_at"] is not None
    assert state["last_success_at"] == state["last_attempt_at"]
    feed = active_alerts(conn, NOW)
    assert [a["event"] for a in feed["alerts"]] == ["Tornado Warning"]


def test_a_successful_empty_poll_is_a_real_all_clear(conn):
    """`[]`, not None: we asked and NWS said none. The other half of the contract."""
    assert poll_once(conn, _fetcher(_payload())) == "ok"
    assert active_alerts(conn, NOW)["alerts"] == []


def test_alerts_is_none_until_a_poll_has_ever_succeeded(conn):
    """The first and worst case: a fresh install whose NWS fetch has never worked.

    An empty table is indistinguishable from an all-clear unless something says
    so, and `None` is that something.
    """
    assert active_alerts(conn, NOW)["alerts"] is None
    assert poll_once(conn, _failing_fetcher()) == "error"
    feed = active_alerts(conn, NOW)
    assert feed["alerts"] is None
    assert feed["last_attempt_at"] is not None, "the poller did try"
    assert feed["last_success_at"] is None


def test_a_failed_poll_after_a_good_one_keeps_publishing_the_last_known_set(conn):
    """A transient NWS outage must not delete a live tornado warning.

    Nothing in this module deletes, so the previous successful poll's set keeps
    being published — and `last_success_at` stops advancing while
    `last_attempt_at` does, which is how the consumer sees it going stale.
    """
    poll_once(conn, _fetcher(_payload(_feature())))
    good = db.weather_alert_poll_state(conn)["last_success_at"]

    assert poll_once(conn, _failing_fetcher()) == "error"

    feed = active_alerts(conn, NOW)
    assert [a["event"] for a in feed["alerts"]] == ["Tornado Warning"]
    assert feed["last_success_at"] == good, "a failure must not move the success clock"
    assert feed["last_attempt_at"] > good, "but it must move the attempt clock"


def test_an_alert_nws_stops_reporting_drops_out_of_the_feed(conn):
    """Cancellation. The active set is the last successful poll's, not a union."""
    poll_once(conn, _fetcher(_payload(_feature())))
    poll_once(conn, _fetcher(_payload()))
    assert active_alerts(conn, NOW)["alerts"] == []
    # ...and the row survives: this is a durable record, not a cache.
    stored = conn.execute("SELECT COUNT(*) FROM weather_alerts").fetchone()[0]
    assert stored == 1


def test_an_alert_that_has_ended_stops_being_published_even_before_the_next_poll(conn):
    """The expiry filter only matters once a successful poll goes stale.

    An hour after the last answer we got, an alert that ended in the meantime
    should stop being published — the poll clock alone cannot express that.
    """
    poll_once(conn, _fetcher(_payload(_feature())))
    during = datetime(2026, 9, 11, 12, 30, tzinfo=UTC)  # 08:30 EDT, mid-warning
    after = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)  # 09:30 EDT, past `ends`
    assert len(active_alerts(conn, during)["alerts"]) == 1
    assert active_alerts(conn, after)["alerts"] == []


def test_an_alert_with_no_end_time_keeps_applying(conn):
    """The safe direction: never drop a live warning over a date we cannot read."""
    poll_once(conn, _fetcher(_payload(_feature(ends=None, expires=None))))
    far_future = NOW + timedelta(days=30)
    assert len(active_alerts(conn, far_future)["alerts"]) == 1


def test_ends_beats_expires_when_the_two_disagree(conn):
    """`expires` is the message's validity; `ends` is the event's.

    A long warning is reissued, so `expires` passes while `ends` has not — and
    reading the wrong one drops a live warning an hour early.
    """
    poll_once(
        conn,
        _fetcher(
            _payload(
                _feature(
                    ends="2026-09-11T18:00:00-04:00",
                    expires="2026-09-11T09:00:00-04:00",
                )
            )
        ),
    )
    after_expires = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)
    assert len(active_alerts(conn, after_expires)["alerts"]) == 1


def test_an_unreadable_end_time_keeps_the_alert_rather_than_dropping_it(conn):
    poll_once(conn, _fetcher(_payload(_feature(ends="soonish", expires=None))))
    assert len(active_alerts(conn, NOW + timedelta(days=3))["alerts"]) == 1


def test_a_malformed_payload_is_one_poll_not_the_process(conn):
    """Same contract as `outdoor.poll_once`: never raise out of the loop."""
    assert poll_once(conn, _fetcher("not json at all")) == "error"
    assert poll_once(conn, _fetcher(_payload({"properties": None}))) == "error"
    assert db.weather_alert_poll_state(conn)["last_success_at"] is None


def test_a_database_failure_during_the_store_is_reported_not_raised(conn, monkeypatch):
    """An `sqlite3.Error` here would unwind `outdoor.main()`'s `while` loop.

    Same process-death shape as #91, reached from the storage side instead of
    the payload side.
    """

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "upsert_weather_alerts", boom)
    assert poll_once(conn, _fetcher(_payload(_feature()))) == "error"
    assert db.weather_alert_poll_state(conn)["last_success_at"] is None


def test_a_database_failure_stamping_the_clock_is_reported_not_raised(
    conn, monkeypatch
):
    """The last write a poll makes is still a write, and it can still fail."""

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "record_weather_alert_poll", boom)
    assert poll_once(conn, _fetcher(_payload())) == "error"


# --- the durable record ----------------------------------------------------


def test_a_persisting_alert_keeps_its_first_seen_and_refreshes_its_end_time(conn):
    """NWS revises a live alert in place; `ends` in particular gets extended.

    `first_seen_at` is the fact the durable record exists to hold, so it is the
    one field the upsert must not touch.
    """
    poll_once(conn, _fetcher(_payload(_feature())))
    first = conn.execute("SELECT first_seen_at, ends FROM weather_alerts").fetchone()

    poll_once(conn, _fetcher(_payload(_feature(ends="2026-09-11T11:00:00-04:00"))))
    second = conn.execute(
        "SELECT first_seen_at, ends, last_seen_at FROM weather_alerts"
    ).fetchone()

    assert conn.execute("SELECT COUNT(*) FROM weather_alerts").fetchone()[0] == 1
    assert second[0] == first[0], "first_seen_at must survive the revision"
    assert second[1] == "2026-09-11T11:00:00-04:00", "ends must be refreshed"
    assert second[2] > first[0]


def test_two_concurrent_alerts_both_survive(conn):
    poll_once(
        conn,
        _fetcher(
            _payload(
                _feature(
                    "urn:a", event="Tornado Warning", onset="2026-09-11T08:00:00Z"
                ),
                _feature("urn:b", event="Flood Watch", onset="2026-09-11T07:00:00Z"),
            )
        ),
    )
    feed = active_alerts(conn, NOW)
    # Ordered by onset, so the Flood Watch (07:00) comes first — stable and
    # chronological, deliberately not ranked by severity. Ranking is the hub's.
    assert [a["event"] for a in feed["alerts"]] == ["Flood Watch", "Tornado Warning"]
