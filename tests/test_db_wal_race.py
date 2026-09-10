"""Three units starting at once on a fresh database (#102).

`restart.sh` starts the indoor poller, the outdoor poller and the web app
together. On a database that is not yet in WAL, all three call `db.connect()`
within milliseconds of each other and race the journal-mode transition -- which
needs a brief exclusive lock and, unlike an ordinary write, **does not invoke
the busy handler**. One process wins and the others crash with `database is
locked` before they have executed a single statement.

Two consequences shape every test here:

- **`busy_timeout` does not help, in either order.** Setting it first is
  correct for the schema bootstrap that follows, but the transition itself
  fails fast regardless. Measured at 3-way concurrency on sqlite 3.49.1, the
  pragmas in either order fail a **variable** fraction of trials -- two
  measurement sessions on the same idle Mac spanned 6 to 23 per 30 -- and the
  retry fails **0**, in every run, at 3, 8, 16 and 32 workers, threads and
  processes alike. Only the zero is a constant; see `_set_journal_mode_wal`
  and the `RACE_TRIALS` note on why not to quote the other side.
  So a test that only asserts `busy_timeout` is set proves nothing about #102.
- **The window closes permanently once anyone wins.** `journal_mode` lives in
  the file header, so the second call on a given database is a no-op read that
  cannot contend. Every test that wants the race must build a *fresh* database;
  reusing one silently tests nothing, which is why the harness below makes a
  new temp file per trial.

The homelab has been in WAL since its first deploy, which is why this has never
bitten in production and why it needs a test rather than a monitoring alert.
"""

import sqlite3
import threading
import time
from typing import NamedTuple

import pytest

from awair import db

# Enough trials that a regression is not a coin flip. Stated as a BOUND rather
# than as an observed rate, because the observed rate is not a property of this
# code: two measurement sessions on the same idle Mac returned 7-23 and 6-17
# failing trials per 30, i.e. per-trial rates of 0.23-0.77 and 0.20-0.57. Any
# range written here is out of date on a busier box.
#
# The bound: at 25 trials a regression survives with probability under 1% for
# any per-trial rate above 0.17 (0.83**25 = 0.86%), and the lowest rate ever
# measured on a machine that races at all is 0.20. Sizing off the *mean* would
# have quoted 0.00001% and bought nothing a busy machine delivers.
#
# `test_the_race_harness_can_still_fail` is what keeps that arithmetic honest
# -- it asserts the harness really does provoke the failure, so a green run
# here cannot be a harness that stopped racing.
RACE_TRIALS = 25
RACE_WORKERS = 3

# Trials the `raceable` fixture spends deciding whether this machine can
# produce the race at all. Kept small: it runs once per module, and on a
# machine that races at all it almost always fails within a few trials.
PROBE_TRIALS = 12


class RaceResult(NamedTuple):
    """What one `_race` run observed.

    `paths` is carried alongside `errors` because the two failure modes of this
    harness need different evidence. "It never raced" shows up in `errors`;
    "every trial reused one database" does not, and cannot -- a reused database
    is in WAL after the first trial, so the remaining ones are no-op reads that
    quietly cannot contend. That mutant killed the control test only about one
    run in four, which is not a gate. `paths` makes it deterministic.
    """

    errors: list
    paths: list


def _race(tmp_path, opener, trials=RACE_TRIALS, workers=RACE_WORKERS):
    """Run `opener(path)` in `workers` threads on a fresh DB, `trials` times.

    Threads are released from a `Barrier` so they contend rather than merely
    overlap. The barrier raises the per-trial failure rate; it is not what
    creates the race, so removing it degrades this harness rather than
    disabling it.
    """
    errors = []
    paths = []
    for trial in range(trials):
        path = tmp_path / f"race-{trial}.db"
        paths.append(path)
        barrier = threading.Barrier(workers)

        # `path` and `barrier` are bound as defaults, not closed over. Each
        # iteration rebinds both, and a closure reading them late would see
        # whatever the *last* iteration left behind. It is harmless today only
        # because the threads are joined before the next iteration starts --
        # exactly the kind of works-by-accident that stops working the first
        # time someone lets trials overlap.
        def attempt(path=path, barrier=barrier):
            # `barrier.wait` is INSIDE the try. Outside it, a BrokenBarrierError
            # escapes to `threading.excepthook`, prints, and lands in nobody's
            # `errors` -- so the headline test reports green over trials in
            # which no connection was ever opened.
            try:
                barrier.wait(timeout=10)
                conn = opener(path)
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        stuck = [thread for thread in threads if thread.is_alive()]
        assert not stuck, f"{len(stuck)} worker thread(s) never finished trial {trial}"
    return RaceResult(errors, paths)


def _connect_unfixed(path):
    """`connect()` as it stood before #102 -- the shape the fix replaces.

    Kept literal rather than imported so this file can still prove the harness
    provokes the bug after the production code has been fixed. It is the
    control, and a control that changes with the subject is not one.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except BaseException:
        # The failing path is the *expected* one here, and leaking the
        # connection on it raises a ResourceWarning attributed to whatever test
        # the GC happens to run in -- the exact thing `tests/conftest.py`'s
        # `conn` fixture exists to avoid.
        conn.close()
        raise
    return conn


@pytest.fixture(scope="module")
def raceable(tmp_path_factory):
    """Whether this machine can produce the race at all, measured not assumed.

    The two tests below need genuine parallelism, and a small CI runner does
    not have it. Measured per-trial probability that the pre-#102 shape fails
    at 3 workers: 0.47 on an idle 8-core Mac, 0.29 on Linux under a 2-CPU CFS
    quota, **0.01 on two real cores**, and **0.00 on one**. Over 25 trials that
    is a control which goes red on correct code 78% of the time on a 2-core box
    and 100% of the time on a 1-core one -- and `ubuntu-latest` is 2 vCPU.

    A gate that cannot go green on a correct tree gets deleted, which is
    strictly worse than one that cannot go red. So the platform is asked first,
    and a machine that cannot race skips rather than fails. `-ra` is on in
    `pyproject.toml` addopts precisely so the skip reason is printed rather
    than silently swallowed -- if you see it in CI, these two tests told you
    nothing on that run and the deterministic tests below are the whole guard.
    """
    probe = _race(
        tmp_path_factory.mktemp("raceprobe"),
        _connect_unfixed,
        trials=PROBE_TRIALS,
    )
    return bool(probe.errors)


def test_every_trial_gets_a_fresh_database(tmp_path):
    """The deterministic half of the harness control -- never skipped.

    A reused database is in WAL from the first trial on, so trials 2..N are
    no-op reads that cannot contend however hard they try. That defeat is
    invisible in the error count (trial 1 still races, so `errors` is still
    non-empty roughly a quarter of the time) and it does not need parallelism
    to detect, so it is asserted separately and unconditionally.
    """
    result = _race(tmp_path, db.connect, trials=3)
    assert len(set(result.paths)) == 3, (
        f"{len(set(result.paths))} distinct databases across 3 trials -- the "
        "harness is reusing one, so most trials open a file already in WAL"
    )


def test_three_concurrent_connects_on_a_fresh_database_all_succeed(tmp_path, raceable):
    """The headline: what `restart.sh` does on a fresh install must not crash.

    Skipped where the platform cannot produce the race, because there it would
    be a vacuous green -- which is the reading this whole file is built to
    avoid. The mechanism is still covered unconditionally by the retry, budget
    and predicate tests below.
    """
    if not raceable:
        pytest.skip(
            f"this machine did not race in {PROBE_TRIALS} probe trials, so a "
            "green here would prove nothing about #102 (see the `raceable` "
            "fixture)"
        )
    result = _race(tmp_path, db.connect)
    assert not result.errors, (
        f"{len(result.errors)} of {RACE_TRIALS * RACE_WORKERS} concurrent "
        f"connect() calls failed on a fresh database: {result.errors[:3]} "
        "-- #102 is back"
    )


def test_the_race_harness_can_still_fail(tmp_path, raceable):
    """Reachability control for the test above.

    That test passes by observing no exceptions, so it also passes when it
    provokes no race -- a broken barrier, threads that serialise, or a database
    already in WAL. Running the identical harness against the pre-#102 shape
    and requiring it to fail is what separates "the fix works" from "nothing
    was measured".

    The one defeat it cannot distinguish is a machine with no parallelism to
    give, which is why `raceable` is consulted rather than asserted.
    """
    if not raceable:
        pytest.skip(
            f"this machine did not race in {PROBE_TRIALS} probe trials -- "
            "nothing here can be measured on it"
        )
    result = _race(tmp_path, _connect_unfixed)
    assert result.errors, (
        "the pre-#102 connect() survived "
        f"{RACE_TRIALS} x {RACE_WORKERS} concurrent opens on fresh databases, "
        "on a machine that raced during the probe. The harness has stopped "
        "provoking the race, so "
        "test_three_concurrent_connects_on_a_fresh_database_all_succeed is "
        "passing vacuously -- fix the harness before trusting it"
    )
    assert all(isinstance(exc, sqlite3.OperationalError) for exc in result.errors)


def test_busy_timeout_is_set_before_the_journal_mode_transition(tmp_path, monkeypatch):
    """Order, observed inside `db.connect()` rather than re-staged around it.

    `busy_timeout` cannot rescue the transition, but it does cover the SCHEMA
    bootstrap and the migration that follow -- both ordinary writes, both of
    which honour the busy handler. Setting it afterwards would leave those with
    no retry budget on a contended database.

    The connection factory is patched so the statements recorded are the ones
    **`connect()` issues**. An earlier version of this test built its own
    connection and executed the two pragmas itself in the order it wanted, then
    asserted that order -- which is a test of its own arrangement and passes
    however `connect()` is written. Moving `busy_timeout` after the transition
    in production left it green; that mutant is what this rewrite exists to
    kill.
    """
    executed = []
    real_connect = sqlite3.connect

    class Recording:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            executed.append(" ".join(str(sql).split()))
            return self._conn.execute(sql, *args)

        def executescript(self, sql):
            executed.append("EXECUTESCRIPT")
            return self._conn.executescript(sql)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **kw: Recording(real_connect(*a, **kw))
    )

    conn = db.connect(tmp_path / "ordered.db")
    conn.close()
    monkeypatch.undo()

    assert executed, "connect() issued no statements through the patched factory"
    pragmas = [sql.lower() for sql in executed if sql.lower().startswith("pragma")]
    busy = next((i for i, sql in enumerate(pragmas) if "busy_timeout" in sql), None)
    journal = next((i for i, sql in enumerate(pragmas) if "journal_mode" in sql), None)
    assert busy is not None, f"connect() never set busy_timeout: {pragmas}"
    assert journal is not None, f"connect() never set journal_mode: {pragmas}"
    assert busy < journal, (
        "busy_timeout must be set BEFORE the journal-mode transition, so the "
        f"schema bootstrap after it has a retry budget: {pragmas}"
    )
    # And the schema bootstrap must come after both, not between them.
    first_journal = next(sql for sql in executed if "journal_mode" in sql.lower())
    assert executed.index("EXECUTESCRIPT") > executed.index(first_journal)


def test_connect_closes_its_connection_when_the_transition_gives_up(
    tmp_path, monkeypatch
):
    """A `connect()` that raises must not leave the handle open.

    Latent before #102 -- any of the four statements in `connect()` could
    always raise -- but #102 widens the window on one of them from
    instantaneous to the whole retry budget. It stops being merely untidy once
    #73/PR #103 lands: `web._bootstrap_schema` swallows `sqlite3.Error`, so a
    bootstrap that gives up leaks one handle per gunicorn worker instead of
    taking the process down with it.

    Asserted on the connection object rather than by chasing a
    `ResourceWarning`, which the GC attributes to whichever test happens to be
    running when it fires -- exactly the misattribution `tests/conftest.py`'s
    `conn` fixture exists to prevent.
    """
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    monkeypatch.setattr(
        db, "_set_journal_mode_wal", lambda conn, **kw: (_ for _ in ()).throw(_Busy())
    )

    with pytest.raises(sqlite3.OperationalError):
        db.connect(tmp_path / "gives-up.db")

    assert len(opened) == 1, f"expected one connection, saw {len(opened)}"
    with pytest.raises(sqlite3.ProgrammingError):
        # A closed connection refuses to be used again; an open one would
        # happily answer this and the leak would go unnoticed.
        opened[0].execute("SELECT 1")


def test_connect_leaves_the_database_in_wal_with_a_busy_timeout(tmp_path):
    """The end state, independent of how it was reached."""
    conn = db.connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    conn.close()


class _Busy(sqlite3.OperationalError):
    """SQLITE_BUSY that reports its own result code.

    Subclassed rather than constructed, because `sqlite_errorcode` is set by
    the C layer and is **absent entirely** -- not 0 -- on an
    `OperationalError("database is locked")` built by hand. The production
    predicate reads it with `getattr(..., 0)`, so a hand-built double is
    classified non-retryable and a test using one would pass whether or not
    that predicate looks at the code at all. The real failure was measured at
    code 5 / `SQLITE_BUSY` in every one of 30 race trials, which is what this
    mirrors. (`test_an_error_carrying_no_sqlite_errorcode_is_not_retried`
    covers the absent case deliberately; this class is the present one.)
    """

    sqlite_errorcode = sqlite3.SQLITE_BUSY
    sqlite_errorname = "SQLITE_BUSY"

    def __init__(self):
        super().__init__("database is locked")


class _Locked(sqlite3.OperationalError):
    """SQLITE_LOCKED -- the other half of `_RETRYABLE_SQLITE_CODES`.

    Without this double, deleting `sqlite3.SQLITE_LOCKED` from the production
    frozenset is a mutation that survives the whole suite: every other lock
    fixture here carries code 5, so nothing exercises the code-6 branch and
    half the predicate is unguarded.
    """

    sqlite_errorcode = sqlite3.SQLITE_LOCKED
    sqlite_errorname = "SQLITE_LOCKED"

    def __init__(self):
        super().__init__("database table is locked")


class _CantOpen(sqlite3.OperationalError):
    """A non-lock OperationalError -- no amount of waiting fixes it."""

    sqlite_errorcode = 14  # SQLITE_CANTOPEN
    sqlite_errorname = "SQLITE_CANTOPEN"

    def __init__(self):
        super().__init__("unable to open database file")


# Well above what any test here drives the loop to -- 4 to 5 attempts at the
# real 50 ms budget (measured, n=40), ~15 on the virtual clock at 400 ms -- and
# low enough that a loop which ignores its deadline trips it in seconds rather
# than hanging the suite. See `_FailingConn.execute`.
_HARD_CAP = 50


class _RetriedPastItsBudget(RuntimeError):
    """The loop kept retrying long after any deadline should have stopped it."""


class _FailingConn:
    """Raises `error` for the first `failures` journal_mode attempts.

    Past `_HARD_CAP` attempts it raises `_RetriedPastItsBudget` instead. That
    is a test-harness backstop, not a behaviour under test: an unbounded retry
    loop is the realistic regression here, and without the cap the test that
    catches it would hang the suite instead of failing it -- which in CI is a
    timeout nobody attributes to this file.
    """

    def __init__(self, failures, error):
        self.failures = failures
        self.attempts = 0
        self.error = error

    def execute(self, sql, *args):
        self.attempts += 1
        if self.attempts > _HARD_CAP:
            raise _RetriedPastItsBudget(f"{self.attempts} attempts")
        if self.attempts <= self.failures:
            raise self.error
        return None


def test_the_busy_double_carries_a_real_lock_code():
    """The fixture-fidelity check for the three tests below.

    Each of them turns on `sqlite_errorcode & 0xFF`, so a double whose code
    silently reverted to 0 would make the retry tests pass for the wrong reason
    and the non-lock test pass for no reason at all.
    """
    assert _Busy().sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
    assert _CantOpen().sqlite_errorcode & 0xFF not in (
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    )


def test_a_locked_transition_is_retried_until_it_succeeds():
    """The mechanism, pinned without depending on thread timing."""
    conn = _FailingConn(failures=4, error=_Busy())
    db._set_journal_mode_wal(conn, timeout_ms=5000)
    assert conn.attempts == 5, (
        f"expected 4 retries then success, got {conn.attempts} attempts"
    )


def test_the_retry_is_bounded_and_gives_up():
    """The other half of the truth table: it must not wait forever.

    A database held by something that is never going to let go should raise, so
    systemd restarts the unit, rather than hanging it silently. Without this
    test an unbounded `while True` passes every other test in this file --
    including the race test, which a loop that never gives up passes especially
    well.

    `time.sleep` is deliberately *not* stubbed out here. Stubbing it away turns
    the loop into a spin that burns the 50 ms budget over ~a million attempts,
    so the `_HARD_CAP` backstop would fire on correct code and this test would
    go red on a tree with nothing wrong with it. Real sleeps put the attempt
    count at 4 to 5, well inside the cap.

    The bound is scaled to the budget rather than fixed. An earlier revision
    asserted `elapsed < 5` against a 50 ms budget -- 100x slack, which is
    enough to swallow a loop that has stopped respecting its deadline
    altogether. The slack that remains is for `time.sleep` overshoot, measured
    on this box at a median ~10 ms and a max ~68 ms per call.
    """
    budget_s = 0.05
    conn = _FailingConn(failures=10**9, error=_Busy())
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        db._set_journal_mode_wal(conn, timeout_ms=budget_s * 1000)
    elapsed = time.monotonic() - started
    assert elapsed < budget_s + 0.5, (
        f"the retry loop ignored its {budget_s * 1000:.0f} ms budget ({elapsed:.3f}s)"
    )
    assert conn.attempts > 1, "it gave up without retrying at all"


def test_no_single_sleep_outruns_the_cap_or_the_budget(monkeypatch):
    """The clamp and the backoff cap, gated -- on a virtual clock.

    `min(delay, remaining)` and `min(delay * 2, _MAX_RETRY_SLEEP)` are each
    other's only backstop, and all three mutations of that pair survived the
    rest of this file: dropping the clamp, dropping the cap, and dropping both
    -- which turns a 5000 ms budget into ~8.2 s. Nothing else here can see it,
    because `test_the_retry_is_bounded_and_gives_up` runs at a 50 ms budget,
    where the doubling never reaches the 50 ms cap and so never binds.

    Time is faked rather than slept so the assertions are exact and the test is
    instant. That is safe *here*, unlike in the test above, because nothing is
    being inferred from wall-clock duration -- the sleep values themselves are
    the thing under test, and the fake clock advances by exactly what the loop
    asks for. 400 ms is chosen because it is the smallest round budget at which
    the doubling overruns the cap, so an uncapped schedule is visible.
    """
    budget_s = 0.4
    clock = {"now": 1000.0}
    slept: list[float] = []

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", fake_sleep)

    conn = _FailingConn(failures=10**9, error=_Busy())
    with pytest.raises(sqlite3.OperationalError):
        db._set_journal_mode_wal(conn, timeout_ms=budget_s * 1000)

    assert slept, "it gave up without sleeping at all"
    assert max(slept) <= db._MAX_RETRY_SLEEP, (
        f"a single retry slept {max(slept) * 1000:.0f} ms against a "
        f"{db._MAX_RETRY_SLEEP * 1000:.0f} ms cap -- the backoff is unbounded, "
        "so a long budget becomes a much longer one"
    )
    # 1 microsecond of slack for float accumulation over ~15 additions -- a
    # correct schedule lands on the budget exactly and sums to 400.0000000001
    # ms. The regressions this guards against overshoot by up to one uncapped
    # sleep (50 ms here, 4096 ms with the cap gone too), so the epsilon is four
    # orders of magnitude below anything it must catch.
    assert sum(slept) <= budget_s + 1e-6, (
        f"the retry schedule slept {sum(slept) * 1000:.3f} ms against a "
        f"{budget_s * 1000:.0f} ms budget -- the final sleep is not clamped to "
        "what remains, so `connect()` outlives the timeout its caller set"
    )


def test_a_sqlite_locked_transition_is_also_retried():
    """The code-6 half of `_RETRYABLE_SQLITE_CODES`, which nothing else covers.

    Every other lock fixture in this file carries code 5, so deleting
    `sqlite3.SQLITE_LOCKED` from the production frozenset was a mutation that
    survived the entire suite -- half the predicate, unguarded. Distinct from
    `test_an_extended_busy_code_is_still_retried`, which proves the `& 0xFF`
    mask; this proves the *set* has two members.
    """
    conn = _FailingConn(failures=2, error=_Locked())
    db._set_journal_mode_wal(conn, timeout_ms=5000)
    assert conn.attempts == 3, (
        f"a SQLITE_LOCKED transition took {conn.attempts} attempts; expected "
        "2 retries then success"
    )


def test_a_non_lock_error_is_not_retried():
    """Waiting cannot fix a missing file or a bad disk, so do not spend 5s on it.

    Constructed to carry a non-lock `sqlite_errorcode`; a bare
    `OperationalError` would have code 0 and pass this test whether or not the
    production predicate looks at the code at all.
    """

    conn = _FailingConn(failures=10**9, error=_CantOpen())
    with pytest.raises(sqlite3.OperationalError):
        db._set_journal_mode_wal(conn, timeout_ms=5000)
    assert conn.attempts == 1, (
        f"a non-lock error was retried {conn.attempts} times; it should fail "
        "on the first attempt rather than burn the whole budget"
    )


def test_an_extended_busy_code_is_still_retried():
    """`sqlite_errorcode` carries extended codes, and they are not bare 5s.

    SQLite reports `primary | (sub << 8)`, so `SQLITE_BUSY_SNAPSHOT` is 517 and
    `SQLITE_BUSY_RECOVERY` is 261 -- neither equal to `SQLITE_BUSY`. Comparing
    the raw code instead of its low byte therefore treats a genuine lock as
    fatal, and a mutation dropping the `& 0xFF` mask passed every other test in
    this file. This is the one that fails.
    """

    class _BusySnapshot(sqlite3.OperationalError):
        sqlite_errorcode = 517  # SQLITE_BUSY_SNAPSHOT = 5 | (2 << 8)
        sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"

        def __init__(self):
            super().__init__("database is locked")

    assert _BusySnapshot().sqlite_errorcode != sqlite3.SQLITE_BUSY, (
        "the fixture must carry an EXTENDED code, or it cannot tell a masked "
        "comparison from an unmasked one"
    )
    assert _BusySnapshot().sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY

    conn = _FailingConn(failures=3, error=_BusySnapshot())
    db._set_journal_mode_wal(conn, timeout_ms=5000)
    assert conn.attempts == 4, (
        f"an extended busy code was not retried ({conn.attempts} attempts) -- "
        "the low-byte mask is gone"
    )


def test_an_error_carrying_no_sqlite_errorcode_is_not_retried():
    """An `OperationalError` SQLite did not raise has no code *at all*.

    Not zero -- absent. `sqlite3.OperationalError("database is locked")` built
    in Python carries no `sqlite_errorcode` attribute, so the production read
    must supply a default, and the default must be non-retryable: an error from
    outside SQLite is not a lock, and spending the whole budget on it delays a
    real failure by five seconds.

    Every other double in this file *sets* the attribute, so none of them
    exercises the default. A mutation flipping it to `SQLITE_BUSY` survived the
    whole round until this test existed.
    """
    hand_built = sqlite3.OperationalError("database is locked")
    assert not hasattr(hand_built, "sqlite_errorcode"), (
        "this Python reports a code on a hand-built OperationalError, so the "
        "default in `_set_journal_mode_wal` is no longer the thing under test"
    )

    conn = _FailingConn(failures=10**9, error=hand_built)
    with pytest.raises(sqlite3.OperationalError):
        db._set_journal_mode_wal(conn, timeout_ms=5000)
    assert conn.attempts == 1, (
        f"an error with no SQLite result code was retried {conn.attempts} "
        "times; absence must default to non-retryable"
    )
