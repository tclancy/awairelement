"""The mini split feed: parsing, keying, and the metered-draw derivation (#139).

The one rule everything here serves: **a row's identity is the head's own
report-window close.** Our poll clock never becomes a key, because the device
reports on its own cadence (220 s and 800 s windows against the bridge's 300 s
cycle) and a poll-stamped row would make every re-read of one window a new
reading.
"""

import json
import os
import signal
import time
from datetime import datetime

import pytest

from awair import db, minisplit

RECEIVED = "2026-10-26T07:30:00+00:00"

# One `/status` body in the shape parsons-pulse #182 publishes: every draw key
# present whenever `head` is, so an absent key means "this bridge predates the
# fields" and a null one means "the head did not report it".
STATUS = {
    "target": {"value": 68.0, "source": "schedule"},
    "awair": {"temp_f": 66.9, "age_s": 44},
    "head": {
        "power": "on",
        "mode": "cool",
        "setpoint": 50.0,
        "temperature": 59.0,
        "watts": 30.0,
        "watts_at": "2026-10-26T07:24:34+00:00",
        "watts_from": "2026-10-26T07:11:14+00:00",
        "delta_wh": 10.0,
    },
}


def status(**head_overrides):
    """`STATUS` with `head` fields replaced; a value of `...` deletes the key."""
    head = dict(STATUS["head"])
    for key, value in head_overrides.items():
        if value is ...:
            head.pop(key)
        else:
            head[key] = value
    return {**STATUS, "head": head}


def fetcher(payload):
    """A `make_fetch`-shaped callable returning `payload` as a JSON body."""

    def fetch():
        return json.dumps(payload)

    return fetch


# -- keying -------------------------------------------------------------------


def test_the_row_is_keyed_on_the_report_window_close_not_the_poll_clock():
    reading = minisplit.parse_reading(STATUS, received_at=RECEIVED)
    assert reading["ts"] == "2026-10-26T07:24:34+00:00"
    assert reading["received_at"] == RECEIVED
    assert reading["window_start"] == "2026-10-26T07:11:14+00:00"


def test_a_window_close_is_normalised_to_utc_so_stored_keys_sort_together():
    """`ts` is compared lexicographically by SQL, so one spelling or no dedup.

    A head in a non-UTC offset is not hypothetical for a timestamp that
    originates at Samsung; two spellings of one instant would both insert and
    then sort against each other by text.
    """
    reading = minisplit.parse_reading(
        status(watts_at="2026-10-26T03:24:34-04:00"), received_at=RECEIVED
    )
    assert reading["ts"] == "2026-10-26T07:24:34+00:00"


@pytest.mark.parametrize(
    "watts_at",
    [
        None,
        ...,
        "2026-10-26T07:24:34",  # naive: an instant we cannot place
        "2026-10-26",  # date-only
        "2026-10-26+05:00",  # reads back NAIVE on 3.13 -- the offset parses as a time
        "20261026",
        "not a timestamp",
        1761463474,  # the device has been seen to send an integer stamp
    ],
)
def test_a_draw_this_bridge_cannot_date_is_unkeyed_and_stores_nothing(watts_at):
    assert minisplit.parse_reading(status(watts_at=watts_at), RECEIVED) is None


def test_a_null_head_is_unkeyed_but_a_payload_that_is_not_this_contract_is_not():
    """`head: null` is the bridge before its first cycle -- an ordinary state.

    A body with no `head` key, or one that is neither an object nor null, is a
    different endpoint or a broken bridge. Answering that with 'unkeyed' would
    hide a fault behind the status this feed sits in until #182 deploys, so it
    raises into `POLL_FAILURES` instead and `poll_once` calls it an error.
    """
    assert minisplit.parse_reading({**STATUS, "head": None}, RECEIVED) is None
    with pytest.raises(KeyError):
        minisplit.parse_reading({"target": {}}, RECEIVED)
    with pytest.raises(TypeError):
        minisplit.parse_reading({"head": "on"}, RECEIVED)


def test_a_window_with_a_close_but_no_energy_still_produces_a_row():
    """The close is what makes a reading identifiable; the energy is a value.

    This row is the half of #139 that works on the bridge deployed today: the
    head's own room temperature travels on it with no draw at all.
    """
    reading = minisplit.parse_reading(
        status(delta_wh=None, watts=None), received_at=RECEIVED
    )
    assert reading is not None
    assert reading["delta_wh"] is None
    assert reading["watts_spot"] is None
    assert reading["temp"] == 15.0


# -- values -------------------------------------------------------------------


def test_temperature_is_converted_to_celsius_at_ingest():
    """Every temperature column in this database is Celsius -- including this one.

    59 F is the reading measured on the dining head on 2026-10-26, against the
    Awair's 66.9 F in another room.
    """
    assert minisplit.parse_reading(STATUS, RECEIVED)["temp"] == 15.0


def test_the_spot_draw_and_the_metered_energy_are_both_stored_unmodified():
    """Neither is derivable from the other, so storage keeps the device's own.

    The spot reading sat at 30.0 W across three windows whose metered energy
    implied 45, 54 and 65 W.
    """
    reading = minisplit.parse_reading(STATUS, RECEIVED)
    assert reading["watts_spot"] == 30.0
    assert reading["delta_wh"] == 10.0


def test_the_switch_and_mode_travel_as_text():
    reading = minisplit.parse_reading(STATUS, RECEIVED)
    assert reading["switch"] == "on"
    assert reading["mode"] == "cool"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["temperature", "watts", "delta_wh"])
def test_a_non_finite_number_degrades_to_none_rather_than_reaching_the_driver(
    bad, field
):
    """The bridge finite-checks its draw fields and says `temperature` is not.

    `json.loads` accepts a bare `NaN`/`Infinity`, so without this guard an
    infinity would bind as a real and autoscale the whole chart axis to it.
    """
    reading = minisplit.parse_reading(status(**{field: bad}), RECEIVED)
    stored = {"temperature": "temp", "watts": "watts_spot", "delta_wh": "delta_wh"}
    assert reading[stored[field]] is None


@pytest.mark.parametrize(
    "field,column",
    [("temperature", "temp"), ("watts", "watts_spot"), ("delta_wh", "delta_wh")],
)
def test_a_non_numeric_value_degrades_to_none(field, column):
    assert minisplit.parse_reading(status(**{field: "warm"}), RECEIVED)[column] is None


@pytest.mark.parametrize("field", ["mode", "power"])
def test_a_non_text_state_degrades_to_none(field):
    column = {"mode": "mode", "power": "switch"}[field]
    assert minisplit.parse_reading(status(**{field: 3}), RECEIVED)[column] is None


def test_a_boolean_is_not_accepted_as_a_number():
    """`isinstance(True, int)` is True, and `True` watts would store as 1.0."""
    assert minisplit.parse_reading(status(watts=True), RECEIVED)["watts_spot"] is None


# -- the metered mean ---------------------------------------------------------


def moment(text):
    return datetime.fromisoformat(text)


def test_the_metered_mean_is_the_energy_over_the_windows_own_length():
    """The three windows measured on 2026-10-26, as the device reported them.

    Each pairs a constant 30.0 W spot reading with a different real draw, which
    is the whole argument for plotting this number instead of that one.
    """
    cases = [
        ("2026-10-26T06:46:24+00:00", "2026-10-26T06:59:44+00:00", 12.0, 54.0),
        ("2026-10-26T07:07:34+00:00", "2026-10-26T07:11:14+00:00", 4.0, 65.45),
        ("2026-10-26T07:11:14+00:00", "2026-10-26T07:24:34+00:00", 10.0, 45.0),
    ]
    for start, end, delta_wh, expected in cases:
        assert minisplit.metered_watts(delta_wh, moment(start), moment(end)) == expected


@pytest.mark.parametrize(
    "delta_wh,start,end",
    [
        (None, "2026-10-26T07:11:14+00:00", "2026-10-26T07:24:34+00:00"),
        (10.0, None, "2026-10-26T07:24:34+00:00"),
        (10.0, "2026-10-26T07:11:14+00:00", None),
        # A zero-length or inverted window: no length to divide by, and no
        # default is close enough to be harmless when the real ones differ by
        # a factor of nearly four.
        (10.0, "2026-10-26T07:24:34+00:00", "2026-10-26T07:24:34+00:00"),
        (10.0, "2026-10-26T07:30:00+00:00", "2026-10-26T07:24:34+00:00"),
    ],
)
def test_an_unmeasurable_window_has_no_mean_rather_than_a_fabricated_one(
    delta_wh, start, end
):
    assert (
        minisplit.metered_watts(
            delta_wh,
            None if start is None else moment(start),
            None if end is None else moment(end),
        )
        is None
    )


def test_zero_energy_over_a_real_window_is_zero_watts_not_unknown():
    """A head that drew nothing drew nothing -- this must not read as a gap."""
    assert (
        minisplit.metered_watts(
            0.0,
            moment("2026-10-26T07:11:14+00:00"),
            moment("2026-10-26T07:24:34+00:00"),
        )
        == 0.0
    )


# -- the poll loop ------------------------------------------------------------


def test_a_fresh_window_inserts_and_a_re_read_of_it_is_a_duplicate(conn):
    """Most polls are this: the bridge cycles faster than the device reports."""
    assert minisplit.poll_once(conn, fetcher(STATUS)) == "inserted"
    assert minisplit.poll_once(conn, fetcher(STATUS)) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 1


def test_a_later_window_inserts_beside_the_earlier_one(conn):
    assert minisplit.poll_once(conn, fetcher(STATUS)) == "inserted"
    later = status(
        watts_at="2026-10-26T07:37:54+00:00",
        watts_from="2026-10-26T07:24:34+00:00",
        delta_wh=14.0,
    )
    assert minisplit.poll_once(conn, fetcher(later)) == "inserted"
    stored = [row[0] for row in conn.execute("SELECT ts FROM minisplit_readings")]
    assert stored == ["2026-10-26T07:24:34+00:00", "2026-10-26T07:37:54+00:00"]


def test_an_undatable_payload_reports_unkeyed_not_error(conn):
    """The distinction is the whole status: 'unkeyed' is the expected state.

    Until parsons-pulse #182 is merged AND deployed, the bridge on the box
    publishes no draw fields, so every poll is legitimately unkeyed. Reporting
    that as an error would make a run of them look like a fault.
    """
    assert minisplit.poll_once(conn, fetcher(status(watts_at=...))) == "unkeyed"
    assert conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fetch",
    [
        lambda: (_ for _ in ()).throw(OSError("connection refused")),
        lambda: (_ for _ in ()).throw(TimeoutError("timed out")),
        lambda: "not json",
        lambda: None,  # json.loads(None) is a TypeError
        lambda: "[1, 2, 3]",  # a list has no `.get`
    ],
)
def test_one_bad_poll_costs_one_poll_and_never_the_service(conn, fetch):
    assert minisplit.poll_once(conn, fetch) == "error"
    # And the loop can carry straight on into a good payload.
    assert minisplit.poll_once(conn, fetcher(STATUS)) == "inserted"


def test_an_insert_failure_is_reported_rather_than_unwinding_the_loop(
    conn, monkeypatch
):
    """A `ProgrammingError` at bind time is an upstream payload fault.

    Raising would exit the process and systemd would restart it straight back
    into the same value, which is the crash loop #91 closed for the outdoor
    poller.
    """

    def boom(_conn, _reading):
        raise db.sqlite3.ProgrammingError("unsupported type")

    monkeypatch.setattr(db, "insert_minisplit_reading", boom)
    assert minisplit.poll_once(conn, fetcher(STATUS)) == "error"


def test_the_fetch_url_and_timeout_are_the_bridges_loopback_contract(monkeypatch):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        return _Resp()

    monkeypatch.setattr(minisplit.urllib.request, "urlopen", fake_urlopen)
    minisplit.make_fetch(minisplit.DEFAULT_STATUS_URL)()
    assert calls == [("http://127.0.0.1:8100/status", minisplit.FETCH_TIMEOUT_SECONDS)]


def test_the_poll_cadence_matches_the_bridges_control_cycle():
    """Polling faster buys nothing: `/status` republishes one window until the
    bridge's next cycle reads a new one."""
    assert minisplit.DEFAULT_POLL_SECONDS == 300


# -- the service entry point --------------------------------------------------


def test_main_polls_once_then_exits_cleanly_on_sigterm(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """A SIGTERM finishes the poll in flight and returns — it does not raise.

    The same contract #83 gave the other two pollers, and worth asserting here
    rather than assumed: this is also the only test that proves `main` reads
    the env names the README documents and reaches `poll_once` with a real
    connection. A typo in either is invisible to every test above.
    """
    db_path = tmp_path / "minisplit.db"
    monkeypatch.setenv("AWAIR_DB", str(db_path))
    monkeypatch.setenv("AWAIR_MINISPLIT_URL", "http://127.0.0.1:8100/status")
    monkeypatch.setenv("AWAIR_MINISPLIT_POLL_SECONDS", "900")

    def status_then_sigterm():
        os.kill(os.getpid(), signal.SIGTERM)
        return json.dumps(STATUS)

    monkeypatch.setattr(minisplit, "make_fetch", lambda url: status_then_sigterm)

    started = time.monotonic()
    minisplit.main()  # returns normally; must not raise
    # The interval is 900 s, so returning promptly proves the wait was cut
    # short by the handler rather than slept through.
    assert time.monotonic() - started < 10

    conn = db.connect(db_path)
    try:
        stored = conn.execute("SELECT ts FROM minisplit_readings").fetchall()
    finally:
        conn.close()
    assert stored == [("2026-10-26T07:24:34+00:00",)]


def test_main_reads_the_url_it_is_configured_with(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """`AWAIR_MINISPLIT_URL` has to reach `make_fetch`, not just be read.

    The default is loopback, so a `main` that ignored the override would still
    work on the bridge's box and fail on every other — including in whatever
    test environment someone points it at a fixture.
    """
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "minisplit.db"))
    monkeypatch.setenv("AWAIR_MINISPLIT_URL", "http://bridge.test:9999/status")
    monkeypatch.setenv("AWAIR_MINISPLIT_POLL_SECONDS", "900")
    urls = []

    def fake_make_fetch(url):
        urls.append(url)

        def fetch():
            os.kill(os.getpid(), signal.SIGTERM)
            return json.dumps(STATUS)

        return fetch

    monkeypatch.setattr(minisplit, "make_fetch", fake_make_fetch)
    minisplit.main()
    assert urls == ["http://bridge.test:9999/status"]
