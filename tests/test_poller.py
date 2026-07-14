"""Parsing device JSON and single poll iterations."""

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from awair import poller
from awair.poller import handle_device_health, make_fetch, parse_reading, poll_once

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
    from datetime import datetime, timezone

    return datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


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


def test_main_polls_once_and_exits_when_sleep_raises(monkeypatch, tmp_path):
    """Drive the main() poll loop through exactly one iteration."""
    monkeypatch.setenv("AWAIR_DB", str(tmp_path / "poller.db"))
    monkeypatch.setenv("AWAIR_POLL_SECONDS", "1")
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")

    fixture = FIXTURE_TEXT
    monkeypatch.setattr(poller, "make_fetch", lambda url: lambda: fixture)
    monkeypatch.setattr(poller, "check_metrics", lambda *a, **k: None)
    monkeypatch.setattr(poller, "check_fans", lambda *a, **k: None)

    class Stop(Exception):
        pass

    def stop(_seconds):
        raise Stop

    monkeypatch.setattr(poller.time, "sleep", stop)
    with pytest.raises(Stop):
        poller.main([])
    # The reading landed — proves poll_once ran with the real connection.
    from awair import db

    conn = db.connect(str(tmp_path / "poller.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1
    finally:
        conn.close()
