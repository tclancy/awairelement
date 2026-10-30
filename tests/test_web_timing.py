"""Per-request response-time logging for the dashboard (#141).

#141 asks a question the app could not answer: *"Sometimes it's the entire
page load, sometimes it's just the json."* Nothing on this side timed anything
-- gunicorn runs without `--access-logfile`, so the journal carried lifecycle
lines and not one request -- so the first job is an instrument, not a fix.

Three properties, each of which fails in its own way:

* **The line is emitted at all.** Flask leaves `app.logger` at `NOTSET`, so
  its effective level comes from root and is `WARNING`; an `INFO` call on it is
  dropped on the floor. Measured on this repo's Flask (3.1.3): a fresh app
  reports `isEnabledFor(INFO) == False`. So the level is configured
  deliberately and pinned here, because the failure mode is a feature that
  looks shipped, logs nothing, and has no error to go looking for.
* **Every request is timed, including the ones that are not JSON.** That is
  the whole point of the ticket -- the document and the static assets have to
  appear beside `/api/series` or the log cannot separate "the page" from "the
  json". Static files are the case with teeth: `send_file` hands back a
  response in *direct passthrough* mode, and `get_data()` on one raises, so
  the length has to come from `content_length`. `test_a_static_file_is_timed`
  asserts the passthrough mode as a control -- if Flask ever stops using it,
  that test should say so rather than quietly covering nothing.
* **A slow request is logged even if the level configuration is undone.**
  Slow lines go out at `WARNING`, which is above the default effective level.
  That is not belt-and-braces: the one line nobody can afford to lose is the
  one that would have explained a stall.

What the log line cannot see is named in `awair/timing.py` and in
`docs/verification/response_time_probe.py`: queue time ahead of a sync worker,
and transfer time after the response is handed to gunicorn.
"""

import logging
import re

import flask
import pytest

from awair import db, timing
from awair.web import create_app

RECORD_RE = re.compile(
    r"^request (?P<method>\w+) (?P<path>\S+) status=(?P<status>\d+) "
    r"dur_ms=(?P<dur_ms>[\d.]+) bytes=(?P<bytes>\d+|-) endpoint=(?P<endpoint>\S+)$"
)


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "awair.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO readings (ts, received_at, co2, temp) VALUES (?, ?, ?, ?)",
        ("2026-09-10T01:00:00.000Z", "2026-09-10T01:00:00+00:00", 500, 22.5),
    )
    conn.commit()
    conn.close()
    return str(path)


class Collector(logging.Handler):
    """Collect records that actually got through the logger's level."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def lines(self):
        return [r.getMessage() for r in self.records]

    def matched(self):
        """Every collected line parsed by RECORD_RE, as dicts."""
        out = []
        for line in self.lines:
            match = RECORD_RE.match(line)
            if match:
                out.append(match.groupdict())
        return out


@pytest.fixture
def collector():
    """Attach a handler to the logger `install` configures, then detach it.

    Deliberately not `caplog`: `caplog.set_level` would raise the level of the
    very logger whose level is the thing under test, so the first property
    above would pass on a build that had lost it.
    """
    handler = Collector()
    logger = logging.getLogger(timing.LOGGER_NAME)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def client_for(seeded, collector):
    client = create_app(seeded).test_client()
    collector.records.clear()  # discard anything app construction logged
    return client


REQUESTS = ("/", "/api/series", "/api/latest", "/static/dashboard.js")


@pytest.mark.parametrize("path", REQUESTS)
def test_every_request_logs_exactly_one_timed_line(path, seeded, collector):
    client = client_for(seeded, collector)

    assert client.get(path).status_code == 200

    matched = collector.matched()
    assert len(matched) == 1, collector.lines
    line = matched[0]
    assert line["method"] == "GET"
    assert line["path"] == path
    assert line["status"] == "200"
    assert float(line["dur_ms"]) >= 0


def test_the_query_string_is_on_the_line(seeded, collector):
    """`?range=` is what the duration varies with, so it has to be visible.

    Measured on the homelab's 56 MB database, `/api/series` is 7.9 ms at
    `today` and 308 ms at `30d`. A log line naming only `/api/series` averages
    those together and answers nothing.
    """
    client = client_for(seeded, collector)

    client.get("/api/series?range=30d")

    assert collector.matched()[0]["path"] == "/api/series?range=30d"


def test_a_pathless_request_does_not_carry_a_bare_question_mark(seeded, collector):
    """`request.full_path` appends `?` even with no query; it is trimmed."""
    client = client_for(seeded, collector)

    client.get("/")

    assert collector.matched()[0]["path"] == "/"


def test_a_static_file_is_timed_without_reading_its_body(seeded, collector):
    """Control and assertion in one: the passthrough mode is real.

    `send_file` responses raise on `get_data()`, so a length read that way
    would 500 every static request -- which is to say every page load. If the
    first assertion ever fails, Flask has changed and this test is no longer
    covering the trap it was written for.
    """
    client = client_for(seeded, collector)
    app = create_app(seeded)
    with app.test_request_context("/static/dashboard.js"):
        direct = app.send_static_file("dashboard.js")
    assert direct.direct_passthrough is True
    with pytest.raises(RuntimeError):
        direct.get_data()

    response = client.get("/static/dashboard.js")

    assert response.status_code == 200
    line = collector.matched()[0]
    assert line["endpoint"] == "static"
    assert int(line["bytes"]) > 0


def test_flask_leaves_an_app_logger_below_info():
    """The negative control for the level: this is what `install` fixes.

    A deliberately unrelated logger name, so a sibling test that has already
    configured `awair.web` cannot make this one pass by inheritance.
    """
    control = flask.Flask("awair-timing-level-control")

    assert control.logger.isEnabledFor(logging.INFO) is False


def test_create_app_enables_info_on_the_timing_logger(seeded):
    app = create_app(seeded)

    assert app.logger.name == timing.LOGGER_NAME
    assert app.logger.isEnabledFor(logging.INFO) is True


def test_the_logger_carries_its_own_handler_in_a_production_topology(
    seeded, monkeypatch
):
    """Under systemd nothing configures root, so Flask's handler is the emitter.

    pytest attaches capture handlers to root, which satisfies Flask's
    `has_level_handler` check and stops it installing its own -- so asserting
    this without clearing them first would assert nothing. Cleared here, the
    topology is production's: no root handlers, and the line has to reach
    stderr on the strength of `app.logger`'s own.
    """
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setattr(logging.getLogger(timing.LOGGER_NAME), "handlers", [])

    app = create_app(seeded)

    assert flask.logging.default_handler in app.logger.handlers


def test_server_timing_header_matches_the_logged_duration(seeded, collector):
    """The header is how the browser separates the server from everything else.

    Server-side timing cannot see queue or transfer time. `Server-Timing` can
    be read in devtools next to the request's TTFB, and the difference between
    them is exactly the part this app is blind to.
    """
    client = client_for(seeded, collector)

    response = client.get("/api/series")

    header = response.headers["Server-Timing"]
    assert header.startswith("app;dur=")
    assert float(header.removeprefix("app;dur=")) == pytest.approx(
        float(collector.matched()[0]["dur_ms"])
    )


def test_a_request_over_the_threshold_logs_at_warning(seeded, collector, monkeypatch):
    monkeypatch.setenv(timing.SLOW_MS_ENV, "0")
    client = client_for(seeded, collector)

    client.get("/api/series")

    assert collector.records[0].levelno == logging.WARNING


def test_a_request_under_the_threshold_logs_at_info(seeded, collector, monkeypatch):
    monkeypatch.setenv(timing.SLOW_MS_ENV, "100000")
    client = client_for(seeded, collector)

    client.get("/api/series")

    assert collector.records[0].levelno == logging.INFO


def test_a_slow_line_survives_an_operator_resetting_the_level(
    seeded, collector, monkeypatch
):
    """The reason slow lines are WARNING rather than INFO with a marker."""
    monkeypatch.setenv(timing.SLOW_MS_ENV, "0")
    client = client_for(seeded, collector)
    logging.getLogger(timing.LOGGER_NAME).setLevel(logging.WARNING)

    client.get("/api/series")

    assert len(collector.matched()) == 1


def test_a_fast_line_is_what_that_level_reset_costs(seeded, collector, monkeypatch):
    """The complement, so the test above is not passing for a vacuous reason."""
    monkeypatch.setenv(timing.SLOW_MS_ENV, "100000")
    client = client_for(seeded, collector)
    logging.getLogger(timing.LOGGER_NAME).setLevel(logging.WARNING)

    client.get("/api/series")

    assert collector.matched() == []


def test_a_client_error_is_still_timed(seeded, collector):
    """A 400 is a response, and a slow 400 is still a slow request."""
    client = client_for(seeded, collector)

    assert client.get("/api/series?range=nonsense").status_code == 400

    assert collector.matched()[0]["status"] == "400"


def test_a_server_error_is_still_timed(tmp_path, collector):
    """The 500 path, which is the one worth timing most.

    An unopenable database 500s each `/api/*` route (`test_web_readonly.py`),
    and Flask runs `after_request` on the error path too -- so a request that
    died slowly is on the log rather than missing from it.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    client = create_app(str(blocker / "awair.db")).test_client()
    collector.records.clear()

    assert client.get("/api/latest").status_code == 500

    assert collector.matched()[0]["status"] == "500"


def test_bytes_is_a_dash_when_the_length_is_unknown(seeded, collector):
    """A streamed response has no `Content-Length`; the line still goes out."""
    app = create_app(seeded)

    @app.get("/_streamed")
    def streamed():
        return flask.Response((chunk for chunk in ("a", "b")), mimetype="text/plain")

    client = app.test_client()
    collector.records.clear()

    assert client.get("/_streamed").status_code == 200

    assert collector.matched()[0]["bytes"] == "-"


def test_a_missing_start_marker_warns_instead_of_raising(seeded, collector):
    """The hook must not be the thing that breaks a response.

    Reached by invoking the `after_request` callback in a request context that
    `before_request` never ran in -- which is what an exception raised by
    another `before_request` hook would produce.
    """
    app = create_app(seeded)
    collector.records.clear()
    with app.test_request_context("/api/latest"):
        response = flask.Response("x")
        for func in app.after_request_funcs[None]:
            response = func(response)

    assert response.status_code == 200
    assert collector.matched() == []
    assert any("timing start marker missing" in line for line in collector.lines)


def test_a_malformed_threshold_falls_back_and_says_so(seeded, collector, monkeypatch):
    """A typo in the env must not silently disarm the WARNING tier."""
    monkeypatch.setenv(timing.SLOW_MS_ENV, "quite slow")

    app = create_app(seeded)

    assert app.config["AWAIR_WEB_SLOW_MS"] == timing.DEFAULT_SLOW_MS
    assert any("quite slow" in line for line in collector.lines)


def test_the_threshold_comes_from_the_environment(seeded, monkeypatch):
    monkeypatch.setenv(timing.SLOW_MS_ENV, "250.5")

    assert create_app(seeded).config["AWAIR_WEB_SLOW_MS"] == 250.5


def test_a_negative_threshold_is_refused(seeded, collector, monkeypatch):
    """`-1` would make every request slow, which is a disarmed tier, not a tier."""
    monkeypatch.setenv(timing.SLOW_MS_ENV, "-1")

    app = create_app(seeded)

    assert app.config["AWAIR_WEB_SLOW_MS"] == timing.DEFAULT_SLOW_MS
    assert any("-1" in line for line in collector.lines)


def test_the_probe_covers_every_route_the_app_serves(seeded):
    """The committed probe is a measuring tool; a stale one under-reports.

    `docs/verification/response_time_probe.py` writes its endpoint list out by
    hand, because which endpoints take a `?range=` is not readable from the url
    map. This is the guard that keeps the hand-written list honest -- without
    it, a probe reporting on four routes out of five reads exactly like a probe
    reporting on all five.
    """
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "verification"
        / "response_time_probe.py"
    )
    spec = importlib.util.spec_from_file_location("response_time_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.assert_covers_every_route(create_app(seeded))


def test_timing_does_not_change_what_a_route_returns(seeded, collector):
    """The instrument is a `Server-Timing` header and a log line, nothing else."""
    client = client_for(seeded, collector)

    response = client.get("/api/latest")

    assert response.status_code == 200
    assert response.get_json()["reading"]["co2"] == 500


def test_web_module_installs_timing_rather_than_reimplementing_it(seeded, monkeypatch):
    """`awair.web` must route through `timing.install`, not hand-roll a hook.

    Without this, the module above could grow its own before/after pair and
    every test here would go on passing against a `timing` nobody calls. The
    spy is set on the `timing` module rather than imported into `web`, which
    is why `web` has to reach it by attribute at call time.
    """
    calls = []
    real = timing.install

    def spy(app):
        calls.append(app)
        return real(app)

    monkeypatch.setattr(timing, "install", spy)

    create_app(seeded)

    assert len(calls) == 1
