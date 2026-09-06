"""Clean shutdown for the two long-running poll loops (#83).

systemd stops a service by sending SIGTERM. With no handler, Python's default
disposition kills the process mid-loop and it exits non-zero; systemd records
`Result=exit-code` and writes `Failed with result 'exit-code'` to the journal —
on *every* restart. A poller behaving exactly as designed then looks identical,
in the one place you would go looking, to one that crashed. `itguy logs` runs
at warning level for systemd-shape apps, so that false failure is most of what
it shows.

`awairelement-web` never had this problem: gunicorn installs its own handler
and exits 0. This module gives the two stdlib loops the same manners.

Deliberately narrow. Nothing here touches fan state: `fan_state` is written on
every command and the next start reconciles from it, so a *restart* — the only
case a deploy creates — already resolves itself within one poll. The uncovered
case is a deliberate `stop` that never returns, which strands the fans; that is
a visibility problem first and belongs to #84.
"""

import logging
import signal
import threading

log = logging.getLogger("awair.shutdown")

#: SIGTERM is what systemd sends on stop; SIGINT is Ctrl-C in a foreground run.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def install_handler(signals: tuple = STOP_SIGNALS) -> threading.Event:
    """Register the stop signals and return the Event they set.

    An `Event` rather than a plain flag because the callers sleep between polls.
    `time.sleep` resumes for the remainder of its nap once a handler returns
    (PEP 475), so a flag checked after sleeping would leave every deploy waiting
    out the rest of the poll interval — up to 30 s of a restart spent doing
    nothing. `Event.wait(interval)` returns the moment the event is set, so the
    loop stops promptly and still never abandons a poll half-finished.
    """
    stop = threading.Event()

    def _request_stop(signum, _frame) -> None:
        log.info(
            "%s received — finishing this poll and exiting",
            signal.Signals(signum).name,
        )
        stop.set()

    for sig in signals:
        signal.signal(sig, _request_stop)
    return stop
