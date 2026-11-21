"""Poll the great room's Levoit Superior 6000S humidifier and store its state.

Run as: python -m awair.humidifier
Config via environment:
  AWAIR_VESYNC_EMAIL            — the VeSync account's email. No default.
  AWAIR_VESYNC_PASSWORD         — that account's password. No default.
  AWAIR_HUMIDIFIER_NAME         — which humidifier, when the account has more
                                  than one. Unset is fine with exactly one.
  AWAIR_HUMIDIFIER_POLL_SECONDS — default and floor 300 (see `poll_seconds`)
  AWAIR_DB                      — shared with the other four pollers

**Both credentials or nothing.** With either one missing this logs one line
and returns, exit status 0, before opening the database or importing
`pyvesync`. That is the "not set up" state and it is a normal one: the other
four feeds are local and configuration-free, and a box that has no VeSync
account must not have a unit crash-looping on it. The homelab unit therefore
wants `Restart=on-failure`, not the `Restart=always` its four siblings use --
see `systemd/awairelement-humidifier.service`, which says so where somebody
installing it will read it.

**Neither credential is ever logged**, and that is enforced rather than
intended: `install_redaction` puts a filter on the root logger's handlers that
replaces either value with `##_REDACTED_##` anywhere in any record, including
the ones `pyvesync` emits. The library's own redaction stays on as well
(`VeSync(redact=True)`, its default, which is what blanks `"email"` and
`"password"` out of a debug-level request dump) -- two independent guards,
because the one that is only a default is the one an upgrade can move.

**The VeSync API is async-only and this poller is its own process.** Three
options were on the table: a thread inside the existing indoor poller, a
thread of its own inside it, and a fifth process. A process, for the reason
`awairelement-basement.service` already gives for its own: a hung read of one
source must not stall any other feed, and an event loop living inside a
`while not stop.is_set()` loop that also does blocking `urllib` and `sqlite3`
work is a stall waiting to happen. The loop here owns its process, so the
only bridge needed is the stop Event, and it crosses via
`asyncio.to_thread(stop.wait, delay)`.

**`pyvesync` is imported lazily**, inside `_poll_loop`, through
`awair.vesync`. `awair.feeds` imports this module for its cadence, and
`awair.feeds` is imported by the web app -- so a module-scope import would
load aiohttp, mashumaro and orjson into both gunicorn workers and the four
pollers that make no VeSync call at all. Everything in this module above
`_poll_loop` is library-free and unit-tested without it.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import UTC, datetime

from awair import db, units
from awair.shutdown import install_handler

log = logging.getLogger("awair.humidifier")

#: The two environment variables the login comes from. Named as constants so
#: the startup check, the README table and the tests all read one spelling.
EMAIL_ENV = "AWAIR_VESYNC_EMAIL"
PASSWORD_ENV = "AWAIR_VESYNC_PASSWORD"

#: Which humidifier, when the account holds more than one. Optional: with a
#: single humidifier on the account there is nothing to disambiguate, and a
#: required name would make the common case need a value nobody can guess
#: without the app open.
NAME_ENV = "AWAIR_HUMIDIFIER_NAME"

#: What `##_REDACTED_##` replaces a credential with. The same marker
#: `pyvesync.utils.logs.LibraryLogger` uses, so a log line that has been
#: through either guard reads the same way.
REDACTION = "##_REDACTED_##"

#: Seconds between polls, and also the floor (#161 asks for "no faster than
#: every 5 minutes"). Five minutes is both because the device's own numbers do
#: not move faster than that in any useful way -- a humidifier changes a room
#: by tenths of a percent a minute -- and because the ceiling here is somebody
#: else's: VeSync rate-limits per account, the library has a
#: `VeSyncRateLimitError` for it, and this process shares that account with
#: every phone the house has the app on.
DEFAULT_POLL_SECONDS = 300
MIN_POLL_SECONDS = 300

#: How many times the back-off doubles before it stops. Four doublings from
#: 300 s is 4800, clamped by `MAX_BACKOFF_SECONDS` to an hour.
#:
#: An hour is the figure to beat: VeSync's limit is an account-wide quota
#: rather than a per-second rate, so the cure is to stop asking for a while,
#: and a poller that retried every five minutes through a quota outage would
#: spend the whole of the next window being refused as well. Doubling rather
#: than jumping straight to the cap keeps a single unlucky refusal cheap.
MAX_BACKOFF_SECONDS = 3600


class VeSyncUnavailable(Exception):
    """The account, the cloud or the network could not answer this poll.

    Raised by `awair.vesync`, caught here, and defined *here* rather than
    there so `poll_once` can name it without importing `pyvesync`: this
    module is on the web app's import path (see the module docstring) and
    that library is not.
    """


class VeSyncRateLimited(VeSyncUnavailable):
    """VeSync refused this poll for quota, not for any fault of ours (#161).

    A subclass, so a caller that only wants "the read failed" needs one
    `except`, and the one caller that must behave differently -- `poll_once`,
    which has to back off rather than retry on the next tick -- lists it
    first.
    """


#: What a bad poll is allowed to cost: one poll, never the service. The same
#: ruling `poller.POLL_FAILURES` and `basement.POLL_FAILURES` record, with the
#: transport half delegated to `awair.vesync` (which translates aiohttp's and
#: pyvesync's exceptions into the two above) and the rest covering what
#: `reading_from_state` and the insert can raise.
POLL_FAILURES = (VeSyncUnavailable, TypeError, ValueError, sqlite3.Error)


def poll_seconds() -> int:
    """Seconds between humidifier polls, per `AWAIR_HUMIDIFIER_POLL_SECONDS`.

    The one reader of that name in this repo, so `/health` judges this feed
    against the cadence `main()` polls at rather than restating it (#158's
    rule). Read per call rather than bound at import, for the reason
    `poller.poll_seconds` explains.

    **Clamped at `MIN_POLL_SECONDS` and the clamp is the requirement**, not a
    guard rail: #161 asks for no faster than five minutes, and a value that
    was merely the default could be overridden to 10 by a copy-pasted
    environment file. A rejected value would be worse -- the poller would not
    start at all over a number it can simply raise -- so it logs the override
    and polls at the floor.
    """
    asked = int(os.environ.get("AWAIR_HUMIDIFIER_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    if asked < MIN_POLL_SECONDS:
        log.warning(
            "%ss is below the %ss floor for this feed -- polling every %ss",
            asked,
            MIN_POLL_SECONDS,
            MIN_POLL_SECONDS,
        )
        return MIN_POLL_SECONDS
    return asked


def credentials(environ=None):
    """`(email, password)`, or None with one line logged if either is missing.

    One line, naming the variables and neither value, and None rather than an
    exception: the caller's response is to exit 0 (see the module docstring on
    `Restart=on-failure`), and a traceback would read as a crash in the
    journal of a box that simply has no VeSync account.

    An empty string counts as missing. `EnvironmentFile=` lines are routinely
    written as `AWAIR_VESYNC_PASSWORD=` with the value still to come, and
    attempting a login with an empty password spends a real request to be told
    what we could see from here.
    """
    source = os.environ if environ is None else environ
    email = (source.get(EMAIL_ENV) or "").strip()
    password = source.get(PASSWORD_ENV) or ""
    if not email or not password:
        log.info(
            "no VeSync login configured (%s and %s must both be set)"
            " -- not recording the humidifier",
            EMAIL_ENV,
            PASSWORD_ENV,
        )
        return None
    return email, password


def redact(text, secrets):
    """`text` with every secret in `secrets` replaced by `REDACTION`.

    A pure function over a string so the filter below has nothing in it worth
    testing separately. Empty secrets are skipped: `str.replace("", ...)`
    inserts the marker between every character, which would turn one
    unconfigured variable into unreadable logs.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTION)
    return text


class _Redactor(logging.Filter):
    """Strip the credentials out of every record that reaches a handler.

    Attached to the root logger's **handlers** rather than to a logger, which
    is the only placement that covers everything: a filter on a logger is
    consulted for records logged on that logger alone, so `pyvesync`'s own
    records -- the ones most likely to carry an account email -- would walk
    straight past it on their way up to the root handler.

    **The message is formatted here and the arguments dropped**, rather than
    each argument being redacted in place. The first draft did the latter and
    `tests/test_humidifier.py::test_no_log_line_anywhere_carries_either_credential`
    caught it: `log.warning("read failed: %s", exc)` carries the credential
    inside an *exception object*, which is not a string, so a per-argument
    redaction skipped it and the handler formatted the secret in afterwards.
    Anything with a `__str__` can carry one; formatting first is the only
    version of this that cannot be walked around by an argument's type.

    The cost is that `%`-formatting happens once per record even if no
    handler would have emitted it -- which for this process is two string
    replacements on a line that was about to be formatted anyway.

    A traceback is covered too, by rendering it here and leaving it in
    `exc_text` where `Formatter.format` will reuse it rather than recomputing
    from `exc_info`.
    """

    def __init__(self, secrets):
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record):
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(
                logging.Formatter().formatException(record.exc_info), self._secrets
            )
        return True


def install_redaction(secrets, logger=None):
    """Put the credential filter on every handler of `logger` (root by default).

    Called from `main()` straight after `basicConfig`, so every line this
    process writes -- ours, pyvesync's, aiohttp's -- has been through it. The
    filter is cheap: two `str.replace` calls on a message that is about to be
    formatted anyway.
    """
    target = logging.getLogger() if logger is None else logger
    redactor = _Redactor(secrets)
    for handler in target.handlers:
        handler.addFilter(redactor)
    return redactor


def backoff_seconds(consecutive_rate_limits, interval):
    """How long to wait after `consecutive_rate_limits` refusals in a row.

    `0` is the ordinary case and answers the configured interval, so the one
    expression here covers both the normal cadence and the back-off -- the
    caller has no branch and cannot forget the reset.

    Doubles from the interval and is capped at `MAX_BACKOFF_SECONDS`. The cap
    is what makes this safe to leave running through a long refusal: without
    one, eight refusals would be a nine-hour silence, and a feed that stops
    recording for nine hours should be visible on `/health` rather than
    patient.
    """
    if consecutive_rate_limits <= 0:
        return interval
    return min(interval * 2**consecutive_rate_limits, MAX_BACKOFF_SECONDS)


def poll_minute(polled_at):
    """The row key: our poll clock in UTC, truncated to the minute.

    The humidifier's state carries no clock of its own -- see
    `db.HUMIDIFIER_COLUMNS_DDL` -- so this is the basement sensor's rule
    rather than the Awair's.
    """
    return polled_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def _flag(value):
    """A device boolean as 0/1, or None if the library never set it."""
    return None if value is None else int(bool(value))


def _text(value):
    """A device enum as the plain string it stands for, or None.

    `DeviceStatus` and `HumidifierModes` are `StrEnum`s, so they would bind to
    SQLite unchanged; `str()` is here so the stored value is a bare `'on'`
    rather than depending on an enum's `__str__` staying what it is today.
    """
    return None if value is None else str(value)


def reading_from_state(state, polled_at):
    """One `HumidifierState` as a `humidifier_readings` row.

    `state` is the object pyvesync fills in from a `Superior6000SResult`; this
    reads it rather than the raw result model, so the library's own mapping --
    including the `temperature / 10` that makes its Fahrenheit a real number
    -- is the one under test. See `tests/test_humidifier.py`, whose payloads
    are hand-built `Superior6000SResult` dicts fed through the library's own
    `get_details` with the transport mocked.

    Every field is nullable and nothing here raises on a missing one: a
    humidifier that is off, empty, or on a firmware that dropped a field is
    exactly the state this feed exists to record, and refusing the row would
    lose the one observation that explains the humidity trace beside it.

    **The temperature is converted here, once, into Celsius.** The device
    reports Fahrenheit (see `db.HUMIDIFIER_COLUMNS_DDL`) and every temperature
    column in this database is Celsius.
    """
    return {
        "ts": poll_minute(polled_at),
        "received_at": polled_at.astimezone(UTC).isoformat(),
        "humid": state.humidity,
        "temp": units.to_celsius(state.temperature, "F"),
        "target_humid": state.auto_target_humidity,
        "mist_level": state.mist_level,
        "mode": _text(state.mode),
        "switch": _text(state.device_status),
        "water_lacks": _flag(state.water_lacks),
        "water_tank_lifted": _flag(state.water_tank_lifted),
        "filter_life": state.filter_life,
    }


async def poll_once(conn, read_state, now=lambda: datetime.now(UTC)) -> str:
    """One poll iteration.

    Returns one of: 'inserted', 'duplicate', 'rate_limited', 'error'.

    'rate_limited' is its own status rather than an 'error' because the
    caller's response to it is different -- it is the only status that changes
    when the next poll happens -- and because the two want different log
    levels: a quota refusal is the account working as designed, an error is
    something to look at.

    The insert is inside the guard for `poller.poll_once`'s reason: five
    writers share the file, and a `database is locked` must cost one poll
    rather than the unit.
    """
    try:
        state = await read_state()
    except VeSyncRateLimited:
        log.warning("VeSync reports a rate limit -- backing off")
        return "rate_limited"
    except POLL_FAILURES as exc:
        log.warning("humidifier read failed: %s: %s", type(exc).__name__, exc)
        return "error"
    try:
        inserted = db.insert_humidifier_reading(conn, reading_from_state(state, now()))
    except POLL_FAILURES as exc:
        log.warning("humidifier store failed: %s: %s", type(exc).__name__, exc)
        return "error"
    return "inserted" if inserted else "duplicate"


def _log_status(status, delay) -> None:
    """One line per poll, at the level the status deserves."""
    level = logging.INFO if status in ("inserted", "duplicate") else logging.WARNING
    log.log(level, "humidifier poll: %s (next in %ss)", status, delay)


async def _poll_loop(conn, interval, login, device_name) -> None:
    """Poll until the stop Event is set. Owns the VeSync session.

    `awair.vesync` is imported here rather than at module scope -- the whole
    of this module's reason for existing as two files. See the module
    docstring.

    The stop Event is a `threading.Event` because the other four pollers'
    loops are synchronous and share `awair.shutdown`; waiting on it from a
    worker thread is what lets one clean-shutdown contract serve both shapes.
    A poll already in flight finishes, exactly as it does there.
    """
    from awair import vesync

    stop = install_handler()
    rate_limits = 0
    async with vesync.Account(*login, device_name=device_name) as account:
        while not stop.is_set():
            status = await poll_once(conn, account.state)
            rate_limits = rate_limits + 1 if status == "rate_limited" else 0
            delay = backoff_seconds(rate_limits, interval)
            _log_status(status, delay)
            if await asyncio.to_thread(stop.wait, delay):
                break


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    login = credentials()
    if login is None:
        return
    install_redaction(login)
    db_path = os.environ.get(
        "AWAIR_DB", os.path.expanduser("~/data/awairelement/awair.db")
    )
    device_name = os.environ.get(NAME_ENV) or None
    interval = poll_seconds()

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = db.connect(db_path)
    log.info(
        "polling the %s humidifier every %ss into %s",
        device_name or "account's only",
        interval,
        db_path,
    )
    try:
        asyncio.run(_poll_loop(conn, interval, login, device_name))
    finally:
        conn.close()
    log.info("humidifier poller stopped cleanly")


if __name__ == "__main__":
    main()
