"""A crash-loop shorter than the threshold must still alert (#124).

`adopt_open_event` (#100) restores the `alerted` **latch** from a row on disk.
This is the other half: a process that died *before* reaching its threshold
opened no row, so there is nothing to adopt, and the run counter — which lives
only in memory — restarts at zero. `awairelement-outdoor.service` is
`Restart=always` / `RestartSec=30` against a threshold of 4 polls x 900 s, so a
crash-loop never accumulates four consecutive bad polls and a sustained upstream
outage is **entirely silent**. Indoors the same shape is narrower (`RestartSec=10`
against 10 x 30 s) but identical in kind.

The fix persists the run in `health_state`, one row per metric, overwritten in
place — the `fan_state` shape rather than the `alert_events` one, because
`alert_events` rows only exist *after* the threshold fires and that is precisely
the window this issue is about.

Every `main()`-lifetime test below is paired with a control that disables only
the adoption and asserts the same scenario alerts **zero** times, which is what
the ticket asks for: without it a scenario that quietly stopped reaching its
threshold would also produce "one alert" and the assertion would have measured
nothing.
"""

import json
import os
import signal
from datetime import UTC, datetime, timedelta

import pytest

from awair import db, monitor, outdoor, poller
from awair.monitor import DeviceHealth, OutdoorHealth
from tests._helpers import FakeNotifier

DEVICE = DeviceHealth.METRIC
OUTDOOR = OutdoorHealth.METRIC

# Long enough that nothing in this module trips the staleness rule by accident:
# every lifetime here runs in milliseconds.
INDOOR_INTERVAL = 30
OUTDOOR_INTERVAL = 900


def _t():
    return datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The two helpers, on their own
# --------------------------------------------------------------------------


def test_recording_a_run_persists_it(conn):
    health = OutdoorHealth()
    health.observe("error")

    assert monitor.record_health_run(health, conn, OUTDOOR, _t()) is True
    state = db.get_health_state(conn, OUTDOOR)
    assert (state["last_status"], state["run_length"]) == ("error", 1)
    assert state["observed_at"] == _t()


def test_recording_an_unchanged_run_does_not_write_again(conn):
    """Steady health is the overwhelming majority of polls and must stay free.

    The ticket's stated cost for this option was "it makes every poll a write".
    It does not: the run only moves on a non-inserting poll, so once the healthy
    no-op has been written once there is nothing further to say.
    """
    health = DeviceHealth()
    health.observe("inserted")
    assert monitor.record_health_run(health, conn, DEVICE, _t()) is True

    health.observe("inserted")
    assert (
        monitor.record_health_run(health, conn, DEVICE, _t() + timedelta(days=1))
        is False
    )
    assert db.get_health_state(conn, DEVICE)["observed_at"] == _t()


def test_adopting_a_run_restores_the_counter(conn):
    db.upsert_health_state(conn, OUTDOOR, "partial", 3, _t())
    health = OutdoorHealth()

    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 3
    assert health.unhealthy == 3


def test_adopting_is_a_no_op_when_nothing_was_persisted(conn):
    health = OutdoorHealth()
    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 0
    assert health.unhealthy == 0


def test_adopting_reads_only_its_own_metric(conn):
    """Two pollers, two processes, one database — the #94 rule, in a new place."""
    db.upsert_health_state(conn, OUTDOOR, "error", 3, _t())
    health = DeviceHealth()

    assert monitor.adopt_health_run(health, conn, DEVICE, _t(), INDOOR_INTERVAL) == 0
    assert health.errors == 0


@pytest.mark.parametrize(
    ("status", "restored", "other"),
    [("error", "errors", "duplicates"), ("duplicate", "duplicates", "errors")],
)
def test_the_indoor_run_is_restored_onto_the_counter_its_status_names(
    conn, status, restored, other
):
    """`DeviceHealth` keeps two runs; one persisted shape has to cover both.

    Restoring onto the wrong counter would be worse than not restoring: it
    would announce `stale` for a device that is unreachable, which sends a
    human to the wrong box.
    """
    db.upsert_health_state(conn, DEVICE, status, 7, _t())
    health = DeviceHealth()

    monitor.adopt_health_run(health, conn, DEVICE, _t(), INDOOR_INTERVAL)
    assert getattr(health, restored) == 7
    assert getattr(health, other) == 0


def test_a_run_nobody_has_observed_recently_is_discarded(conn):
    """A counter from three days ago is not evidence about now."""
    db.upsert_health_state(conn, OUTDOOR, "error", 3, _t() - timedelta(days=3))
    health = OutdoorHealth()

    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 0
    assert health.unhealthy == 0


def test_the_staleness_window_tracks_the_cadence_rather_than_restating_it():
    """The derivation has to be observable, or it is a coincidence.

    Bound at import, `2 * 900` and a literal `1800` are the same number to every
    test that can be written — the same argument `web._outdoor_carry_max_age_seconds`
    is built around (#109). Read per call, a test can move the cadence and watch
    the window follow.
    """
    assert monitor.health_run_max_age_seconds(900) == 2 * 900
    assert monitor.health_run_max_age_seconds(30) == 2 * 30
    assert monitor.health_run_max_age_seconds(1) == 2


def test_a_run_exactly_at_the_bound_is_still_adopted(conn):
    """Inclusive, deliberately — same call as carry-forward's bound (#109)."""
    age = timedelta(seconds=monitor.health_run_max_age_seconds(OUTDOOR_INTERVAL))
    db.upsert_health_state(conn, OUTDOOR, "error", 3, _t() - age)
    health = OutdoorHealth()

    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 3


def test_adopting_a_healthy_run_leaves_the_counter_alone(conn):
    db.upsert_health_state(conn, OUTDOOR, "inserted", 0, _t())
    health = OutdoorHealth()

    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 0
    assert health.unhealthy == 0


# --------------------------------------------------------------------------
# Real `main()` lifetimes against one DB — the ticket's Done-when.
#
# Each lifetime is ONE poll, which is strictly shorter than either threshold,
# so the run can only reach the threshold by surviving the restarts.
# --------------------------------------------------------------------------

FIXTURE_TEXT = (
    '{"timestamp": "2026-09-15T09:00:00.000Z", "score": 90, "dew_point": 10.0,'
    ' "temp": 22.0, "humid": 45.0, "abs_humid": 8.0, "co2": 500,'
    ' "co2_est": 500, "co2_est_baseline": 400, "voc": 100,'
    ' "voc_baseline": 100, "voc_h2_raw": 20, "voc_ethanol_raw": 20,'
    ' "pm25": 5, "pm10_est": 6}'
)

NWS_EMPTY = '{"type": "FeatureCollection", "features": []}'

WEATHER_TEXT = json.dumps(
    {
        "current": {
            "time": "2026-09-15T09:00",
            "interval": 900,
            "temperature_2m": 22.4,
            "relative_humidity_2m": 68,
            "wind_speed_10m": 3.2,
            "pressure_msl": 1013.2,
            "precipitation": 0.0,
            "weather_code": 3,
        }
    }
)
AIR_QUALITY_TEXT = json.dumps(
    {
        "current": {
            "time": "2026-09-15T09:00",
            "pm2_5": 5.6,
            "pm10": 8.1,
            "us_aqi": 32,
            "carbon_monoxide": 200,
            "ozone": 55,
        }
    }
)

OUTDOOR_THRESHOLD = 4


def _rows(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, metric, tier, closed_at FROM alert_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _open_rows(db_path):
    return [row for row in _rows(db_path) if row[3] is None]


def _persisted(db_path, metric):
    """`(last_status, run_length)` on disk, for asserting what a control reached."""
    conn = db.connect(db_path)
    try:
        state = db.get_health_state(conn, metric)
    finally:
        conn.close()
    return (state["last_status"], state["run_length"])


@pytest.fixture
def indoor_crash(monkeypatch, tmp_path):
    """Run one indoor `main()` lifetime of exactly `polls` failing polls.

    A real poll interval rather than the 0 the adoption tests use: the staleness
    bound is derived from the cadence, and a cadence of 0 makes every persisted
    run stale on arrival. One poll per lifetime means `stop.wait(interval)`
    never actually waits — the SIGTERM has already landed.
    """
    db_path = str(tmp_path / "indoor.db")
    monkeypatch.setenv("AWAIR_DB", db_path)
    monkeypatch.setenv("AWAIR_POLL_SECONDS", str(INDOOR_INTERVAL))
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")
    monkeypatch.setattr(poller, "Notifier", lambda **kw: FakeNotifier())

    def run(polls=1, status="error"):
        remaining = {"n": polls}

        def fetch():
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                os.kill(os.getpid(), signal.SIGTERM)
            if status == "error":
                raise OSError("simulated outage")
            return FIXTURE_TEXT

        monkeypatch.setattr(poller, "make_fetch", lambda url: fetch)
        poller.main([])
        return db_path

    return run


@pytest.fixture
def outdoor_crash(monkeypatch, tmp_path):
    """The same, for the poller where this is a lost alert rather than a narrow one."""
    db_path = str(tmp_path / "outdoor.db")
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", db_path)
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", str(OUTDOOR_INTERVAL))
    monkeypatch.setenv("AWAIR_OUTDOOR_HEALTH_POLLS", str(OUTDOOR_THRESHOLD))
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")
    monkeypatch.setattr(outdoor, "Notifier", lambda **kw: FakeNotifier())
    monkeypatch.setattr(
        outdoor.weather_alerts, "make_fetch", lambda url, agent: lambda: NWS_EMPTY
    )

    def run(polls=1, status="error"):
        remaining = {"n": polls}

        def weather():
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                os.kill(os.getpid(), signal.SIGTERM)
            if status == "error":
                raise OSError("simulated Open-Meteo outage")
            return WEATHER_TEXT

        monkeypatch.setattr(
            outdoor,
            "make_fetch",
            lambda url: (
                weather if "air-quality" not in url else (lambda: AIR_QUALITY_TEXT)
            ),
        )
        outdoor.main()
        return db_path

    return run


def test_an_outdoor_crash_loop_through_an_outage_alerts(
    outdoor_crash, restore_signal_handlers
):
    """Four lifetimes of one poll each: no lifetime ever reaches the threshold."""
    for _ in range(OUTDOOR_THRESHOLD):
        db_path = outdoor_crash()

    rows = _open_rows(db_path)
    assert len(rows) == 1, _rows(db_path)
    assert rows[0][2] == "unreachable"


def test_the_same_outdoor_crash_loop_alerts_zero_times_without_the_fix(
    outdoor_crash, monkeypatch, restore_signal_handlers
):
    """The control the ticket asks for.

    Disables only the run adoption — the poll count, the threshold and the
    crash cadence are identical. This is the defect as it stands on `main`: a
    sustained upstream outage, entirely silent.
    """
    monkeypatch.setattr(outdoor, "adopt_health_run", lambda *a, **k: 0)
    for _ in range(OUTDOOR_THRESHOLD):
        db_path = outdoor_crash()

    assert _rows(db_path) == []
    # And for the right reason. An empty table is also what a scenario that
    # quietly stopped reaching its threshold produces, so assert the state the
    # control is named for: every lifetime started its count from zero.
    assert _persisted(db_path, OUTDOOR) == ("error", 1)


def test_an_indoor_crash_loop_through_an_outage_alerts(
    indoor_crash, restore_signal_handlers
):
    for _ in range(DeviceHealth().threshold):
        db_path = indoor_crash()

    rows = _open_rows(db_path)
    assert len(rows) == 1, _rows(db_path)
    assert rows[0][2] == "unreachable"


def test_the_same_indoor_crash_loop_alerts_zero_times_without_the_fix(
    indoor_crash, monkeypatch, restore_signal_handlers
):
    monkeypatch.setattr(poller, "adopt_health_run", lambda *a, **k: 0)
    for _ in range(DeviceHealth().threshold):
        db_path = indoor_crash()

    assert _rows(db_path) == []
    assert _persisted(db_path, DEVICE) == ("error", 1)


def test_a_healthy_poll_after_the_crash_loop_clears_the_persisted_run(
    outdoor_crash, restore_signal_handlers
):
    """Recovery has to reach disk too, or the next restart re-adopts a dead run."""
    for _ in range(OUTDOOR_THRESHOLD - 1):
        db_path = outdoor_crash()
    outdoor_crash(status="ok")

    conn = db.connect(db_path)
    try:
        state = db.get_health_state(conn, OUTDOOR)
    finally:
        conn.close()
    assert (state["last_status"], state["run_length"]) == ("inserted", 0)


def test_the_crash_loop_alerts_once_and_not_once_per_restart(
    outdoor_crash, restore_signal_handlers
):
    """The run survives, and so must the latch — otherwise this fires forever.

    Restoring the counter without `adopt_open_event` (#100) would make every
    subsequent restart open another row: the run is already at the threshold and
    the latch is clear. The two fixes are halves of one behaviour, which is why
    this branch is stacked on that one.
    """
    for _ in range(OUTDOOR_THRESHOLD + 3):
        db_path = outdoor_crash()

    assert len(_rows(db_path)) == 1, _rows(db_path)


# --------------------------------------------------------------------------
# Regressions this change could introduce, found in review
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["error", "duplicate"])
def test_an_indoor_run_restored_at_the_threshold_still_alerts(conn, status):
    """`DeviceHealth.observe` tested `== threshold`, which restoring can skip past.

    Reachable, and it is a regression this change would otherwise *introduce*:
    `record_health_run` commits before `db.open_event` runs, so a process that
    dies in between leaves the run at the threshold with no row to adopt. The
    counter then resumes at 10 and every later poll is 11, 12, ... — never
    equal — so the indoor `unreachable` alert is unreachable for the rest of
    the outage. Before this change the same crash reset the counter and the
    alert fired ten polls later.

    `OutdoorHealth` already tested `>=`; this is the asymmetry closing.
    """
    health = DeviceHealth()
    db.upsert_health_state(conn, DEVICE, status, health.threshold, _t())
    monitor.adopt_health_run(health, conn, DEVICE, _t(), INDOOR_INTERVAL)

    assert health.observe(status) is not None


def test_a_healthy_indoor_run_restores_nothing(conn):
    """`("inserted", 0)` is exactly what steady indoor health persists.

    Any restart within the staleness window of a healthy poll takes this
    branch, and it is the branch that governs whether `persisted` stays None —
    which is what makes the next poll correct the record.
    """
    db.upsert_health_state(conn, DEVICE, "inserted", 0, _t())
    health = DeviceHealth()

    assert monitor.adopt_health_run(health, conn, DEVICE, _t(), INDOOR_INTERVAL) == 0
    assert (health.errors, health.duplicates, health.persisted) == (0, 0, None)


def test_a_clock_that_has_run_backwards_does_not_adopt_an_ancient_run(conn):
    """A host without an RTC comes up behind real time, then jumps forward.

    A bare `age > max_age` admits every negative age, so a row from any
    distance in the past is adopted as though it were current.
    """
    db.upsert_health_state(conn, OUTDOOR, "error", 3, _t() + timedelta(days=3))
    health = OutdoorHealth()

    assert monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL) == 0


def test_the_run_reaches_disk_even_when_announcing_it_fails(conn):
    """Two docstrings and a GLOSSARY entry call this ordering load-bearing.

    Nothing pinned it: moving `record_health_run` below `db.open_event` left
    the whole suite green. The hazard is real — `db.open_event` can raise, and
    the pre-threshold polls this whole issue is about are never announced at
    all, so the run is the only evidence there is.
    """

    class Exploding:
        def send(self, message, title="", priority="default"):
            raise OSError("ntfy is down and so, probably, is everything else")

    health = OutdoorHealth(threshold=1)
    with pytest.raises(OSError):
        outdoor.handle_outdoor_health(
            conn, Exploding(), health, "error", _t(), OUTDOOR_INTERVAL
        )

    state = db.get_health_state(conn, OUTDOOR)
    assert (state["last_status"], state["run_length"]) == ("error", 1)


def test_the_alert_names_the_poll_count_and_not_just_a_wall_clock_span(conn):
    """A run may now outlive the process, so `threshold x interval` is not elapsed time.

    Four bad polls across four 30-second restarts span about two minutes and
    used to page "~1h of polls" — wrong by an order of magnitude, in the first
    thing a human reads when deciding how urgent this is. The window is still
    worth stating; it is a property of the threshold, not a measurement, and
    the message now says so.
    """

    class Recording:
        def __init__(self):
            self.messages = []

        def send(self, message, title="", priority="default"):
            self.messages.append(message)
            return True

        def close(self):
            pass

    notifier = Recording()
    health = OutdoorHealth(threshold=4)
    monitor.adopt_health_run(health, conn, OUTDOOR, _t(), OUTDOOR_INTERVAL)
    db.upsert_health_state(conn, OUTDOOR, "error", 3, _t())
    health.restore("error", 3)

    outdoor.handle_outdoor_health(conn, notifier, health, "error", _t(), 900)
    assert "4 consecutive polls" in notifier.messages[0]
    assert "~1h at this cadence" in notifier.messages[0]
