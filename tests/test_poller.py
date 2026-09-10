"""Parsing device JSON and single poll iterations."""

import http.client
import json
import os
import signal
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

import pytest

from awair import poller
from awair.monitor import DeviceHealth
from awair.poller import (
    handle_device_health,
    make_fetch,
    parse_reading,
    poll_once,
)

FIXTURE_TEXT = (Path(__file__).parent / "fixtures" / "air_data_latest.json").read_text()
FIXTURE = json.loads(FIXTURE_TEXT)

RECEIVED = "2026-07-11T01:24:20+00:00"


def test_parse_reading_maps_device_fields():
    reading = parse_reading(FIXTURE, received_at=RECEIVED)
    assert reading["ts"] == "2026-07-11T01:24:22.662Z"
    assert reading["received_at"] == RECEIVED
    assert reading["score"] == 83
    assert reading["temp"] == 24.45
    assert reading["humid"] == 64.67
    assert reading["abs_humid"] == 14.40
    assert reading["dew_point"] == 17.36
    assert reading["co2"] == 435
    assert reading["co2_est"] == 400
    assert reading["co2_est_baseline"] == 37731
    assert reading["voc"] == 267
    assert reading["voc_baseline"] == 40869
    assert reading["voc_h2_raw"] == 27
    assert reading["voc_ethanol_raw"] == 39
    assert reading["pm25"] == 7
    assert reading["pm10_est"] == 8


def test_parse_reading_tolerates_missing_sensor_field():
    payload = dict(FIXTURE)
    del payload["pm10_est"]
    reading = parse_reading(payload, received_at=RECEIVED)
    assert reading["pm10_est"] is None


def test_parse_reading_requires_device_timestamp():
    payload = dict(FIXTURE)
    del payload["timestamp"]
    with pytest.raises(KeyError):
        parse_reading(payload, received_at=RECEIVED)


def test_poll_once_inserts_fresh_reading(conn):
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_poll_once_reports_duplicate_device_ts(conn):
    poll_once(conn, fetch=lambda: FIXTURE_TEXT)
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


def test_poll_once_reports_fetch_error_without_inserting(conn):
    def failing_fetch():
        raise URLError("device unreachable")

    assert poll_once(conn, fetch=failing_fetch) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_poll_once_reports_bad_json_as_error(conn):
    assert poll_once(conn, fetch=lambda: "<html>not json</html>") == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


# Four payload shapes the device could publish that `parse_reading` indexes
# into as if it were a dict. Every one of them raised TypeError past
# `poll_once`'s old `(OSError, ValueError, KeyError)` and unwound `main()` --
# reproduced on this branch before the fix, not inferred (#95).
NON_OBJECT_PAYLOADS = [
    pytest.param("[]", id="json-list"),
    pytest.param('"hello"', id="json-string"),
    pytest.param("42", id="json-number"),
    pytest.param(None, id="fetcher-returned-none"),
]


@pytest.mark.parametrize("body", NON_OBJECT_PAYLOADS)
def test_poll_once_survives_a_payload_that_is_not_an_object(conn, body):
    """A bad upstream payload costs one poll, never the service.

    `parse_reading` subscripts the payload, so a list, a string or a number
    raises TypeError rather than KeyError; `json.loads(None)` raises TypeError
    too, which is the shape any `make_fetch` replacement missing a `return`
    produces. `main()` has no try around `poll_once`, so before #95 all four
    exited the process and systemd restarted straight back into the same
    value.
    """
    assert poll_once(conn, fetch=lambda: body) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_parse_reading_rejects_a_null_device_timestamp():
    """A null `ts` is a bad reading, not a reading with a blank field.

    `readings.ts` is `TEXT NOT NULL` and the old `INSERT OR IGNORE` ignored
    that violation exactly as it ignored a uniqueness one -- so handing SQLite
    a null reported `"duplicate"` and stored nothing. Rejecting it here is
    what turns a silent discard into a logged `"error"`.
    """
    payload = dict(FIXTURE, timestamp=None)
    with pytest.raises(ValueError):
        parse_reading(payload, received_at=RECEIVED)


@pytest.mark.parametrize("bad_ts", [None, "", 0])
def test_poll_once_reports_a_bad_timestamp_as_error_not_duplicate(conn, bad_ts):
    """The distinction the whole issue turns on.

    `"duplicate"` means the device republished a reading we already hold, and
    it is the one status that is *fine*. Reporting a rejected payload as a
    duplicate made "the device is quiet" and "ingestion has been dead for a
    week" the same line in the log.
    """
    fetch = lambda: json.dumps(dict(FIXTURE, timestamp=bad_ts))  # noqa: E731
    assert poll_once(conn, fetch=fetch) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_a_bad_timestamp_does_not_look_like_a_quiet_device_to_alerting(conn):
    """Why this is a bug and not a tidiness complaint (#95).

    `check_metrics` only runs on `"inserted"`, so for as long as the device
    publishes a bad timestamp no CO2/VOC/PM2.5 spike can open an event or fire
    an ntfy. The issue said the DeviceHealth path does not cover this at all;
    it does, but only in the sustained case -- ten consecutive errors raise
    `"unreachable"`, where before the fix the same polls counted as duplicates
    and raised `"stale"`. **Interleaved** with good readings it never fires,
    because any insert resets the counter, and that is the genuinely silent
    shape: half the readings vanish and nothing anywhere says so.

    Both halves are pinned. The fix does not change DeviceHealth's arithmetic
    -- it changes which bucket a bad payload lands in, from `"duplicate"`
    ("the device is wedged") to `"error"` ("the fetch produced nothing
    usable"), which is the honest one.
    """
    bad = lambda: json.dumps(dict(FIXTURE, timestamp=None))  # noqa: E731
    health = DeviceHealth()
    sustained = [health.observe(poll_once(conn, fetch=bad)) for _ in range(12)]
    assert "unreachable" in sustained, sustained
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_an_intermittent_bad_timestamp_is_the_silent_shape(conn):
    """The case DeviceHealth genuinely cannot see, before OR after the fix.

    Alternating good and bad payloads keeps both counters below the alert
    threshold forever, so no event opens either way. What the fix buys here is
    not an alert -- it is that every discarded poll now reports `"error"` and
    logs the reason, where before it reported `"duplicate"` and looked exactly
    like a device that had nothing new to say.

    The no-alert half of that is true before AND after the fix, so asserting
    only it would leave this test passing on a full revert. The status
    sequence is the binding assertion.
    """

    def bad():
        return json.dumps(dict(FIXTURE, timestamp=None))

    def good(minute):
        return lambda: json.dumps(
            dict(FIXTURE, timestamp=f"2026-09-09T10:{minute:02d}:00Z")
        )

    health = DeviceHealth()
    statuses, verdicts = [], []
    for i in range(60):
        fetch = bad if i % 2 else good(i)
        status = poll_once(conn, fetch=fetch)
        statuses.append(status)
        verdicts.append(health.observe(status))

    # The half that binds: pre-fix every odd poll came back "duplicate".
    assert statuses[1::2] == ["error"] * 30, statuses
    assert statuses[0::2] == ["inserted"] * 30, statuses
    assert [v for v in verdicts if v] == [], (
        "no alert fires -- this is the silent shape"
    )
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 30


# A sensor field the device publishes as a nested object or array. `ts` is
# validated but these 14 are handed to the driver unchecked, so they bind-fail
# as sqlite3.ProgrammingError -- which is why POLL_FAILURES names sqlite3.Error
# and not sqlite3.IntegrityError. This is the natural caller; the monkeypatched
# test below is the unnatural one.
NON_SCALAR_SENSOR_FIELDS = [
    pytest.param({"co2": {"value": 900}}, id="co2-is-an-object"),
    pytest.param({"co2": [900]}, id="co2-is-a-list"),
    pytest.param({"score": {"nested": {"deep": 1}}}, id="score-is-nested"),
]


@pytest.mark.parametrize("override", NON_SCALAR_SENSOR_FIELDS)
def test_poll_once_survives_a_non_scalar_sensor_field(conn, override):
    """The contract is "one poll, never the service" -- for every payload.

    `parse_reading` gates `ts` and then does `payload.get(field)` for all 14
    sensor fields with no shape check, so a nested object reaches
    `conn.execute` and raises `sqlite3.ProgrammingError` at bind time. That is
    a real shape from a firmware change, and it is the reason `POLL_FAILURES`
    widened to `sqlite3.Error` rather than stopping at `IntegrityError`.
    """
    body = json.dumps(dict(FIXTURE, **override))
    assert poll_once(conn, fetch=lambda: body) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_a_failed_insert_does_not_leave_the_write_lock_held(conn):
    """Costing one poll is the point; costing the write lock is a worse bug.

    sqlite3 opens an implicit transaction before an INSERT, and a statement
    that raises does not resolve it -- so without the rollback in
    `insert_reading` the connection holds the write lock until some later poll
    commits, which if the device is stuck on a bad value is never.
    """
    body = json.dumps(dict(FIXTURE, co2={"value": 900}))
    assert poll_once(conn, fetch=lambda: body) == "error"
    assert not conn.in_transaction

    # And the next good poll still works, rather than meeting its own lock.
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "inserted"


def test_poll_once_survives_a_truncated_response_from_the_device(conn):
    """IncompleteRead is not an OSError, so it escaped the fetch guard.

    `http.client` exceptions inherit from `Exception`, not `OSError`, and
    urllib does not convert what `getresponse()`/`read()` raise -- so a
    truncated response over a flaky LAN exited the process.
    """

    def truncated():
        raise http.client.IncompleteRead(b'{"timestamp"')

    assert poll_once(conn, fetch=truncated) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_poll_once_reports_a_db_error_from_the_insert_as_error(conn, monkeypatch):
    """The belt to `parse_reading`'s braces, pinned rather than assumed.

    `db.insert_reading` can raise now that it names its conflict target instead
    of swallowing every constraint violation. Nothing reaches THIS branch today
    -- `parse_reading` rejects the only bad `ts` shape and `received_at` is our
    own clock -- so it is asserted with a monkeypatched insert. A later schema
    NOT NULL, or a `parse_reading` that stops validating, would otherwise turn
    a discarded poll back into a dead process, which is the bug #95 exists to
    fix.
    """

    def raising_insert(*_args, **_kwargs):
        raise sqlite3.IntegrityError("NOT NULL constraint failed: readings.ts")

    monkeypatch.setattr(poller.db, "insert_reading", raising_insert)
    assert poll_once(conn, fetch=lambda: FIXTURE_TEXT) == "error"
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0


def test_main_with_test_flag_runs_fan_test_and_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "test.db"))
    ran = []
    monkeypatch.setattr(poller, "run_fan_test", lambda *a, **k: ran.append(a))
    poller.main(["--test"])  # must return instead of entering the poll loop
    assert len(ran) == 1


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_make_fetch_returns_decoded_body(monkeypatch):
    """The closure returned by make_fetch decodes the urlopen response body."""
    seen = {}

    def fake_urlopen(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return _FakeResponse("hello")

    monkeypatch.setattr(poller.urllib.request, "urlopen", fake_urlopen)
    fetch = make_fetch("http://awair.local/air-data/latest")
    assert fetch() == "hello"
    assert seen["url"] == "http://awair.local/air-data/latest"
    assert seen["timeout"] == poller.FETCH_TIMEOUT_SECONDS


class _RecordingNotifier:
    """Stand-in for alerts.Notifier that captures send() calls."""

    def __init__(self, return_value=True):
        self.calls = []
        self.return_value = return_value

    def send(self, message, title="", priority="default"):
        self.calls.append({"message": message, "title": title, "priority": priority})
        return self.return_value


def _now():
    return datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_handle_device_health_no_verdict_noops(conn):
    """One 'error' below threshold produces no verdict → no notify, no event."""
    from awair.monitor import DeviceHealth

    notifier = _RecordingNotifier()
    handle_device_health(conn, notifier, DeviceHealth(), status="error", now=_now())
    assert notifier.calls == []
    from awair import db

    assert db.get_open_events(conn) == {}


def test_handle_device_health_unreachable_opens_event(conn):
    """`threshold` consecutive errors trip unreachable → notify + open_event."""
    from awair import db
    from awair.monitor import DeviceHealth

    notifier = _RecordingNotifier(return_value=True)
    health = DeviceHealth(threshold=3)
    for _ in range(2):
        handle_device_health(conn, notifier, health, "error", _now())
    assert notifier.calls == []
    handle_device_health(conn, notifier, health, "error", _now())
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["priority"] == "high"
    assert call["title"] == "Awair device unreachable"
    event = db.get_open_events(conn)["device"]
    assert event["tier"] == "unreachable"


def test_handle_device_health_stale_opens_event(conn):
    """Same shape, `duplicate` path — the wedged-but-serving failure mode."""
    from awair import db
    from awair.monitor import DeviceHealth

    notifier = _RecordingNotifier()
    health = DeviceHealth(threshold=2)
    handle_device_health(conn, notifier, health, "duplicate", _now())
    handle_device_health(conn, notifier, health, "duplicate", _now())
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["title"] == "Awair device stale"
    assert db.get_open_events(conn)["device"]["tier"] == "stale"


def test_handle_device_health_recovered_closes_open_event(conn):
    """A fresh insert after alerting closes the row and sends the recovery notice."""
    from awair import db
    from awair.monitor import DeviceHealth

    notifier = _RecordingNotifier()
    health = DeviceHealth(threshold=1)
    handle_device_health(conn, notifier, health, "error", _now())
    assert "device" in db.get_open_events(conn)
    # Now an insert flips the health verdict to 'recovered'.
    handle_device_health(conn, notifier, health, "inserted", _now())
    assert len(notifier.calls) == 2  # open + recovered
    assert notifier.calls[1]["title"] == "Awair device recovered"
    # The alert row is closed.
    assert "device" not in db.get_open_events(conn)


def test_handle_device_health_recovered_without_prior_event_still_notifies(conn):
    """DeviceHealth is the source of truth for 'recovered' — no DB row required.

    A recovered verdict with no matching open row (e.g. DB pruned) still
    sends the notification; the close_event branch is skipped gracefully.
    """
    from awair.monitor import DeviceHealth

    notifier = _RecordingNotifier()
    health = DeviceHealth(threshold=1)
    handle_device_health(conn, notifier, health, "error", _now())
    # Wipe the event to simulate a stray recovery.
    conn.execute("DELETE FROM alert_events")
    conn.commit()
    handle_device_health(conn, notifier, health, "inserted", _now())
    assert notifier.calls[-1]["title"] == "Awair device recovered"


def test_startup_banner_says_disabled_in_code_not_merely_off(monkeypatch):
    """The banner carries a third state, not just on/off.

    The warning in `config_from_env` only fires while the environment still
    asks for fans — so once the Ansible flag agrees, this line is the only
    thing in the logs distinguishing a code-level kill switch (fix: the
    constant in `awair.fans`) from someone having simply left mitigation
    switched off (fix: the Ansible variable). Kept through ADR-002, which turned
    mitigation back on but not the kill switch off.
    """
    from awair import fans

    off = fans.FansConfig(enabled=False, fan_host="h", fan_ids=(1,))
    on = fans.FansConfig(enabled=True, fan_host="h", fan_ids=(1,))

    monkeypatch.setattr(fans, "MITIGATION_RETIRED", True)
    assert poller._fan_mitigation_status(off) == "disabled in code"

    monkeypatch.setattr(fans, "MITIGATION_RETIRED", False)
    assert poller._fan_mitigation_status(off) == "off"
    assert poller._fan_mitigation_status(on) == "on"


def test_main_polls_once_then_exits_cleanly_on_sigterm(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """A SIGTERM finishes the poll in flight and returns — it does not raise (#83).

    This test used to break the loop by making `time.sleep` raise, which
    asserted the exact behaviour #83 exists to remove: an exception escaping
    `main()` IS the non-zero exit systemd reports as
    `Failed with result 'exit-code'` on every restart. Delivering the real
    signal and requiring a normal return is the difference between the two.
    """
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "poller.db"))
    monkeypatch.setenv("AWAIR_POLL_SECONDS", "30")
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")

    def fetch_then_sigterm():
        os.kill(os.getpid(), signal.SIGTERM)
        return FIXTURE_TEXT

    monkeypatch.setattr(poller, "make_fetch", lambda url: fetch_then_sigterm)
    monkeypatch.setattr(poller, "check_metrics", lambda *a, **k: None)
    monkeypatch.setattr(poller, "check_fans", lambda *a, **k: None)

    started = time.monotonic()
    poller.main([])  # returns normally; must not raise
    # The interval is 30 s. Returning promptly proves the wait was interrupted
    # rather than slept through — the reason this is an Event, not a flag.
    assert time.monotonic() - started < 10

    # The reading landed — proves poll_once ran with the real connection, and
    # that the signal did not abandon the poll half-done.
    from awair import db

    conn = db.connect(str(tmp_path / "poller.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
    finally:
        conn.close()


def test_main_survives_a_payload_that_used_to_kill_the_process(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """`poll_once` returning "error" is worthless if the loop above it unwound.

    This is the assertion #95 asks for by name, and it is the one the
    unit-level tests cannot make: before the fix a non-object payload raised
    TypeError straight out of `poll_once`, past a `main()` that has no
    `except` of its own, and
    exited the process -- systemd then restarted into the same value. Here the
    first poll is a bare JSON list and the second is a real reading, and
    `main()` has to reach the second.
    """
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "poller.db"))
    # 0, not the 30 the sibling SIGTERM test uses. That one signals on the
    # FIRST fetch so `stop.wait(interval)` returns immediately; this one has to
    # reach a second poll, so a 30 s interval is 30 s of real sleep in a suite
    # whose total work is about two.
    monkeypatch.setenv("AWAIR_POLL_SECONDS", "0")
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")

    started = time.monotonic()
    bodies = iter(["[]", FIXTURE_TEXT])

    def fetch():
        body = next(bodies)
        if body is FIXTURE_TEXT:
            os.kill(os.getpid(), signal.SIGTERM)
        return body

    monkeypatch.setattr(poller, "make_fetch", lambda url: fetch)
    monkeypatch.setattr(poller, "check_metrics", lambda *a, **k: None)
    monkeypatch.setattr(poller, "check_fans", lambda *a, **k: None)

    poller.main([])  # must return normally, not raise

    # Guards against the interval creeping back: this test is bounded by the
    # two fetches, not by a wait.
    assert time.monotonic() - started < 10

    from awair import db

    conn = db.connect(str(tmp_path / "poller.db"))
    try:
        # The SECOND poll is what proves the loop survived the first.
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
    finally:
        conn.close()


def test_main_does_not_start_a_poll_when_told_to_stop_first(
    monkeypatch, tmp_path, restore_signal_handlers
):
    """A signal landing before the first poll exits without fetching anything."""
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "poller.db"))
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")

    calls = []

    def never_called():
        calls.append(1)
        return FIXTURE_TEXT

    monkeypatch.setattr(poller, "make_fetch", lambda url: never_called)

    real_install = poller.install_handler

    def install_already_stopped():
        stop = real_install()
        stop.set()
        return stop

    monkeypatch.setattr(poller, "install_handler", install_already_stopped)
    poller.main([])
    assert calls == []
