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
    # The bridge cycle that produced this snapshot, and the row's key. Published
    # on `origin/main` as well as on #182's branch, which is what lets this feed
    # record anything at all before #182 deploys.
    "last_decision": {"at": "2026-10-26T07:25:00+00:00", "reason": "hold"},
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


def at_cycle(at):
    """`STATUS` with a different cycle clock -- the row's key."""
    return {**STATUS, "last_decision": {"at": at, "reason": "hold"}}


# The payload `parsons-pulse origin/main` serves: no draw fields at all, which
# is the state of the bridge on the box until #182 is merged AND deployed.
DEPLOYED_BRIDGE_STATUS = {
    "target": {"value": 68.0, "source": "schedule"},
    "awair": {"temp_f": 66.9, "age_s": 44},
    "last_decision": {"at": "2026-10-26T07:25:00+00:00", "reason": "hold"},
    "head": {"power": "on", "mode": "cool", "setpoint": 50.0, "temperature": 59.0},
}


def fetcher(payload):
    """A `make_fetch`-shaped callable returning `payload` as a JSON body."""

    def fetch():
        return json.dumps(payload)

    return fetch


# -- keying -------------------------------------------------------------------


def test_the_row_is_keyed_on_the_bridge_cycle_not_the_poll_clock():
    """One row per bridge cycle: its own clock, never ours.

    The window's bounds ride along as columns, because the two quantities move
    at different rates -- the head's temperature is read every cycle, a power
    report closes when the device feels like it.
    """
    reading = minisplit.parse_reading(STATUS, received_at=RECEIVED)
    assert reading["ts"] == "2026-10-26T07:25:00+00:00"
    assert reading["received_at"] == RECEIVED
    assert reading["window_start"] == "2026-10-26T07:11:14+00:00"
    assert reading["window_close"] == "2026-10-26T07:24:34+00:00"


def test_the_bridge_deployed_today_produces_a_row_with_a_temperature_and_no_draw():
    """The half of #139 that works before parsons-pulse #182 lands.

    This is the test the first draft of this feed could not pass: keyed on the
    window close, `DEPLOYED_BRIDGE_STATUS` stored nothing at all and the 59 F
    reading was discarded on every poll.
    """
    reading = minisplit.parse_reading(DEPLOYED_BRIDGE_STATUS, RECEIVED)
    assert reading["ts"] == "2026-10-26T07:25:00+00:00"
    assert reading["temp"] == 15.0
    assert reading["window_start"] is None
    assert reading["window_close"] is None
    assert reading["delta_wh"] is None
    assert reading["watts_spot"] is None


def test_a_fresher_temperature_is_not_suppressed_by_a_window_that_has_not_moved():
    """The whole reason the key is the cycle and not the window.

    The device's windows ran 220 s and 800 s against a 300 s cycle, so a window
    that has not closed again is the *normal* case -- and under the window key
    every one of these later readings was a duplicate and thrown away.
    """
    rows = [
        minisplit.parse_reading(
            {**at_cycle(at), "head": {**STATUS["head"], "temperature": temp_f}},
            RECEIVED,
        )
        for at, temp_f in [
            ("2026-10-26T07:25:00+00:00", 59.0),
            ("2026-10-26T07:30:00+00:00", 62.0),
            ("2026-10-26T07:35:00+00:00", 65.0),
        ]
    ]
    assert [r["ts"] for r in rows] == [
        "2026-10-26T07:25:00+00:00",
        "2026-10-26T07:30:00+00:00",
        "2026-10-26T07:35:00+00:00",
    ]
    assert [r["temp"] for r in rows] == [15.0, 16.67, 18.33]
    # All three carry the same unmoved window, which is the truth about it.
    assert {r["window_close"] for r in rows} == {"2026-10-26T07:24:34+00:00"}


def test_a_cycle_clock_is_normalised_to_utc_so_stored_keys_sort_together():
    """`ts` is compared lexicographically by SQL, so one spelling or no dedup."""
    reading = minisplit.parse_reading(
        at_cycle("2026-10-26T03:25:00-04:00"), received_at=RECEIVED
    )
    assert reading["ts"] == "2026-10-26T07:25:00+00:00"


@pytest.mark.parametrize(
    "at",
    [
        None,
        "2026-10-26T07:25:00",  # naive: an instant we cannot place
        "2026-10-26",  # date-only
        "2026-10-26+05:00",  # reads back NAIVE on 3.13 -- the offset parses as a time
        "20261026",
        "not a timestamp",
        1761463500,  # the device has been seen to send an integer stamp
    ],
)
def test_a_cycle_this_bridge_cannot_date_is_unkeyed_and_stores_nothing(at):
    assert minisplit.parse_reading(at_cycle(at), RECEIVED) is None


@pytest.mark.parametrize(
    "watts_at",
    [None, ..., "2026-10-26T07:24:34", "2026-10-26", "not a timestamp", 1761463474],
)
def test_an_undatable_window_costs_the_window_and_not_the_row(watts_at):
    """The reading is still a reading; only its draw loses its bounds."""
    reading = minisplit.parse_reading(status(watts_at=watts_at), RECEIVED)
    assert reading["ts"] == "2026-10-26T07:25:00+00:00"
    assert reading["window_close"] is None
    assert reading["temp"] == 15.0


def test_a_bridge_with_no_completed_cycle_is_unkeyed():
    """`head` and `last_decision` are both null until the first cycle lands.

    `status()` derives them from one attribute, so they are null together; both
    are read so a bridge that ever decouples them is unkeyed rather than
    half-stored.
    """
    assert (
        minisplit.parse_reading(
            {**STATUS, "head": None, "last_decision": None}, RECEIVED
        )
        is None
    )
    assert minisplit.parse_reading({**STATUS, "head": None}, RECEIVED) is None
    assert minisplit.parse_reading({**STATUS, "last_decision": None}, RECEIVED) is None


def test_a_payload_that_is_not_this_contract_raises_rather_than_reading_unkeyed():
    """Answering a broken bridge with the benign status would hide the fault."""
    with pytest.raises(KeyError):
        minisplit.parse_reading({"target": {}}, RECEIVED)
    with pytest.raises(KeyError):
        minisplit.parse_reading({"head": STATUS["head"]}, RECEIVED)
    with pytest.raises(TypeError):
        minisplit.parse_reading({**STATUS, "head": "on"}, RECEIVED)
    with pytest.raises(TypeError):
        minisplit.parse_reading({**STATUS, "last_decision": "07:25"}, RECEIVED)
    with pytest.raises(KeyError):
        minisplit.parse_reading(
            {**STATUS, "last_decision": {"reason": "hold"}}, RECEIVED
        )


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

    Each of these windows carried a 30.0 W spot reading, and this function
    never sees it -- that the spot value is the one kept off the chart is
    asserted end-to-end in `tests/test_web.py`'s
    `test_the_draw_on_the_chart_is_the_metered_mean_not_the_spot_reading`.
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
    assert minisplit.poll_once(conn, fetcher(STATUS))["dining"] == "inserted"
    assert minisplit.poll_once(conn, fetcher(STATUS))["dining"] == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 1


def test_a_later_cycle_inserts_beside_the_earlier_one(conn):
    assert minisplit.poll_once(conn, fetcher(STATUS))["dining"] == "inserted"
    later = at_cycle("2026-10-26T07:30:00+00:00")
    assert minisplit.poll_once(conn, fetcher(later))["dining"] == "inserted"
    stored = [row[0] for row in conn.execute("SELECT ts FROM minisplit_readings")]
    assert stored == ["2026-10-26T07:25:00+00:00", "2026-10-26T07:30:00+00:00"]


def test_a_bridge_with_no_cycle_yet_reports_unkeyed_not_error(conn):
    """'unkeyed' is a state to wait through -- a restart, or a 429 backoff."""
    no_cycle = {**STATUS, "head": None, "last_decision": None}
    assert minisplit.poll_once(conn, fetcher(no_cycle))["dining"] == "unkeyed"
    assert conn.execute("SELECT COUNT(*) FROM minisplit_readings").fetchone()[0] == 0


def test_the_deployed_bridge_stores_a_row_rather_than_reporting_unkeyed(conn):
    """The feed is useful before its dependency lands, not inert until then."""
    assert (
        minisplit.poll_once(conn, fetcher(DEPLOYED_BRIDGE_STATUS))["dining"]
        == "inserted"
    )
    stored = conn.execute(
        "SELECT ts, temp, delta_wh FROM minisplit_readings"
    ).fetchall()
    assert stored == [("2026-10-26T07:25:00+00:00", 15.0, None)]


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
    assert minisplit.poll_once(conn, fetch)["dining"] == "error"
    # And the loop can carry straight on into a good payload.
    assert minisplit.poll_once(conn, fetcher(STATUS))["dining"] == "inserted"


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
    assert minisplit.poll_once(conn, fetcher(STATUS))["dining"] == "error"


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
    assert stored == [("2026-10-26T07:25:00+00:00",)]


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


@pytest.mark.parametrize("delta_wh", [-0.01, -0.5, -1.0, -5.0])
def test_a_negative_energy_has_no_mean_rather_than_a_negative_draw(delta_wh):
    """A head cannot draw negative power: a rollover must not autoscale the axis.

    Parametrized down to the boundary on purpose. A single -5.0 left the
    guard's *threshold* unpinned — a mutation round widened `< 0` to `< -1`
    and the test stayed green while every draw between -1 and 0 Wh reached the
    chart (#139 review round 2). The zero case is the other side of the same
    boundary and has its own test above: 0.0 is a reading, not a refusal.

    Pinned to 0.01 Wh and no finer, deliberately. `< -0.001` still survives
    this round and the regress has no end — what the smallest case buys is a
    bound on the harm rather than an exact threshold, and a draw of -0.0005 Wh
    over a real window rounds to -0.0 W, which cannot move an axis.
    """
    assert (
        minisplit.metered_watts(
            delta_wh,
            moment("2026-10-26T07:11:14+00:00"),
            moment("2026-10-26T07:24:34+00:00"),
        )
        is None
    )


# -- both heads (the heads scope, 2026-10-31) ---------------------------------


def smartthings(setpoint_unit="F", humidity=30, at="2026-10-31T16:54:05.403Z"):
    """A cut-down `head.raw`: the attributes this feed reads, plus one it keeps."""
    return {
        "components": {
            "main": {
                "thermostatCoolingSetpoint": {
                    "coolingSetpoint": {
                        "value": 55,
                        "unit": setpoint_unit,
                        "timestamp": at,
                    }
                },
                "relativeHumidityMeasurement": {
                    "humidity": {"value": humidity, "unit": "%", "timestamp": at}
                },
            }
        }
    }


def zone(at="2026-10-31T17:01:03+00:00", target=55.0, raw=None, **head):
    return {
        "target": {"value": 99.0, "source": "schedule"},  # request-time: never stored
        "last_decision": {
            "at": at,
            "reason": "step",
            "target": {"value": target, "source": "schedule"},
        },
        "head": {
            "power": "on",
            "mode": "heat",
            "setpoint": 55.0,
            "temperature": 57.0,
            "temperature_at": "2026-10-31T14:38:20.870000+00:00",
            "raw": smartthings() if raw is None else raw,
            **head,
        },
    }


def body(bedroom=..., **dining):
    """A whole `/status`: dining at the top level, the bedroom under its key."""
    top = {**STATUS, **dining}
    top["bedroom"] = zone() if bedroom is ... else bedroom
    return top


def stored(conn, *columns):
    return conn.execute(
        f"SELECT head, {', '.join(columns)} FROM minisplit_readings ORDER BY head, ts"
    ).fetchall()


def test_one_poll_stores_a_row_for_each_head(conn):
    assert minisplit.poll_once(conn, fetcher(body())) == {
        "dining": "inserted",
        "bedroom": "inserted",
    }
    assert stored(conn, "ts", "temp") == [
        ("bedroom", "2026-10-31T17:01:03+00:00", 13.89),
        ("dining", "2026-10-26T07:25:00+00:00", 15.0),
    ]


@pytest.mark.parametrize("payload", [body(bedroom=None), STATUS])
def test_a_bedroom_zone_that_is_off_is_unkeyed_and_dining_still_stores(conn, payload):
    """`bedroom: null` (zone off) and no key at all (a pre-ADR-025 bridge)."""
    assert minisplit.poll_once(conn, fetcher(payload)) == {
        "dining": "inserted",
        "bedroom": "unkeyed",
    }


def test_a_broken_bedroom_block_costs_the_bedroom_row_and_not_dinings(conn):
    broken = {"last_decision": {"at": "2026-10-31T17:01:03+00:00"}, "head": "garbage"}
    assert minisplit.poll_once(conn, fetcher(body(bedroom=broken))) == {
        "dining": "inserted",
        "bedroom": "error",
    }


def test_a_failed_fetch_is_an_error_for_both_heads(conn):
    assert minisplit.poll_once(conn, lambda: "not json") == {
        "dining": "error",
        "bedroom": "error",
    }


def test_dinings_zone_block_never_carries_the_bedroom():
    [(_, dining), (_, bedroom)] = minisplit.zones(body())
    assert "bedroom" not in dining
    assert bedroom == zone()


def test_the_setpoint_is_stored_in_celsius_when_the_head_says_fahrenheit():
    row = minisplit.parse_reading(zone(), RECEIVED, "bedroom")
    assert row["setpoint"] == 12.78


@pytest.mark.parametrize("raw", [smartthings(setpoint_unit="C"), {}, "not an object"])
def test_a_setpoint_whose_unit_is_not_fahrenheit_or_unknown_is_null(raw):
    """The bridge does not gate the setpoint's unit, so this feed must."""
    assert (
        minisplit.parse_reading(zone(raw=raw), RECEIVED, "bedroom")["setpoint"] is None
    )


def test_the_target_is_the_cycles_own_not_the_request_time_one():
    row = minisplit.parse_reading(zone(target=64.0), RECEIVED, "bedroom")
    assert (row["target"], row["target_source"]) == (17.78, "schedule")
    assert row["decision_reason"] == "step"


def test_a_bridge_with_no_cycle_target_stores_null_rather_than_the_request_time_one():
    row = minisplit.parse_reading(STATUS, RECEIVED)
    assert (row["target"], row["target_source"]) == (None, None)


def test_the_temperature_stamp_is_normalised_to_utc():
    stamp = "2026-10-31T10:38:20-04:00"
    row = minisplit.parse_reading(zone(temperature_at=stamp), RECEIVED, "bedroom")
    assert row["temperature_at"] == "2026-10-31T14:38:20+00:00"


def test_raw_is_stored_when_it_changes_and_not_when_only_its_stamps_do(conn):
    def poll(at, raw):
        minisplit.poll_once(conn, fetcher(body(bedroom=zone(at=at, raw=raw))))

    poll("2026-10-31T17:00:00+00:00", smartthings(humidity=30))
    poll(
        "2026-10-31T17:05:00+00:00", smartthings(humidity=30, at="2026-10-31T17:04:00Z")
    )
    poll("2026-10-31T17:10:00+00:00", smartthings(humidity=29))
    rows = conn.execute(
        "SELECT raw FROM minisplit_readings WHERE head = 'bedroom' ORDER BY ts"
    ).fetchall()
    first, unchanged, changed = (row[0] for row in rows)
    assert json.loads(first) == smartthings(humidity=30)
    assert unchanged is None
    assert json.loads(changed) == smartthings(humidity=29)


def test_a_bridge_without_raw_stores_the_row_with_null_raw():
    """A rollback to a bridge predating `head.raw` must not cost the row."""
    old = zone()
    del old["head"]["raw"]
    row = minisplit.parse_reading(old, RECEIVED, "bedroom")
    assert row["raw"] is None and row["temp"] == 13.89
