"""One log line per dashboard request, with how long the app took (#141).

Tom's complaint on #141 is *"I often see long delays in responding to a
request. Sometimes it's the entire page load, some times it's just the json."*
Before this module there was nothing to look at: `awairelement-web.service`
runs gunicorn with no `--access-logfile`, so the journal held worker lifecycle
lines and not one request. "Is it slow?" was unanswerable on the record.

So the deliverable is a measurement, and `install(app)` is all of it:

* a line per request, naming the path **with its query string** -- the duration
  of `/api/series` varies 40x across `?range=`, so a line that omits it
  averages the fast case and the slow one together and answers nothing;
* a `Server-Timing: app;dur=<ms>` header, which is the one thing that splits
  "the entire page load" from "just the json" *in Tom's browser* rather than
  here. Devtools shows it beside the request's TTFB, and JS can read it off
  `performance.getEntriesByType("resource")[i].serverTiming`.

## What this cannot see, and neither can any in-app timer

The clock starts when a worker begins handling the request and stops when the
response object is finished. Two real costs sit outside that:

1. **Waiting for a worker.** The unit runs `gunicorn -w 2` with *sync* workers,
   so two requests are in flight at a time. `dashboard.js` fires three `/api/*`
   fetches at once, after the browser has already asked for the document and
   three static assets -- so a request can queue, and its timer does not start
   until it stops queueing. Nothing in the WSGI environ carries an arrival
   stamp, so this number is not recoverable from inside the app. **TTFB minus
   `Server-Timing: app` is that wait**, which is why the header is here.
2. **Transfer.** `/api/series?range=30d` is a ~500 KB body. Over wifi that is a
   wait no server-side timer attributes to itself, so the byte count is on the
   line to make the possibility visible.

Both are measured once, against the live database, in
`docs/verification/response_time_probe.py`.

## Why the level is set explicitly

Flask leaves `app.logger` at `NOTSET`, so its effective level comes from root:
`WARNING`. Measured on this repo's Flask (3.1.3), a fresh app reports
`isEnabledFor(logging.INFO) is False` -- an `INFO` call on it is dropped with
no error anywhere. A timing instrument that logs nothing is the worst outcome
available, because it looks shipped. `install` therefore raises the level, and
`tests/test_web_timing.py` pins both halves: the enabled level, and (with
root's handlers cleared, which is production's topology) that the logger
carries Flask's own stderr handler, since stderr is what systemd captures.

A request at or over `AWAIR_WEB_SLOW_MS` is logged at `WARNING` instead. That
is not decoration: `WARNING` clears the default effective level on its own, so
the one line nobody can afford to lose survives the level configuration above
being undone by a future edit.
"""

import logging
import os
from time import perf_counter

from flask import g, request

# The logger `install` configures. `awair.web` is `Flask(__name__)`'s logger
# name in this app, which is the logger Flask attaches its stderr handler to --
# so configuring any other name would log to a logger nothing is listening on.
LOGGER_NAME = "awair.web"

# Attribute on `flask.g` holding the request's start mark. `g` is per-request
# and per-context, so this is safe under any worker class.
_START_ATTR = "awair_timing_start"

# Requests at or above this many milliseconds log at WARNING rather than INFO.
# One second, because the thing #141 is about is a delay Tom *notices*, and
# nothing measured on the box comes close: the slowest request in the whole
# probe is `/api/series?range=30d` at 308 ms. A threshold under the measured
# worst case would make the warning tier mean "normal".
DEFAULT_SLOW_MS = 1000.0
SLOW_MS_ENV = "AWAIR_WEB_SLOW_MS"
SLOW_MS_CONFIG = "AWAIR_WEB_SLOW_MS"


def threshold_from_env(logger, environ=None):
    """Resolve `AWAIR_WEB_SLOW_MS` once, loudly.

    Resolved at app construction rather than per request so a malformed value
    produces one startup warning instead of one per request -- and so it is a
    warning at all. A typo here disarms the WARNING tier, which is the tier
    that survives a level misconfiguration, so falling back in silence would
    lose the backstop and the notice of having lost it.

    Negative values are refused for the same reason from the other side: at
    `-1` every request is "slow", so the tier stops distinguishing anything.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(SLOW_MS_ENV)
    if raw is None:
        return DEFAULT_SLOW_MS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number — using the default %.0f ms",
            SLOW_MS_ENV,
            raw,
            DEFAULT_SLOW_MS,
        )
        return DEFAULT_SLOW_MS
    if value < 0:
        logger.warning(
            "%s=%r is negative, which would mark every request slow — "
            "using the default %.0f ms",
            SLOW_MS_ENV,
            raw,
            DEFAULT_SLOW_MS,
        )
        return DEFAULT_SLOW_MS
    return value


def response_bytes(response):
    """The response length for the log line, or `-` when it is unknown.

    Read off `Content-Length` and never from the body. `send_file` -- which is
    every static asset, which is every page load -- returns a response in
    direct passthrough mode, and `get_data()` on one *raises*, so reading the
    body to count it would 500 the assets rather than time them. A streamed
    response has no length at all, and that is reported as missing rather than
    as zero.
    """
    length = response.content_length
    return "-" if length is None else str(length)


def logged_path():
    """`/api/series?range=30d` -- the path with its query, minus a bare `?`.

    `request.full_path` appends `?` unconditionally, so `/` would log as `/?`.
    """
    full = request.full_path
    return full[:-1] if full.endswith("?") else full


def install(app):
    """Time every request on `app`, logging one line each and a timing header."""
    logger = app.logger
    logger.setLevel(logging.INFO)
    app.config[SLOW_MS_CONFIG] = threshold_from_env(logger)

    @app.before_request
    def _mark_request_start():
        setattr(g, _START_ATTR, perf_counter())

    @app.after_request
    def _log_request_duration(response):
        start = getattr(g, _START_ATTR, None)
        if start is None:
            # Reachable only if a `before_request` ahead of this one raised.
            # Say so and hand the response back untouched: an instrument has
            # no business being the reason a page fails to load.
            logger.warning("request timing start marker missing for %s", request.path)
            return response
        duration_ms = (perf_counter() - start) * 1000
        response.headers["Server-Timing"] = f"app;dur={duration_ms:.1f}"
        logger.log(
            logging.WARNING
            if duration_ms >= app.config[SLOW_MS_CONFIG]
            else logging.INFO,
            "request %s %s status=%s dur_ms=%.1f bytes=%s endpoint=%s",
            request.method,
            logged_path(),
            response.status_code,
            duration_ms,
            response_bytes(response),
            request.endpoint,
        )
        return response

    return app
