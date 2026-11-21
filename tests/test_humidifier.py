"""The humidifier feed: what it stores, what it refuses, and what it logs (#161).

**No test here needs the real VeSync account, and none may.** The device half
runs the actual `pyvesync` objects -- the real `VeSyncSuperior6000S`, the real
`Superior6000SResult` parsing, the real `get_details` -- with one method
mocked: `VeSync.async_call_api`, which is the library's single HTTP call. That
is the library boundary, so everything between the recorded payload and
`device.state` is the code that runs on the homelab, including the
`temperature / 10` that makes the device's Fahrenheit a real number. A fake
state object would have tested our reading of a shape we invented.

The account half (`awair.vesync.Account`) is driven through a fake manager
instead, because what it has to get right -- caching a login, dropping it on
one kind of failure and keeping it on another, refusing to guess between two
humidifiers -- is about call sequencing rather than about payloads, and
replaying VeSync's login protocol would test that we can write a fixture.
"""

import asyncio
import io
import json
import logging
import sqlite3
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from pyvesync import VeSync
from pyvesync.device_map import get_device_config
from pyvesync.devices.vesynchumidifier import VeSyncSuperior6000S
from pyvesync.models.vesync_models import ResponseDeviceDetailsModel
from pyvesync.utils.errors import (
    VeSyncLoginError,
    VeSyncRateLimitError,
    VeSyncServerError,
)

from awair import db, humidifier, vesync

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The device-list entry for a Superior 6000S. Only the fields pyvesync's own
#: model requires, with the device type that makes `get_device_config` hand
#: back the Superior 6000S feature map -- which is what decides the class, the
#: mist modes and the target range.
DEVICE_DETAILS = {
    "deviceRegion": "US",
    "isOwner": True,
    "connectionType": "WiFi+BTOnboarding+BTNotify",
    "deviceName": "Great room humidifier",
    "deviceType": "LEH-S601S-WUS",
    "deviceStatus": "on",
    "connectionStatus": "online",
    "cid": "cid-not-a-real-device",
    "uuid": "uuid-not-a-real-device",
    "configModule": "WFON_AHM_LEH-S601S-WUS_US",
    "macID": "aa:bb:cc:dd:ee:ff",
    "type": "wifi-air",
}

EMAIL = "someone@example.invalid"
PASSWORD = "not-the-real-password-either"

AT = datetime(2026, 11, 20, 18, 7, 42, tzinfo=UTC)


def status_payload(**overrides):
    """The recorded `getHumidifierStatus` response, with fields overridden.

    Overrides land on the INNER result -- the `Superior6000SResult` -- so a
    case reads as the one state it is about ("out of water") rather than as a
    wall of JSON with one number changed.
    """
    payload = json.loads((FIXTURES / "superior_6000s_status.json").read_text())
    payload.pop("_comment", None)
    payload["result"]["result"].update(overrides)
    return payload


def _stored(conn):
    """The single stored row as a dict, so a case names the columns it means."""
    cursor = conn.execute("SELECT * FROM humidifier_readings")
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row, strict=True))


def make_device():
    """A real `VeSyncSuperior6000S`, built offline from the device-list entry."""
    manager = VeSync(EMAIL, PASSWORD, redact=True)
    manager.set_credentials("token", "account-id", "US", "US")
    details = ResponseDeviceDetailsModel.from_dict(DEVICE_DETAILS)
    return manager, VeSyncSuperior6000S(
        details, manager, get_device_config(details.deviceType)
    )


def read_state(monkeypatch, payload, status=200):
    """`(device, read_state)` where one API call answers `payload`.

    `VeSync.async_call_api` is the mock point and the only one: everything
    above it -- the bypassV2 envelope, the error-code check, the model
    parsing, `_set_state` -- is the library's real code running against a
    payload shaped like the one the device sends.

    Patched on the CLASS because `VeSync` uses `__slots__`, so the instance
    attribute cannot be replaced.
    """
    _manager, device = make_device()
    calls = []

    async def fake_call(self, *args, **kwargs):
        calls.append(args)
        return payload, status

    monkeypatch.setattr(VeSync, "async_call_api", fake_call)

    async def read():
        await device.update()
        vesync._check_refreshed(device)
        return device.state

    return device, read, calls


def state_from(monkeypatch, **overrides):
    """The device state after one poll of a payload with `overrides` applied."""
    _device, read, _calls = read_state(monkeypatch, status_payload(**overrides))
    return asyncio.run(read())


# --- what a reading is -----------------------------------------------------


def test_a_normal_reading_is_stored(conn, monkeypatch):
    """One ordinary poll: every column the issue asks for, in the row."""
    _device, read, _calls = read_state(monkeypatch, status_payload())

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "inserted"

    stored = _stored(conn)
    assert stored["humid"] == 38
    assert stored["target_humid"] == 45
    assert stored["mist_level"] == 5
    assert stored["mode"] == "humidity"
    assert stored["switch"] == "on"
    assert stored["water_lacks"] == 0
    assert stored["water_tank_lifted"] == 0
    assert stored["filter_life"] == 92
    assert stored["ts"] == "2026-11-20T18:07:00+00:00"
    assert stored["received_at"] == "2026-11-20T18:07:42+00:00"


def test_the_temperature_is_stored_in_celsius(conn, monkeypatch):
    """The device reports tenths of a degree F; this database is Celsius.

    714 is 71.4 F is 21.89 C. Asserted as a number rather than "not 71.4",
    because the failure this guards against is a silent 40-degree error on a
    card that draws it beside five other places' temperatures.
    """
    _device, read, _calls = read_state(monkeypatch, status_payload(temperature=714))

    asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT))

    (temp,) = conn.execute("SELECT temp FROM humidifier_readings").fetchone()
    assert temp == pytest.approx(21.89, abs=0.01)


def test_an_out_of_water_humidifier_is_stored(conn, monkeypatch):
    """The state the humidity trace needs explaining by, not a reason to skip.

    An empty reservoir reports itself switched off with `waterLacksState` set
    and mist level 0. All of that is a reading: the dashboard shades these
    stretches, and a poller that refused the row would leave the humidity sag
    with nothing beside it to say why.
    """
    _device, read, _calls = read_state(
        monkeypatch,
        status_payload(waterLacksState=1, powerSwitch=0, mistLevel=0, humidity=29),
    )

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "inserted"

    row = conn.execute(
        "SELECT switch, water_lacks, water_tank_lifted, mist_level, humid"
        " FROM humidifier_readings"
    ).fetchone()
    assert row == ("off", 1, 0, 0, 29)


def test_a_lifted_tank_is_stored_and_is_not_the_same_as_empty(conn, monkeypatch):
    """Two columns because they are two facts: someone is refilling it.

    Collapsing the pair would lose which one the shading on the dashboard is
    about, and they do not imply each other -- a full tank can be lifted out
    and an empty one can be sitting in place.
    """
    _device, read, _calls = read_state(
        monkeypatch, status_payload(waterTankLifted=1, waterLacksState=0)
    )

    asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT))

    row = conn.execute(
        "SELECT water_lacks, water_tank_lifted FROM humidifier_readings"
    ).fetchone()
    assert row == (0, 1)


def test_an_off_humidifier_is_stored(conn, monkeypatch):
    """Switched off by hand: still a reading, and the one that explains a sag."""
    _device, read, _calls = read_state(
        monkeypatch, status_payload(powerSwitch=0, mistLevel=0, workMode="manual")
    )

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "inserted"

    row = conn.execute("SELECT switch, mode FROM humidifier_readings").fetchone()
    assert row == ("off", "manual")


def test_a_second_poll_in_the_same_minute_stores_nothing(conn, monkeypatch):
    """Deduplicated like the basement feed, and on the same key for its reason.

    The state carries no clock of its own, so the row is keyed on our poll
    clock truncated to the minute. At a five-minute cadence this fires only
    when something polls twice inside one minute -- a restart -- which is
    exactly the case the other four feeds' dedup exists for.
    """
    _device, read, _calls = read_state(monkeypatch, status_payload())

    first = asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT))
    second = asyncio.run(
        humidifier.poll_once(conn, read, now=lambda: AT.replace(second=59))
    )

    assert (first, second) == ("inserted", "duplicate")
    assert conn.execute("SELECT COUNT(*) FROM humidifier_readings").fetchone()[0] == 1


def test_the_next_minute_stores_a_second_row(conn, monkeypatch):
    """Control for the dedup above: it must not swallow real polls."""
    _device, read, _calls = read_state(monkeypatch, status_payload())

    asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT))
    status = asyncio.run(
        humidifier.poll_once(conn, read, now=lambda: AT.replace(minute=12))
    )

    assert status == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM humidifier_readings").fetchone()[0] == 2


def test_a_field_the_device_stops_reporting_stores_null(conn, monkeypatch):
    """A firmware that drops a field costs that column, not the row.

    pyvesync leaves the state attribute at its initial None when it never
    sets one; here that is forced by never having a mode the library
    recognises, which is the one field it warns about rather than refusing.
    """
    _device, read, _calls = read_state(monkeypatch, status_payload(workMode="turbo?"))

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "inserted"

    (mode,) = conn.execute("SELECT mode FROM humidifier_readings").fetchone()
    assert mode is None


def test_a_device_level_error_stores_nothing(conn, monkeypatch):
    """pyvesync logs a refused read and returns; we must not store the old state.

    `get_details` leaves the state object untouched when the device answers a
    non-zero code, so a caller that trusted it would write the PREVIOUS
    poll's humidity under a fresh timestamp -- a dead humidifier drawn as a
    flat line rather than as a gap. `vesync._check_refreshed` is what turns
    that back into an error.
    """
    payload = status_payload()
    payload["result"]["code"] = -11300030  # device offline
    _device, read, _calls = read_state(monkeypatch, payload)

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "error"
    assert conn.execute("SELECT COUNT(*) FROM humidifier_readings").fetchone()[0] == 0


def test_a_locked_database_costs_one_poll_not_the_service(conn, monkeypatch):
    """`sqlite3.Error` is caught, like every other poller in this repo."""
    _device, read, _calls = read_state(monkeypatch, status_payload())

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "insert_humidifier_reading", refuse)

    assert asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT)) == "error"


# --- the cadence and the back-off ------------------------------------------


def test_the_default_cadence_is_five_minutes():
    assert humidifier.poll_seconds() == 300


def test_a_cadence_under_the_floor_is_raised_to_it(monkeypatch, caplog):
    """#161 asks for no faster than five minutes, so the floor is the rule.

    Raised rather than rejected: a poller that refused to start over a number
    it can simply clamp would turn a copy-pasted environment file into a dead
    feed. It says so in the log, because a silently-ignored setting is worse
    than either.
    """
    monkeypatch.setenv("AWAIR_HUMIDIFIER_POLL_SECONDS", "10")

    with caplog.at_level(logging.WARNING, logger="awair.humidifier"):
        assert humidifier.poll_seconds() == humidifier.MIN_POLL_SECONDS

    assert "floor" in caplog.text


def test_a_slower_cadence_is_honoured(monkeypatch):
    """Control for the clamp: it is a floor, not a fixed value."""
    monkeypatch.setenv("AWAIR_HUMIDIFIER_POLL_SECONDS", "900")

    assert humidifier.poll_seconds() == 900


def test_a_rate_limit_is_its_own_status(conn, monkeypatch):
    """Not an 'error': it is the only status that changes when we poll next."""

    async def refuse():
        raise humidifier.VeSyncRateLimited("rate limited")

    assert asyncio.run(humidifier.poll_once(conn, refuse)) == "rate_limited"


def test_the_back_off_doubles_and_is_capped():
    interval = 300
    delays = [humidifier.backoff_seconds(n, interval) for n in range(0, 6)]

    assert delays[0] == interval, "no refusals means the ordinary cadence"
    assert delays[1:4] == [600, 1200, 2400]
    assert delays[-1] == humidifier.MAX_BACKOFF_SECONDS
    assert all(b >= a for a, b in pairwise(delays))


def test_a_rate_limited_poll_waits_longer_and_a_good_one_resets(conn, monkeypatch):
    """The loop itself, not just the arithmetic: refuse twice, then succeed.

    Drives `_poll_loop` with a fake `Account` and a stop Event that records
    every delay it is asked to wait. Without this the back-off could be
    correct in `backoff_seconds` and never reach the sleep -- which is the
    bug the issue is actually asking to be prevented.
    """
    answers = [
        humidifier.VeSyncRateLimited("rate limited"),
        humidifier.VeSyncRateLimited("rate limited"),
        "ok",
        "ok",
    ]
    waits = _run_loop(conn, monkeypatch, answers, status_payload())

    assert waits == [600, 1200, 300, 300]


def test_a_transport_failure_does_not_back_off(conn, monkeypatch):
    """Only a quota refusal changes the cadence. A timeout is one bad poll."""
    answers = [humidifier.VeSyncUnavailable("boom"), "ok"]

    assert _run_loop(conn, monkeypatch, answers, status_payload()) == [300, 300]


def _run_loop(conn, monkeypatch, answers, payload):
    """Run `_poll_loop` over a scripted list of answers; return the waits.

    An entry is either an exception to raise or the word 'ok', which reads
    the real device state built from `payload`.
    """
    _device, read, _calls = read_state(monkeypatch, payload)
    script = list(answers)
    waits = []

    class FakeAccount:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def state(self):
            answer = script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return await read()

    class FakeStop:
        def is_set(self):
            return not script

        def wait(self, delay):
            waits.append(delay)
            return not script

    monkeypatch.setattr(vesync, "Account", FakeAccount)
    monkeypatch.setattr(humidifier, "install_handler", lambda: FakeStop())
    # One row per poll would collide on the poll-minute key; the clock moves
    # so the loop's statuses are the ones under test rather than 'duplicate'.
    minutes = iter(range(len(answers) + 1))
    monkeypatch.setattr(
        humidifier,
        "poll_minute",
        lambda at: f"2026-11-20T18:{next(minutes):02d}:00+00:00",
    )
    asyncio.run(humidifier._poll_loop(conn, 300, (EMAIL, PASSWORD), None))
    return waits


# --- the login, and what is never logged -----------------------------------


def test_both_credentials_missing_is_a_clean_exit(tmp_path, monkeypatch, caplog):
    """No traceback, no database, no crash loop -- and one line that says why.

    This is the "not set up" state and it is a normal one: four of the five
    feeds here are local and configuration-free, so a box with no VeSync
    account must not have a unit failing on it.
    """
    monkeypatch.delenv(humidifier.EMAIL_ENV, raising=False)
    monkeypatch.delenv(humidifier.PASSWORD_ENV, raising=False)
    db_path = tmp_path / "never" / "awair.db"
    monkeypatch.setenv("AWAIR_DB", str(db_path))

    with caplog.at_level(logging.INFO, logger="awair.humidifier"):
        assert humidifier.main() is None

    assert not db_path.exists(), "a poller with no login must not create the database"
    assert humidifier.EMAIL_ENV in caplog.text
    assert humidifier.PASSWORD_ENV in caplog.text


@pytest.mark.parametrize("present", [humidifier.EMAIL_ENV, humidifier.PASSWORD_ENV])
def test_one_credential_alone_is_also_not_configured(present, monkeypatch, caplog):
    """Half a login is not a login, and must not spend a request finding out."""
    monkeypatch.delenv(humidifier.EMAIL_ENV, raising=False)
    monkeypatch.delenv(humidifier.PASSWORD_ENV, raising=False)
    monkeypatch.setenv(present, "something")

    with caplog.at_level(logging.INFO, logger="awair.humidifier"):
        assert humidifier.credentials() is None


def test_an_empty_password_is_missing_rather_than_a_login(monkeypatch):
    """`AWAIR_VESYNC_PASSWORD=` with nothing after it is the half-written file."""
    monkeypatch.setenv(humidifier.EMAIL_ENV, EMAIL)
    monkeypatch.setenv(humidifier.PASSWORD_ENV, "")

    assert humidifier.credentials() is None


def test_configured_credentials_are_returned(monkeypatch):
    """Control: the check above must not refuse a real login."""
    monkeypatch.setenv(humidifier.EMAIL_ENV, f"  {EMAIL} ")
    monkeypatch.setenv(humidifier.PASSWORD_ENV, PASSWORD)

    assert humidifier.credentials() == (EMAIL, PASSWORD)


def test_no_log_line_anywhere_carries_either_credential(conn, monkeypatch, root_log):
    """The requirement, asserted over every line a whole poll cycle writes.

    The handler is attached to the root logger and `install_redaction` to its
    handlers, which is the production arrangement (`main` does exactly this
    after `basicConfig`) -- so this covers `pyvesync`'s own records as well
    as ours. Then every failure path that could carry a credential is driven
    through it: a login error whose MESSAGE contains the password, a device
    error, a rate limit, and a successful poll.

    The login error carrying the password is the case that makes this a test
    rather than a hope: nothing stops a cloud API from echoing a credential
    back in an error string, and `log.warning("%s", exc)` would print it.
    """
    humidifier.install_redaction((EMAIL, PASSWORD))

    logging.getLogger("pyvesync").error(
        "API call failed for %s with password %s", EMAIL, PASSWORD
    )
    _device, read, _calls = read_state(monkeypatch, status_payload())
    asyncio.run(humidifier.poll_once(conn, read, now=lambda: AT))

    async def refuse_login():
        raise humidifier.VeSyncUnavailable(
            f"logging in: VeSyncLoginError: bad password {PASSWORD} for {EMAIL}"
        )

    asyncio.run(humidifier.poll_once(conn, refuse_login))

    written = root_log.getvalue()
    assert written, "nothing was logged, so this asserts nothing"
    assert EMAIL not in written
    assert PASSWORD not in written
    assert humidifier.REDACTION in written


def test_the_redactor_leaves_everything_else_alone(root_log):
    """Control for the filter: it must not eat the message it was protecting."""
    humidifier.install_redaction((EMAIL, PASSWORD))

    logging.getLogger("awair.humidifier").warning("humidifier poll: %s", "rate_limited")

    assert "humidifier poll: rate_limited" in root_log.getvalue()


def test_an_unset_credential_does_not_redact_everything():
    """An empty secret would match between every character of every message."""
    assert humidifier.redact("hello", ("",)) == "hello"
    assert humidifier.redact("hello", ("ell",)) == f"h{humidifier.REDACTION}o"


@pytest.fixture
def root_log():
    """Every record this process logs, in one stream, handler cleaned up after.

    The ROOT logger, because the credential test's point is to cover the
    records this project does not write -- `pyvesync`'s -- and because that
    is where `humidifier.main` installs the redaction filter.

    The teardown matters as much as the setup: the root logger is
    process-global and `install_redaction` adds a filter to its handlers, so
    a test that left one attached would quietly redact the rest of the
    session's output.
    """
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    saved = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(saved)


def test_a_traceback_is_redacted_too(root_log):
    """`exc_info=` renders the exception's own text, which can carry a secret."""
    humidifier.install_redaction((EMAIL, PASSWORD))

    try:
        raise RuntimeError(f"refused for {EMAIL}")
    except RuntimeError:
        logging.getLogger("awair.humidifier").warning("boom", exc_info=True)

    written = root_log.getvalue()
    assert "Traceback" in written, "no traceback was rendered, so this asserts nothing"
    assert EMAIL not in written


# --- the VeSync boundary ---------------------------------------------------
#
# `awair.vesync.Account` is driven through a fake manager: what it has to get
# right is call sequencing (log in once, keep the device, drop both on the
# right failures) rather than payload parsing, which the device half above
# covers against the real library.


class FakeManager:
    """A `VeSync` stand-in: records calls, answers scripted failures."""

    def __init__(self, username, password, redact=True):
        self.username = username
        self.password = password
        self.redact = redact
        self.enabled = False
        self.devices = type("Devices", (), {"humidifiers": []})()
        self.calls = []
        self.login_result = True
        self.login_error = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False

    async def login(self):
        self.calls.append("login")
        if self.login_error is not None:
            raise self.login_error
        self.enabled = bool(self.login_result)
        return self.login_result

    async def get_devices(self):
        self.calls.append("get_devices")
        return True


class FakeDevice:
    """One humidifier on the fake account, with a scripted `update`."""

    def __init__(self, name="Great room humidifier", error=None):
        self.device_name = name
        self.error = error
        self.updates = 0
        self.state = object()
        self.last_response = type("Info", (), {"code": 0, "name": "", "message": ""})()

    async def update(self):
        self.updates += 1
        if self.error is not None:
            raise self.error


def account_with(*devices, **kwargs):
    """An `Account` over a fake manager holding `devices`."""
    made = {}

    def factory(username, password, redact=True):
        manager = FakeManager(username, password, redact=redact)
        manager.devices.humidifiers = list(devices)
        made["manager"] = manager
        return manager

    account = vesync.Account(EMAIL, PASSWORD, manager_factory=factory, **kwargs)
    return account, made


def test_the_login_and_device_lookup_happen_once(conn):
    """Three polls, one login: the account is rate-limited and we share it.

    Re-authenticating every five minutes would spend a third of this
    poller's request budget on saying who we are.
    """
    device = FakeDevice()
    account, made = account_with(device)

    async def poll_three():
        async with account:
            for _ in range(3):
                await account.state()

    asyncio.run(poll_three())

    assert made["manager"].calls == ["login", "get_devices"]
    assert device.updates == 3


def test_a_rate_limit_keeps_the_session(conn):
    """Logging in again would spend another request against the quota."""
    device = FakeDevice(error=VeSyncRateLimitError())
    account, made = account_with(device)

    async def poll_twice():
        async with account:
            for _ in range(2):
                with pytest.raises(humidifier.VeSyncRateLimited):
                    await account.state()

    asyncio.run(poll_twice())

    assert made["manager"].calls == ["login", "get_devices"]


def test_any_other_failure_drops_the_session(conn):
    """An expired token costs one poll, not every poll after it."""
    device = FakeDevice(error=VeSyncServerError("upstream fell over"))
    account, made = account_with(device)

    async def poll_twice():
        async with account:
            for _ in range(2):
                with pytest.raises(humidifier.VeSyncUnavailable):
                    await account.state()

    asyncio.run(poll_twice())

    assert made["manager"].calls == ["login", "get_devices", "login", "get_devices"]


def test_a_rate_limit_is_translated_rather_than_leaked():
    """The poller must not have to name a dependency's exception tree."""
    account, _made = account_with(FakeDevice(error=VeSyncRateLimitError()))

    async def poll():
        async with account:
            await account.state()

    with pytest.raises(humidifier.VeSyncRateLimited):
        asyncio.run(poll())


def test_a_refused_login_is_unavailable_not_a_crash():
    account, made = account_with(FakeDevice())

    async def poll():
        async with account:
            made["manager"].login_error = VeSyncLoginError("account or password")
            await account.state()

    with pytest.raises(humidifier.VeSyncUnavailable):
        asyncio.run(poll())


def test_a_login_that_answers_false_is_also_unavailable():
    """`login()` returns a bool as well as raising; both mean no session."""
    account, made = account_with(FakeDevice())

    async def poll():
        async with account:
            made["manager"].login_result = False
            await account.state()

    with pytest.raises(humidifier.VeSyncUnavailable):
        asyncio.run(poll())


def test_the_only_humidifier_needs_no_name():
    """One humidifier on the account is not ambiguous, so do not ask for a name."""
    device = FakeDevice()

    assert vesync.find_humidifier(_manager_holding(device)) is device


def test_a_named_humidifier_is_matched_loosely():
    """The name is typed into an environment file from what the app shows."""
    wanted = FakeDevice("Great Room Humidifier")
    other = FakeDevice("Bedroom humidifier")
    manager = _manager_holding(other, wanted)

    assert vesync.find_humidifier(manager, "  great room humidifier ") is wanted


def test_two_humidifiers_and_no_name_is_a_refusal_that_says_what_to_set():
    """Picking one at random would make a feed nobody can identify later."""
    manager = _manager_holding(FakeDevice("One"), FakeDevice("Two"))

    with pytest.raises(humidifier.VeSyncUnavailable) as caught:
        vesync.find_humidifier(manager)

    assert humidifier.NAME_ENV in str(caught.value)
    assert "One" in str(caught.value)


def test_a_name_that_matches_nothing_lists_what_is_there():
    manager = _manager_holding(FakeDevice("Great room humidifier"))

    with pytest.raises(humidifier.VeSyncUnavailable) as caught:
        vesync.find_humidifier(manager, "Office")

    assert "Great room humidifier" in str(caught.value)


def test_an_account_with_no_humidifier_says_so():
    with pytest.raises(humidifier.VeSyncUnavailable) as caught:
        vesync.find_humidifier(_manager_holding())

    assert "no humidifier" in str(caught.value)


def _manager_holding(*devices):
    manager = FakeManager(EMAIL, PASSWORD)
    manager.devices.humidifiers = list(devices)
    return manager


def test_the_library_is_kept_out_of_the_web_process():
    """`awair.humidifier` must stay importable without loading `pyvesync`.

    `awair.feeds` imports this module for its cadence and the Flask app
    imports `awair.feeds`, so a module-scope import here would pull aiohttp,
    mashumaro and orjson into both gunicorn workers and the four pollers that
    make no VeSync call. Asserted on the source rather than on `sys.modules`,
    because by the time this test runs the library is loaded anyway -- by
    this file.
    """
    source = Path(humidifier.__file__).read_text()
    module_scope = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "pyvesync" in line
    ]

    assert module_scope == [], module_scope
    assert "    from awair import vesync" in source, (
        "the lazy import of the boundary module has moved -- if it is now at "
        "module scope, the web worker loads an async HTTP stack it never uses"
    )


def test_main_with_a_login_stores_a_reading_and_redacts_its_own_log(
    tmp_path, monkeypatch, root_log
):
    """`main()` end to end with a login set: the wiring, not the parts.

    Every piece above is tested alone; this is the one place that proves
    `main` installs the redaction BEFORE anything that could log a
    credential, hands the login to the account, and closes cleanly. The
    account is a fake (no VeSync call) that logs both credentials the way a
    careless library would, and the device state is the real library object.
    """
    db_path = tmp_path / "data" / "awair.db"
    monkeypatch.setenv("AWAIR_DB", str(db_path))
    monkeypatch.setenv(humidifier.EMAIL_ENV, EMAIL)
    monkeypatch.setenv(humidifier.PASSWORD_ENV, PASSWORD)
    monkeypatch.delenv(humidifier.NAME_ENV, raising=False)
    _device, read, _calls = read_state(monkeypatch, status_payload())
    handed = []

    class LeakyAccount:
        def __init__(self, email, password, device_name=None):
            handed.append((email, password, device_name))

        async def __aenter__(self):
            logging.getLogger("pyvesync").warning(
                "login %s / %s", handed[0][0], handed[0][1]
            )
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def state(self):
            return await read()

    class StopAfterOnePoll:
        def is_set(self):
            return False

        def wait(self, delay):
            return True

    monkeypatch.setattr(vesync, "Account", LeakyAccount)
    monkeypatch.setattr(humidifier, "install_handler", StopAfterOnePoll)

    humidifier.main()

    assert handed == [(EMAIL, PASSWORD, None)]
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT count(*) FROM humidifier_readings").fetchone() == (1,)
    check.close()
    written = root_log.getvalue()
    assert "stopped cleanly" in written
    assert humidifier.REDACTION in written, "the leaky line never reached the log"
    assert EMAIL not in written
    assert PASSWORD not in written
