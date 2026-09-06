"""Clean shutdown on SIGTERM (#83).

These tests deliver real signals to the test process rather than calling the
handler directly. Registration is the whole feature — a handler that is written
but never installed would pass every indirect test and still leave systemd
logging `Failed with result 'exit-code'` on every restart.
"""

import os
import signal
import threading
import time

import pytest

from awair import shutdown


@pytest.fixture(autouse=True)
def _restore(restore_signal_handlers):
    """Every test here installs handlers; see the conftest fixture for why."""


def test_install_handler_starts_unset():
    # Nothing has asked us to stop yet, so a loop guarded by this runs.
    assert shutdown.install_handler().is_set() is False


def test_both_stop_signals_are_actually_registered():
    shutdown.install_handler()
    for sig in shutdown.STOP_SIGNALS:
        assert signal.getsignal(sig) not in (signal.SIG_DFL, signal.SIG_IGN)


@pytest.mark.parametrize("sig", shutdown.STOP_SIGNALS)
def test_a_delivered_signal_sets_the_event(sig):
    """The behaviour #83 is about, proven with the real signal.

    SIGTERM is what systemd sends on stop; SIGINT is Ctrl-C in a foreground
    run. Under the default disposition SIGTERM would kill this process, so
    reaching the assertion at all is part of the evidence.
    """
    stop = shutdown.install_handler()
    os.kill(os.getpid(), sig)
    assert stop.wait(5) is True


def test_a_long_wait_ends_the_moment_the_signal_lands():
    """Why this is an Event and not a flag checked after `time.sleep`.

    The poll loops sleep 30 s between polls. `time.sleep` resumes for the rest
    of its nap after a handler runs (PEP 475), so a flag would make every
    deploy wait out the remaining interval before the process exited — up to
    30 s of a restart spent doing nothing. `Event.wait` returns as soon as the
    event is set, so the wait here ends in milliseconds rather than 30 s.
    """
    stop = shutdown.install_handler()
    threading.Timer(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    started = time.monotonic()

    assert stop.wait(30) is True
    assert time.monotonic() - started < 5


def test_the_handler_says_what_it_received(caplog):
    stop = shutdown.install_handler()
    with caplog.at_level("INFO", logger="awair.shutdown"):
        os.kill(os.getpid(), signal.SIGTERM)
        assert stop.wait(5) is True
    assert "SIGTERM" in caplog.text
