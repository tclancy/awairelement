"""A poller restart mid-outage must not open a second alert_event (#100).

Both health trackers keep their run counter and their `alerted` latch **in
memory**. `db.get_open_events` returns at most one row per metric and the later
row wins, so before this fix a restart during an outage opened a second row,
recovery closed only the newer one, and the older stayed open on the dashboard
with no code path able to reach it. Reproduced against a real DB across two
simulated process lifetimes before anything here was written:

    rows after two lifetimes: [(1, 'outdoor', None), (2, 'outdoor', None)]
    get_open_events keys: ['outdoor'] -> id 2
    after recovery, still-open rows: [(1,)]

The fix is `monitor.adopt_open_event`: a starting poller seeds `alerted` from
whatever row is already open for its metric, so the latch survives the restart
the way the row already did. Pre-existing and inherited -- the indoor handler
has the identical shape and predates #94 -- so both are covered here.

**What this does NOT fix, deliberately.** A crash *before* the threshold is
reached opens no row, so there is nothing to adopt and the run counter still
restarts at zero. That is the other half of the ticket's "worse outdoors"
argument and it is out of this issue's Done-when, which asks only that a
restart not double-open and that recovery close the row that is open.
"""

import json
import os
import signal

import pytest

from awair import db, monitor, outdoor, poller
from awair.monitor import DeviceHealth, OutdoorHealth
from tests._helpers import FakeNotifier

DEVICE = "device"
OUTDOOR = "outdoor"


# --------------------------------------------------------------------------
# The helper, on its own
# --------------------------------------------------------------------------


def test_adopt_seeds_the_latch_from_the_open_row(conn):
    db.open_event(
        conn,
        metric=DEVICE,
        tier="unreachable",
        opened_at=_t(),
        value=None,
        baseline=None,
        threshold=None,
        notified=True,
    )
    health = DeviceHealth()
    assert health.alerted is None

    assert monitor.adopt_open_event(health, conn, DEVICE) == "unreachable"
    assert health.alerted == "unreachable"


def test_adopt_is_a_no_op_when_nothing_is_open(conn):
    health = DeviceHealth()
    assert monitor.adopt_open_event(health, conn, DEVICE) is None
    assert health.alerted is None


def test_adopt_reads_only_its_own_metric(conn):
    """The two pollers are separate processes against one DB.

    Adopting on the wrong key is the #94 defect in a new place: it would latch
    the outdoor poller onto the indoor poller's outage and suppress an outdoor
    alert that has not been sent.
    """
    db.open_event(
        conn,
        metric=OUTDOOR,
        tier="degraded",
        opened_at=_t(),
        value=None,
        baseline=None,
        threshold=None,
        notified=True,
    )
    health = DeviceHealth()
    assert monitor.adopt_open_event(health, conn, DEVICE) is None
    assert health.alerted is None


def test_adopt_carries_the_tier_rather_than_a_flag(conn):
    """`alerted` is read as a tier elsewhere, so a truthy sentinel is not enough."""
    db.open_event(
        conn,
        metric=OUTDOOR,
        tier="stale",
        opened_at=_t(),
        value=None,
        baseline=None,
        threshold=None,
        notified=True,
    )
    health = OutdoorHealth()
    monitor.adopt_open_event(health, conn, OUTDOOR)
    assert health.alerted == "stale"


def _t():
    from datetime import UTC, datetime

    return datetime(2026, 9, 13, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Two real `main()` lifetimes against one DB -- the ticket's Done-when
# --------------------------------------------------------------------------


def _rows(db_path):
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT id, metric, opened_at, closed_at FROM alert_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _open_rows(db_path):
    return [row for row in _rows(db_path) if row[3] is None]


@pytest.fixture
def indoor_outage(monkeypatch, tmp_path):
    """Run one indoor `main()` lifetime whose every poll fails.

    `DeviceHealth`'s threshold is not configurable from the environment, so the
    lifetime polls `threshold` times and then SIGTERMs itself -- the same
    clean-shutdown path #83 built, rather than an exception out of `main()`.
    """
    db_path = str(tmp_path / "indoor.db")
    monkeypatch.setenv("AWAIR_DB", db_path)
    monkeypatch.setenv("AWAIR_POLL_SECONDS", "0")
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")
    monkeypatch.setattr(poller, "Notifier", lambda **kw: FakeNotifier())

    def run(polls, status="error"):
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


FIXTURE_TEXT = (
    '{"timestamp": "2026-09-13T09:00:00.000Z", "score": 90, "dew_point": 10.0,'
    ' "temp": 22.0, "humid": 45.0, "abs_humid": 8.0, "co2": 500,'
    ' "co2_est": 500, "co2_est_baseline": 400, "voc": 100,'
    ' "voc_baseline": 100, "voc_h2_raw": 20, "voc_ethanol_raw": 20,'
    ' "pm25": 5, "pm10_est": 6}'
)


def test_indoor_restart_mid_outage_opens_one_row_not_two(
    indoor_outage, restore_signal_handlers
):
    db_path = indoor_outage(DeviceHealth().threshold)
    assert len(_open_rows(db_path)) == 1

    indoor_outage(DeviceHealth().threshold)  # systemd restarts it, outage ongoing
    assert len(_open_rows(db_path)) == 1, _rows(db_path)


def test_indoor_double_open_is_reachable_without_the_fix(
    indoor_outage, monkeypatch, restore_signal_handlers
):
    """Reachability control for the test above.

    Without it, a second lifetime that never reached its threshold -- a fetch
    stub that stopped raising, a SIGTERM one poll early -- would also leave one
    row, and the assertion would pass having measured nothing. Disabling only
    the adoption must produce the two rows the ticket reproduced.
    """
    db_path = indoor_outage(DeviceHealth().threshold)
    monkeypatch.setattr(poller, "adopt_open_event", lambda *a, **k: None)
    indoor_outage(DeviceHealth().threshold)
    assert len(_open_rows(db_path)) == 2, _rows(db_path)


def test_indoor_recovery_after_a_restart_closes_the_row_that_is_open(
    indoor_outage, restore_signal_handlers
):
    """And it closes the ORIGINAL row, so `opened_at` still spans the outage."""
    db_path = indoor_outage(DeviceHealth().threshold)
    ((opened_id, _, opened_at, _),) = _rows(db_path)

    indoor_outage(DeviceHealth().threshold)
    indoor_outage(1, status="ok")  # one healthy poll: recovery

    rows = _rows(db_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == opened_id
    assert rows[0][2] == opened_at
    assert rows[0][3] is not None


# --------------------------------------------------------------------------
# The same thing for the outdoor poller, which is where it bites hardest:
# `awairelement-outdoor.service` is Restart=always / RestartSec=30.
# --------------------------------------------------------------------------

NWS_EMPTY = '{"type": "FeatureCollection", "features": []}'

# Local rather than imported from `tests.test_outdoor`: these only ever have to
# be *insertable*, and an import would tie this file to fixtures that exist to
# pin that module's parsing contract.
WEATHER_TEXT = json.dumps(
    {
        "current": {
            "time": "2026-09-13T09:00",
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
            "time": "2026-09-13T09:00",
            "pm2_5": 5.6,
            "pm10": 8.1,
            "us_aqi": 32,
            "carbon_monoxide": 200,
            "ozone": 55,
        }
    }
)


@pytest.fixture
def outdoor_outage(monkeypatch, tmp_path):
    """Run one outdoor `main()` lifetime whose weather fetches all fail.

    A failing weather fetch is `"error"`, which `OutdoorHealth` tiers as
    `unreachable`. The threshold IS environment-configurable here, so a
    lifetime is two polls rather than ten.
    """
    db_path = str(tmp_path / "outdoor.db")
    monkeypatch.setenv("AWAIR_LAT", "43.1")
    monkeypatch.setenv("AWAIR_LON", "-70.9")
    monkeypatch.setenv("AWAIR_DB", db_path)
    monkeypatch.setenv("AWAIR_OUTDOOR_POLL_SECONDS", "0")
    monkeypatch.setenv("AWAIR_OUTDOOR_HEALTH_POLLS", str(OUTDOOR_THRESHOLD))
    monkeypatch.setenv("AWAIR_NTFY_TOKEN", "")
    monkeypatch.setattr(outdoor, "Notifier", lambda **kw: FakeNotifier())
    # Keep `main()` off api.weather.gov -- `outdoor.make_fetch` builds the two
    # Open-Meteo fetchers, and the loop builds a third through this one.
    monkeypatch.setattr(
        outdoor.weather_alerts, "make_fetch", lambda url, agent: lambda: NWS_EMPTY
    )

    def run(polls, status="error"):
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


OUTDOOR_THRESHOLD = 2


def test_outdoor_restart_mid_outage_opens_one_row_not_two(
    outdoor_outage, restore_signal_handlers
):
    db_path = outdoor_outage(OUTDOOR_THRESHOLD)
    assert len(_open_rows(db_path)) == 1

    outdoor_outage(OUTDOOR_THRESHOLD)  # RestartSec=30 later, outage ongoing
    assert len(_open_rows(db_path)) == 1, _rows(db_path)


def test_outdoor_double_open_is_reachable_without_the_fix(
    outdoor_outage, monkeypatch, restore_signal_handlers
):
    """Reachability control -- see the indoor one for why this is here."""
    db_path = outdoor_outage(OUTDOOR_THRESHOLD)
    monkeypatch.setattr(outdoor, "adopt_open_event", lambda *a, **k: None)
    outdoor_outage(OUTDOOR_THRESHOLD)
    assert len(_open_rows(db_path)) == 2, _rows(db_path)


def test_outdoor_recovery_after_a_restart_closes_the_row_that_is_open(
    outdoor_outage, restore_signal_handlers
):
    db_path = outdoor_outage(OUTDOOR_THRESHOLD)
    ((opened_id, _, opened_at, _),) = _rows(db_path)

    outdoor_outage(OUTDOOR_THRESHOLD)
    outdoor_outage(1, status="ok")  # one inserting poll: recovery

    rows = _rows(db_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == opened_id
    assert rows[0][2] == opened_at
    assert rows[0][3] is not None
