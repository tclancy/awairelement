"""`GET /health` says whether each feed is still recording (#158).

The check this replaces was `GET /`, which could not fail on a dead poller:
it renders a template and queries nothing. So the properties under test here
are the ones that distinguish the new check from the old one, and they fail
differently:

* **A fresh database is 200.** Paired with a control
  (`test_the_fresh_fixture_records_every_feed`) because an "all fresh" fixture
  that quietly stopped seeding a feed would pass this while testing less than
  it claims -- and "less than it claims" for an all-fresh assertion is the
  vacuous direction.
* **One stale feed is 503, and the response names it.** Parametrized over
  every feed rather than asserted on one, because a route that checked three
  of five would pass a single-feed test and miss two pollers forever.
* **A feed with no rows at all is 503, and the response names it.** The case
  the dashboard check is *least* able to notice and the one that matters most
  on a fresh box: a poller that has never run.
* **The thresholds are derived, not restated.** Each feed's window moves when
  that feed's own `AWAIR_*_POLL_SECONDS` moves. Without this the four numbers
  are a coincidence that happens to match the pollers today.
* **Nothing but the database is touched.** A health check that calls a device
  cannot tell a dead feed from an unreachable one, and goes red in exactly the
  outage it exists to describe.
"""

import re
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import db, feeds
from awair.web import create_app

REPO = Path(__file__).resolve().parent.parent

# The env var that moves each feed's cadence, written out independently of
# `awair.feeds` on purpose: this file is the second statement of the mapping,
# and a test that asked the code under test which var to set would agree with
# any answer it gave. `test_every_feed_has_an_env_var_named_here` keeps the two
# lists the same length.
POLL_SECONDS_ENV = {
    "awair": "AWAIR_POLL_SECONDS",
    "basement": "AWAIR_BASEMENT_POLL_SECONDS",
    "outdoor": "AWAIR_OUTDOOR_POLL_SECONDS",
    "minisplit-dining": "AWAIR_MINISPLIT_POLL_SECONDS",
    "minisplit-bedroom": "AWAIR_MINISPLIT_POLL_SECONDS",
    "humidifier": "AWAIR_HUMIDIFIER_POLL_SECONDS",
}

#: The cadence each feed is probed with in the derivation test below, and the
#: humidifier is why this exists at all: its poller CLAMPS anything under five
#: minutes (`humidifier.MIN_POLL_SECONDS`, #161's "poll no faster than every 5
#: minutes"), so probing it with 7 would assert that the clamp is broken.
#:
#: A value per feed rather than one for all, and NOT read back from the code
#: under test: `poll_seconds()` would agree with any answer it gave, which is
#: the whole thing this test is for.
#: `tests/test_humidifier.py::test_a_cadence_under_the_floor_is_raised_to_it`
#: owns the clamp itself; `test_the_humidifiers_floor_reaches_health` below is
#: what pins that `/health` sees the clamped number rather than the asked-for
#: one.
POLL_SECONDS_PROBE = {name: 7 for name in POLL_SECONDS_ENV} | {"humidifier": 700}

FEED_IDS = [feed.name for feed in feeds.FEEDS]

#: The feeds whose absence is a fault, and the one whose absence is not.
#:
#: Every poller here talks to something on the LAN and needs no secret, so an
#: empty table can only mean it stopped -- except the humidifier's, which
#: needs a VeSync login this repo does not hold (#161). A box with no such
#: account must not fail its own deploy check, so a `Feed(optional=True)` with
#: no rows reads as "not set up" instead of stale. See `feeds.verdict`.
REQUIRED_FEEDS = [feed for feed in feeds.FEEDS if not feed.optional]
OPTIONAL_FEEDS = [feed for feed in feeds.FEEDS if feed.optional]
REQUIRED_IDS = [feed.name for feed in REQUIRED_FEEDS]
OPTIONAL_IDS = [feed.name for feed in OPTIONAL_FEEDS]


def _record(conn, feed, received_at):
    """Write one row for `feed`, stamped `received_at` by our clock.

    Dispatches on the table and **raises on an unknown one**, which is the
    point of writing it this way rather than as four fixtures: a fifth feed
    landing in a new table makes every fixture below fail loudly, instead of
    silently not seeding it and leaving the all-fresh assertion weaker than it
    reads.

    Only the NOT NULL columns plus one measurement are filled. `/health` reads
    `received_at` and nothing else, so a richer row would assert nothing extra.
    """
    stamp = received_at.isoformat()
    if feed.table == "readings":
        conn.execute(
            "INSERT INTO readings (ts, received_at, temp) VALUES (?, ?, ?)",
            (db.iso_z(received_at), stamp, 21.5),
        )
    elif feed.table == "basement_readings":
        conn.execute(
            "INSERT INTO basement_readings (ts, received_at, temp) VALUES (?, ?, ?)",
            (stamp, stamp, 18.0),
        )
    elif feed.table == "outdoor_readings":
        conn.execute(
            "INSERT INTO outdoor_readings (ts, received_at, temp) VALUES (?, ?, ?)",
            (stamp, stamp, 5.0),
        )
    elif feed.table == "minisplit_readings":
        conn.execute(
            "INSERT INTO minisplit_readings (head, ts, received_at, temp)"
            " VALUES (?, ?, ?, ?)",
            (feed.head, stamp, stamp, 20.0),
        )
    elif feed.table == "humidifier_readings":
        conn.execute(
            "INSERT INTO humidifier_readings (ts, received_at, humid) VALUES (?, ?, ?)",
            (stamp, stamp, 38.0),
        )
    else:
        raise AssertionError(
            f"no seeder for {feed.table!r} -- `awair.feeds.FEEDS` has gained a "
            "feed this file cannot write a row for, so every fixture here is "
            "now testing one feed fewer than it says it is"
        )


def _database(tmp_path, ages, name="awair.db"):
    """A bootstrapped database holding one row per feed in `ages`.

    `ages` maps a feed to how long ago its newest row was written. A feed
    absent from the mapping gets no row at all, which is the "never recorded"
    case.

    `name` is for the one test that needs two databases in one `tmp_path`:
    `db.connect` opens an existing file rather than replacing it, so a second
    call under the same name would quietly seed on top of the first.
    """
    path = tmp_path / name
    conn = db.connect(path)
    now = datetime.now(UTC)
    for feed, age in ages.items():
        _record(conn, feed, now - age)
    conn.commit()
    conn.close()
    return path


def _all_fresh(tmp_path, name="awair.db"):
    return _database(
        tmp_path, {feed: timedelta(seconds=1) for feed in feeds.FEEDS}, name
    )


def _get(path):
    response = create_app(str(path)).test_client().get("/health")
    return response.status_code, response.get_json()


def test_every_feed_has_an_env_var_named_here():
    """Control for the derivation tests: an unnamed feed would go untested."""
    assert set(POLL_SECONDS_ENV) == {feed.name for feed in feeds.FEEDS}


def test_the_fresh_fixture_records_every_feed(tmp_path):
    """Control for the 200 below: every feed must really have a row.

    Without this, a fixture that stopped seeding the bedroom head -- or a
    `FEEDS` entry pointing at a table `_record` writes nothing to -- would
    leave `test_every_feed_fresh_is_200` passing on a database that proves
    strictly less than it claims.
    """
    _, payload = _get(_all_fresh(tmp_path))

    assert [feed["name"] for feed in payload["feeds"]] == FEED_IDS
    for feed in payload["feeds"]:
        assert feed["newest_reading_at"] is not None, feed["name"]
        assert feed["age_seconds"] is not None, feed["name"]


def test_every_feed_fresh_is_200(tmp_path):
    status, payload = _get(_all_fresh(tmp_path))

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["stale"] == []


@pytest.mark.parametrize("stale_feed", feeds.FEEDS, ids=FEED_IDS)
def test_one_stale_feed_is_503_and_names_it(stale_feed, tmp_path):
    """Past its own window, by one second, with every sibling fresh."""
    ages = {feed: timedelta(seconds=1) for feed in feeds.FEEDS}
    ages[stale_feed] = timedelta(seconds=stale_feed.stale_after_seconds() + 1)

    status, payload = _get(_database(tmp_path, ages))

    assert status == 503
    assert payload["status"] == "stale"
    assert payload["stale"] == [stale_feed.name]
    entry = next(f for f in payload["feeds"] if f["name"] == stale_feed.name)
    assert entry["fresh"] is False
    assert entry["age_seconds"] > entry["stale_after_seconds"]


@pytest.mark.parametrize("missing_feed", REQUIRED_FEEDS, ids=REQUIRED_IDS)
def test_a_feed_with_no_rows_is_503_and_names_it(missing_feed, tmp_path):
    """An empty table is stale, not unknown, and not quietly skipped.

    This is the half `/api/latest`'s contract deliberately answers the other
    way round -- an empty `readings` table is a 200 there, because that
    endpoint feeds a hub drawing a card and "no sensor" is not an outage to
    it. Here the question is "is this feed recording", and the answer for a
    table with nothing in it is no.
    """
    ages = {
        feed: timedelta(seconds=1) for feed in feeds.FEEDS if feed is not missing_feed
    }

    status, payload = _get(_database(tmp_path, ages))

    assert status == 503
    assert payload["stale"] == [missing_feed.name]
    entry = next(f for f in payload["feeds"] if f["name"] == missing_feed.name)
    assert entry["newest_reading_at"] is None
    assert entry["age_seconds"] is None
    assert entry["fresh"] is False


def test_an_empty_database_names_every_feed(tmp_path):
    """A fresh box, before any poller has run: all of them, not just the first.

    "Every feed" means every REQUIRED feed since #161. The humidifier is in
    the other list on the same payload -- see the three tests below, which are
    what keep that from being a feed quietly dropped off the check.
    """
    status, payload = _get(_database(tmp_path, {}))

    assert status == 503
    assert payload["stale"] == REQUIRED_IDS
    assert payload["not_configured"] == OPTIONAL_IDS


@pytest.mark.parametrize("feed", OPTIONAL_FEEDS, ids=OPTIONAL_IDS)
def test_an_optional_feed_with_no_rows_is_not_configured_not_stale(feed, tmp_path):
    """The humidifier on a box with no VeSync account must not fail a deploy.

    This is the half of #161 that `/health` exists to get right, and the
    parametrized sibling above is its control: for every other feed an empty
    table is a 503, and the difference is `Feed.optional` rather than a name
    written into the route.
    """
    ages = {other: timedelta(seconds=1) for other in REQUIRED_FEEDS}

    status, payload = _get(_database(tmp_path, ages))

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["stale"] == []
    assert payload["not_configured"] == [feed.name]
    entry = next(f for f in payload["feeds"] if f["name"] == feed.name)
    assert entry["verdict"] == feeds.NOT_CONFIGURED
    assert entry["newest_reading_at"] is None


@pytest.mark.parametrize("feed", OPTIONAL_FEEDS, ids=OPTIONAL_IDS)
def test_an_optional_feed_that_has_recorded_and_stopped_is_stale(feed, tmp_path):
    """One row, ever, and this feed is held to everything the others are.

    The pair with the test above is the whole distinction #161 asked for:
    "never recorded" is not set up, "recorded and went quiet" stopped
    recording. Rows on disk are what tell them apart, which is why this test
    differs from that one by a single row.
    """
    ages = {other: timedelta(seconds=1) for other in REQUIRED_FEEDS}
    ages[feed] = timedelta(seconds=feed.stale_after_seconds() + 1)

    status, payload = _get(_database(tmp_path, ages))

    assert status == 503
    assert payload["stale"] == [feed.name]
    assert payload["not_configured"] == []
    entry = next(f for f in payload["feeds"] if f["name"] == feed.name)
    assert entry["verdict"] == feeds.STALE


def test_fresh_and_optional_are_not_the_same_word(tmp_path):
    """`fresh` still means recording, for anything written against #158.

    The payload grew a `verdict` and kept `fresh`, and this is what stops the
    two from drifting into disagreement: every entry's boolean is exactly
    "this feed's verdict is recording", including the optional one, whose
    `not_configured` is deliberately NOT fresh and deliberately not stale
    either.
    """
    ages = {other: timedelta(seconds=1) for other in REQUIRED_FEEDS}

    _, payload = _get(_database(tmp_path, ages))

    for entry in payload["feeds"]:
        assert entry["fresh"] == (entry["verdict"] == feeds.RECORDING), entry["name"]
    optional = [f["name"] for f in payload["feeds"] if f["optional"]]
    assert optional == OPTIONAL_IDS


def test_the_body_is_the_same_shape_on_200_and_503(tmp_path):
    """One payload for both codes, so a human reading `curl` output is told why."""
    fresh_status, fresh = _get(_all_fresh(tmp_path, "fresh.db"))
    stale_status, stale = _get(_database(tmp_path, {}, "stale.db"))

    assert (fresh_status, stale_status) == (200, 503)
    assert fresh.keys() == stale.keys()
    for left, right in zip(fresh["feeds"], stale["feeds"], strict=True):
        assert left.keys() == right.keys()


@pytest.mark.parametrize("feed", feeds.FEEDS, ids=FEED_IDS)
def test_each_window_follows_its_own_pollers_cadence(feed, tmp_path, monkeypatch):
    """Move the poller's interval; the feed's window has to move with it.

    The only way to tell a derivation from a coincidence while the cadences
    happen to be 30/60/300/900 -- `web._outdoor_carry_max_age_seconds`'s
    reason for being a function rather than a constant, applied to a route.
    A `/health` that restated the four numbers passes everything above and
    fails here.
    """
    probe = POLL_SECONDS_PROBE[feed.name]
    monkeypatch.setenv(POLL_SECONDS_ENV[feed.name], str(probe))
    expected = feeds.stale_after_polls() * probe

    _, payload = _get(_all_fresh(tmp_path))

    entry = next(f for f in payload["feeds"] if f["name"] == feed.name)
    assert entry["poll_seconds"] == probe
    assert entry["stale_after_seconds"] == expected


def test_the_humidifiers_floor_reaches_health(tmp_path, monkeypatch):
    """A cadence under the floor is reported as the floor, not as asked (#161).

    The companion to the derivation test above and the reason it needs a
    per-feed probe: this feed's cadence is clamped at five minutes, so
    `/health` has to judge it against the interval the poller will really use.
    Report the asked-for 7 s here and the window becomes 42 s for a feed that
    writes a row every 300 s -- permanently red, on a healthy humidifier.
    """
    monkeypatch.setenv("AWAIR_HUMIDIFIER_POLL_SECONDS", "7")

    status, payload = _get(_all_fresh(tmp_path))

    entry = next(f for f in payload["feeds"] if f["name"] == "humidifier")
    assert entry["poll_seconds"] == 300
    assert entry["stale_after_seconds"] == feeds.stale_after_polls() * 300
    assert (status, payload["stale"]) == (200, [])


def test_only_the_named_feeds_window_moves(tmp_path, monkeypatch):
    """Control for the test above: one env var must not move all four windows.

    A single shared cadence would satisfy every parametrized case separately
    and still be the "one number for all" this endpoint exists not to be.
    """
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "7")

    _, payload = _get(_all_fresh(tmp_path))

    windows = {f["name"]: f["stale_after_seconds"] for f in payload["feeds"]}
    assert windows["outdoor"] == feeds.stale_after_polls() * 7
    assert windows["awair"] == feeds.stale_after_polls() * 30


def test_the_missed_poll_count_is_tunable_without_a_deploy(tmp_path, monkeypatch):
    """`AWAIR_HEALTH_STALE_POLLS`, for `AWAIR_OUTDOOR_HEALTH_POLLS`'s reason."""
    monkeypatch.setenv("AWAIR_HEALTH_STALE_POLLS", "2")

    _, payload = _get(_all_fresh(tmp_path))

    windows = {f["name"]: f["stale_after_seconds"] for f in payload["feeds"]}
    assert windows["awair"] == 60
    assert windows["outdoor"] == 1800


def test_a_reading_from_the_future_is_stale_not_fresh(tmp_path):
    """A box whose clock came up behind real time must not read green forever.

    `monitor.adopt_health_run`'s bound, applied here: with a bare
    `age <= window` every ancient row on such a box is "newer than now" and
    every dead feed reports fresh, which is the one wrong answer this endpoint
    must never give.
    """
    ages = {feed: timedelta(seconds=1) for feed in feeds.FEEDS}
    future = feeds.FEEDS[0]
    ages[future] = -timedelta(seconds=future.stale_after_seconds() + 1)

    status, payload = _get(_database(tmp_path, ages))

    assert status == 503
    assert payload["stale"] == [future.name]


def test_a_reading_slightly_ahead_of_now_is_still_fresh(tmp_path):
    """Control for the bound above: ordinary NTP slew is not an outage."""
    ages = {feed: timedelta(seconds=1) for feed in feeds.FEEDS}
    ages[feeds.FEEDS[0]] = -timedelta(seconds=2)

    status, payload = _get(_database(tmp_path, ages))

    assert (status, payload["stale"]) == (200, [])


def test_health_makes_no_network_call(tmp_path, monkeypatch):
    """Database only. Every poller reaches its source through `urlopen`."""

    def refuse(*args, **kwargs):
        raise AssertionError("/health reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    status, _ = _get(_all_fresh(tmp_path))

    assert status == 200


def test_health_opens_a_read_only_connection(tmp_path, monkeypatch):
    """Not DDL, and not a writer -- the same contract as every sibling route.

    Also a reachability control for `test_health_makes_no_network_call`: a
    route that answered from nothing at all would satisfy that one identically.
    """
    opened = []
    real = db.connect_readonly
    monkeypatch.setattr(
        db, "connect_readonly", lambda path: (opened.append(path), real(path))[1]
    )
    path = _all_fresh(tmp_path)
    app = create_app(str(path))
    opened.clear()  # discard nothing; create_app uses connect(), not this

    assert app.test_client().get("/health").status_code == 200
    assert len(opened) == 1


def test_health_publishes_no_device_identifiers(tmp_path):
    """ADR-019: this route is unauthenticated, so the raw bridge status is out.

    `minisplit_readings.raw` carries SmartThings device ids, which is why
    `_BEDROOM_SERIES_COLUMNS` excludes it too. Asserted over the whole body
    rather than per field, so a column added to the payload later is covered.
    """
    path = _all_fresh(tmp_path)
    conn = db.connect(path)
    conn.execute(
        "UPDATE minisplit_readings SET raw = ?", ('{"deviceId": "secret-uuid"}',)
    )
    conn.commit()
    conn.close()

    response = create_app(str(path)).test_client().get("/health")

    assert b"secret-uuid" not in response.data
    assert b"deviceId" not in response.data


def test_newest_received_at_refuses_an_unknown_table(tmp_path):
    """The whitelist that keeps a table name out of an interpolated query."""
    conn = db.connect(tmp_path / "awair.db")
    try:
        with pytest.raises(ValueError, match="unknown reading table"):
            db.newest_received_at(conn, "readings; DROP TABLE readings")
    finally:
        conn.close()


def test_newest_received_at_prefers_arrival_to_the_source_clock(tmp_path):
    """A source clock stepping backwards must not read as a dead feed.

    The row that arrived *second* carries the smaller `ts`, so
    `ORDER BY ts DESC LIMIT 1` -- how every other `latest_*` query in `db`
    works, correctly, for its own question -- would answer with the earlier
    arrival and age the feed by an hour it was recording through.
    """
    conn = db.connect(tmp_path / "awair.db")
    try:
        conn.execute(
            "INSERT INTO readings (ts, received_at) VALUES (?, ?)",
            ("2026-11-20T12:00:00.000Z", "2026-11-20T12:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO readings (ts, received_at) VALUES (?, ?)",
            ("2026-11-20T11:00:00.000Z", "2026-11-20T12:00:30+00:00"),
        )
        conn.commit()

        newest = db.newest_received_at(conn, "readings")
    finally:
        conn.close()

    assert newest == datetime(2026, 11, 20, 12, 0, 30, tzinfo=UTC)


def test_newest_received_at_keeps_the_heads_apart(tmp_path):
    """One table, two feeds: a live dining head cannot cover a dead bedroom."""
    conn = db.connect(tmp_path / "awair.db")
    try:
        conn.execute(
            "INSERT INTO minisplit_readings (head, ts, received_at) VALUES (?, ?, ?)",
            ("dining", "2026-11-20T12:00:00+00:00", "2026-11-20T12:00:00+00:00"),
        )
        conn.commit()

        assert db.newest_received_at(conn, "minisplit_readings", "dining") is not None
        assert db.newest_received_at(conn, "minisplit_readings", "bedroom") is None
    finally:
        conn.close()


def test_a_naive_stored_stamp_is_read_as_utc(tmp_path):
    """A hand-edited or restored row must not 500 the endpoint.

    `web._iso_utc`'s rule, which this function has to share because it does the
    subtraction rather than only the formatting: naive means UTC here, since
    every writer in this app is. Reading it as *local* would be the silent
    failure -- a noon-UTC poll aged four hours in one direction or the other.
    """
    conn = db.connect(tmp_path / "awair.db")
    try:
        conn.execute(
            "INSERT INTO readings (ts, received_at) VALUES (?, ?)",
            ("2026-11-20T12:00:00.000Z", "2026-11-20T12:00:00"),
        )
        conn.commit()
        newest = db.newest_received_at(conn, "readings")
    finally:
        conn.close()

    assert newest == datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_the_head_list_comes_from_the_poller(tmp_path):
    """`FEEDS` must not carry its own copy of the mini split's head names.

    `minisplit.zones` is what decides what a `/status` body contains a head
    for; a head added there has to appear here without a second edit, which is
    the same rule `test_restart_script.py` holds `restart.sh` to.
    """
    from awair import minisplit

    expected = {f"minisplit-{head}" for head, _ in minisplit.zones({})}

    assert expected == {f.name for f in feeds.FEEDS if f.head is not None}
    assert expected  # control: an empty set would pass the line above


def test_every_unit_reads_the_same_environment_file():
    """What licenses the web process to resolve the pollers' cadences at all.

    `/health` runs in the web worker and reads `AWAIR_*_POLL_SECONDS` from its
    own environment. That is the *poller's* cadence only because all five units
    load one `EnvironmentFile`; split them and `/health` would judge each feed
    against a number nobody is polling at.

    These are the repo's dev reference copies -- the canonical units are
    templated by the homelab Ansible role -- so this pins the assumption where
    this repo can pin it, and names the assumption for whoever changes the
    template.
    """
    units = sorted((REPO / "systemd").glob("*.service"))
    assert len(units) >= 2, "no units to compare"

    files = {
        unit.name: re.findall(r"^EnvironmentFile=(.*)$", unit.read_text(), re.M)
        for unit in units
    }

    assert all(files.values()), f"a unit declares no EnvironmentFile: {files}"
    assert len({tuple(value) for value in files.values()}) == 1, files


def test_an_unopenable_database_fails_the_check(tmp_path):
    """500, like every sibling route, rather than a second meaning for 503.

    Both codes fail a deploy check, and keeping 503 to mean exactly "the feeds
    are not recording" is worth more than collapsing the two.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    app = create_app(str(blocker / "awair.db"))

    assert app.test_client().get("/health").status_code == 500
