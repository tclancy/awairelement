"""Shared fixtures.

`conn` closes on teardown so leaked connections don't surface as
ResourceWarnings attributed to whatever test the GC happens to run in.
"""

import logging
import signal

import pytest

from awair import db, timing


@pytest.fixture(autouse=True)
def restore_timing_logger_level():
    """Leave the request-timing logger as the session found it (#141).

    `awair.timing.install` raises the level on a module-global logger --
    `logging.getLogger("awair.web")` is one object for the whole process -- so
    the first test to call `create_app` configures it for every test after.

    Insurance, not a fix for an observed failure: removing this fixture leaves
    the suite green today, because the one test asserting what an
    *unconfigured* app does uses a deliberately unrelated logger name rather
    than relying on the order tests run in. It is here so the next test that
    asserts something about this logger's level is not quietly answered by
    whichever test ran before it.
    """
    logger = logging.getLogger(timing.LOGGER_NAME)
    saved = logger.level
    yield
    logger.setLevel(saved)


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def restore_signal_handlers():
    """Give pytest its signal handlers back after a test installs its own.

    Signal dispositions are process-global, so a test that calls
    `shutdown.install_handler` (or `main()`, which does) leaves SIGINT pointing
    at a stale closure for the rest of the session — Ctrl-C would set a dead
    Event instead of raising KeyboardInterrupt. Restoring in a fixture rather
    than the test body means a failing assertion still cleans up.
    """
    from awair.shutdown import STOP_SIGNALS

    saved = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)
