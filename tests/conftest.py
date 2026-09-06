"""Shared fixtures.

`conn` closes on teardown so leaked connections don't surface as
ResourceWarnings attributed to whatever test the GC happens to run in.
"""

import signal

import pytest

from awair import db


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
