"""The VeSync boundary: log in, find the humidifier, read its state (#161).

This module is the **only** place in the project that imports `pyvesync`, and
that is the whole reason it is a separate file from `awair.humidifier`:
`awair.feeds` imports that module for its cadence and the web app imports
`awair.feeds`, so a `pyvesync` import there would load aiohttp, mashumaro and
orjson into both gunicorn workers and all four other pollers. The poller
imports this lazily, inside its loop.

**It translates the library's exceptions into two of our own**, defined in
`awair.humidifier` (which has no `pyvesync` import to be confused by them):

- `VeSyncRateLimited` for the quota refusal, because that is the one failure
  whose cure is to ask again later rather than to ask again.
- `VeSyncUnavailable` for everything else the cloud, the account or the
  network can do to a poll.

Nothing else escapes. A caller does not have to know that a humidifier read
can fail as a `VeSyncServerError`, an `aiohttp.ClientConnectorError`, a
`VeSyncTokenError` or an `asyncio.TimeoutError`, and the poller's
`POLL_FAILURES` tuple does not have to name a dependency's exception tree.

**A swallowed device error is turned back into an exception.** pyvesync's
`get_details` logs a non-zero device response code and returns without
touching the state, which leaves the previous poll's values in place -- so a
caller that trusted it would store the same reading again, under a new
timestamp, through an outage. `Account.state` checks `device.last_response`
and refuses; see `_check_refreshed`.
"""

import logging

import aiohttp
from pyvesync import VeSync
from pyvesync.utils.errors import VeSyncError, VeSyncRateLimitError

from awair.humidifier import NAME_ENV, VeSyncRateLimited, VeSyncUnavailable

log = logging.getLogger("awair.vesync")

#: Transport failures that never reach pyvesync's own error translation --
#: a DNS failure, a dropped connection, a read timeout. `OSError` is the
#: parent of most of aiohttp's and is named for the ones raised before aiohttp
#: wraps them; `TimeoutError` is `asyncio.TimeoutError` as of Python 3.11, so
#: naming both would be one class written twice.
_TRANSPORT_FAILURES = (aiohttp.ClientError, TimeoutError, OSError)


async def _guard(awaitable, what):
    """Await `awaitable`, re-raising anything the library can throw as ours.

    `what` is a phrase naming the step, so the poller's one log line says
    which call failed without a traceback: "logging in", "listing the
    account's devices", "reading the humidifier".

    The exception *type* is kept in the message and the original is chained,
    so `--log-level DEBUG` and a crash report both still name
    `VeSyncServerError` rather than only our paraphrase of it.
    """
    try:
        return await awaitable
    except VeSyncRateLimitError as exc:
        raise VeSyncRateLimited(f"{what}: rate limited") from exc
    except (VeSyncError, *_TRANSPORT_FAILURES) as exc:
        raise VeSyncUnavailable(f"{what}: {type(exc).__name__}: {exc}") from exc


def _check_refreshed(device):
    """Raise unless the device's last API response was a success.

    pyvesync reports a device-level error -- offline, operational error, a
    non-zero `code` -- by logging it and returning from `get_details` with the
    state untouched. That is reasonable for a library and wrong for a
    recorder: the state object still holds the *previous* poll's humidity, so
    storing it would write a fresh timestamp over a stale reading and a dead
    device would draw as a flat line rather than a gap.
    """
    response = device.last_response
    if response is None:
        raise VeSyncUnavailable("the humidifier returned no response at all")
    if response.code not in (0, None):
        raise VeSyncUnavailable(
            f"the humidifier refused the read: {response.name} ({response.message})"
        )


def find_humidifier(manager, device_name=None):
    """The humidifier to record, out of everything on the account.

    With `device_name` set, the humidifier whose VeSync name matches it,
    compared case-insensitively and stripped -- the name is typed into an
    environment file from what the phone app shows, and `"Great Room
    Humidifier "` should not be a silent no-match.

    Without one: the account's single humidifier. Two humidifiers and no name
    is a refusal that names them, because a recorder that picked one at random
    would produce a feed nobody could identify later -- and the fix is one
    environment variable this message spells out.
    """
    humidifiers = list(manager.devices.humidifiers)
    if device_name:
        wanted = device_name.strip().casefold()
        for device in humidifiers:
            if device.device_name.strip().casefold() == wanted:
                return device
        raise VeSyncUnavailable(
            f"no humidifier named {device_name!r} on this account"
            f" (found: {[d.device_name for d in humidifiers]})"
        )
    if not humidifiers:
        raise VeSyncUnavailable("this VeSync account has no humidifier on it")
    if len(humidifiers) > 1:
        raise VeSyncUnavailable(
            f"this account has {len(humidifiers)} humidifiers"
            f" ({[d.device_name for d in humidifiers]}) -- set {NAME_ENV}"
        )
    return humidifiers[0]


class Account:
    """One VeSync session and the one humidifier this app records.

    Used as an async context manager, which is how the library wants to own
    its `aiohttp.ClientSession`:

        async with Account(email, password) as account:
            state = await account.state()

    **The login and the device lookup are lazy and cached.** They cost two API
    calls, the account is rate-limited, and a poller that re-authenticated
    every five minutes would spend a third of its quota on it. They are
    dropped again on any failure that is not a rate limit (`state` below), so
    an expired token costs one poll rather than every poll after it.

    `redact` is left at the library's default of True and passed explicitly:
    it is the switch that blanks the account email and password out of a
    debug-level request dump, and a default is exactly the kind of thing a
    dependency bump moves. `awair.humidifier.install_redaction` is the other
    half, and does not trust this one.
    """

    def __init__(self, email, password, device_name=None, manager_factory=VeSync):
        self._email = email
        self._password = password
        self._device_name = device_name
        self._manager_factory = manager_factory
        self._manager = None
        self._device = None

    async def __aenter__(self):
        self._manager = self._manager_factory(self._email, self._password, redact=True)
        await self._manager.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        manager, self._manager, self._device = self._manager, None, None
        if manager is not None:
            await manager.__aexit__(*exc_info)
        return False

    def _forget(self):
        """Drop the cached login and device so the next poll starts over."""
        self._device = None
        if self._manager is not None:
            self._manager.enabled = False

    async def _humidifier(self):
        """The device, logging in and listing the account first if needed."""
        if self._device is not None:
            return self._device
        if not self._manager.enabled:
            if not await _guard(self._manager.login(), "logging in"):
                raise VeSyncUnavailable("logging in: VeSync refused the login")
        await _guard(self._manager.get_devices(), "listing the account's devices")
        self._device = find_humidifier(self._manager, self._device_name)
        log.info("recording the humidifier named %r", self._device.device_name)
        return self._device

    async def state(self):
        """The humidifier's current state, or one of our two exceptions.

        A rate limit propagates with the session intact -- re-authenticating
        would spend another request against the quota that just refused us.
        Anything else drops the session, because the failures that are not
        transient at the account level (an expired token, a device that has
        been renamed or removed) are exactly the ones a fresh login and a
        fresh device list would fix, and the ones that are transient cost only
        one extra request per failed poll at a five-minute cadence.
        """
        try:
            device = await self._humidifier()
            await _guard(device.update(), "reading the humidifier")
            _check_refreshed(device)
            return device.state
        except VeSyncRateLimited:
            raise
        except VeSyncUnavailable:
            self._forget()
            raise
