"""The basement trace on `/api/series`: bucketed, then carried onto temp's grid.

Unlike the outdoor trace, the basement sensor publishes as often as the indoor
grid is fine (60 s), so on 7 d and 30 d a plain carry would sample one reading
per bucket stamp -- a single minute standing in for five or fifteen. The rows
are averaged into the same `bucket_seconds` as the indoor line first, then held
for two buckets.
"""

from datetime import UTC, datetime, timedelta

import pytest

from awair import basement_watch, db, web
from tests.test_web import _seed_db


@pytest.fixture(autouse=True)
def default_celsius(monkeypatch):
    monkeypatch.delenv("TEMPERATURE_UNIT", raising=False)


def _seed_basement(db_path, samples):
    """samples: [(datetime, celsius)] or [(datetime, celsius, percent)].

    The humidity is optional because most callers are about the temperature
    trace or the watch, and a sample that omits it stores NULL -- which is
    exactly what a box stores for every minute polled before #156 (and for
    every minute whose second GET failed).
    """
    conn = db.connect(db_path)
    for at, celsius, *humid in samples:
        db.insert_basement_reading(
            conn,
            {
                "ts": at.replace(second=0, microsecond=0).isoformat(),
                "received_at": at.isoformat(),
                "temp": celsius,
                "humid": humid[0] if humid else None,
            },
        )
    conn.close()


def _client(tmp_path, monkeypatch, unit="C", basement=()):
    monkeypatch.setenv("TEMPERATURE_UNIT", unit)
    db_path = tmp_path / f"basement-{unit}.db"
    _seed_db(db_path)
    _seed_basement(db_path, basement)
    app = web.create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client()


# --- the alignment, against hand-built grids ---


def _rows(*samples):
    """`basement_readings_since(..., _BASEMENT_SERIES_COLUMNS)`-shaped rows.

    `(epoch, celsius)` or `(epoch, celsius, percent)`, so a test about the
    temperature trace says nothing about the humidity column beside it (#156).
    """
    return [
        (epoch, *rest, None)[:3] if len(rest) == 1 else (epoch, *rest)
        for epoch, *rest in samples
    ]


def test_each_grid_stamp_takes_its_buckets_average_not_one_reading():
    rows = _rows((0, 10.0), (60, 12.0), (120, 14.0), (300, 20.0))
    on_grid = web._basement_temp_on_grid(rows, [0, 300], 300, "C")
    assert on_grid == [12.0, 20.0]


def test_one_missing_bucket_is_bridged_and_a_second_is_a_gap():
    rows = _rows((0, 15.0))
    grid = [0, 60, 120, 180]
    assert web._basement_temp_on_grid(rows, grid, 60, "C") == [
        15.0,
        15.0,
        15.0,
        None,
    ]


def test_the_carry_window_is_two_buckets_wide_at_every_range():
    for bucket_seconds in (60, 300, 900):
        window = web._BASEMENT_CARRY_BUCKETS * bucket_seconds
        assert web._basement_carry_max_age_seconds(bucket_seconds) == window
        held, gone = web._basement_temp_on_grid(
            _rows((0, 4.0)), [window, window + bucket_seconds], bucket_seconds, "C"
        )
        assert held == 4.0
        assert gone is None


def test_no_back_fill_before_the_first_reading():
    assert web._basement_temp_on_grid(_rows((600, 15.0)), [0, 600], 60, "C") == [
        None,
        15.0,
    ]


def test_converted_to_the_display_unit():
    assert web._basement_temp_on_grid(_rows((0, 10.0)), [0], 60, "F") == [50.0]


def test_no_rows_is_a_full_length_run_of_nulls():
    assert web._basement_temp_on_grid([], [0, 60, 120], 60, "C") == [None] * 3


# --- the humidity trace, off the same rows (#156) ---


def test_the_humidity_rides_the_same_buckets_and_is_not_converted():
    """Averaged per bucket like the temperature, and a percent in both units."""
    rows = _rows((0, 10.0, 60.0), (60, 12.0, 62.0), (300, 20.0, 50.0))
    assert web._basement_humid_on_grid(rows, [0, 300], 300) == [61.0, 50.0]


def test_a_minute_with_no_humidity_contributes_nothing_rather_than_raising():
    """Every row written before #156 has a NULL here, and `bucket` sums values.

    So the null is dropped before the bucketing, and a bucket of nothing but
    nulls is an empty bucket -- held for two, then a break, exactly as a minute
    the sensor never answered is.
    """
    rows = _rows((0, 10.0, None), (60, 12.0, 62.0), (120, 14.0, None))
    assert web._basement_humid_on_grid(rows, [0, 60, 120], 60) == [None, 62.0, 62.0]


def test_the_temperature_survives_a_column_of_nulls_beside_it():
    rows = _rows((0, 10.0, None), (60, 12.0, None))
    assert web._basement_temp_on_grid(rows, [0, 60], 60, "C") == [10.0, 12.0]
    assert web._basement_humid_on_grid(rows, [0, 60], 60) == [None, None]


# --- the endpoint ---


@pytest.mark.parametrize("range_name", ["today", "7d", "30d"])
def test_series_ships_the_basement_trace_on_the_indoor_temp_grid(
    tmp_path, monkeypatch, range_name
):
    now = datetime.now(UTC)
    client = _client(
        tmp_path,
        monkeypatch,
        basement=[(now - timedelta(minutes=m), 15.0) for m in range(1, 40)],
    )
    payload = client.get(f"/api/series?range={range_name}").get_json()
    assert len(payload["basement_temp"]) == len(payload["metrics"]["temp"]["t"])
    assert 15.0 in payload["basement_temp"]


def test_the_basement_trace_is_in_display_units(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    client = _client(
        tmp_path,
        monkeypatch,
        unit="F",
        basement=[(now - timedelta(minutes=m), 10.0) for m in range(1, 20)],
    )
    payload = client.get("/api/series?range=7d").get_json()
    assert 50.0 in payload["basement_temp"]
    assert 10.0 not in payload["basement_temp"]


def test_nothing_polled_yet_is_a_full_length_run_of_nulls(tmp_path, monkeypatch):
    payload = _client(tmp_path, monkeypatch).get("/api/series?range=7d").get_json()
    grid = payload["metrics"]["temp"]["t"]
    assert grid
    assert payload["basement_temp"] == [None] * len(grid)


@pytest.mark.parametrize("range_name", ["7d", "30d"])
def test_rows_are_read_from_one_carry_window_before_since(
    tmp_path, monkeypatch, range_name
):
    """As for the other feeds (#109): the reading current at the left edge
    was taken before it."""
    client = _client(tmp_path, monkeypatch)
    asked = []
    real = db.basement_readings_since

    def spy(conn, since, *columns):
        asked.append(since)
        return real(conn, since, *columns)

    monkeypatch.setattr(db, "basement_readings_since", spy)
    before = datetime.now(UTC)
    client.get(f"/api/series?range={range_name}")
    spec = web.RANGES[range_name]
    window = timedelta(
        seconds=web._basement_carry_max_age_seconds(spec["bucket_seconds"])
    )
    expected = before - timedelta(days=spec["days"]) - window
    # Two reads, not one: the chart's (first, this range's window) and the
    # basement watch's (second, its own fixed window -- #205,
    # `web._basement_watch`). Asserting a call *count* here would have made
    # this test a tripwire on every later reader of the same table, which is
    # not what #109 was about; what it was about is the window, below.
    assert len(asked) == 2
    assert abs((asked[0] - expected).total_seconds()) < 5
    # And the watch's window is its own, which is what keeps its verdict from
    # changing when the reader presses a range button.
    assert asked[1] == pytest.approx(
        before - basement_watch.history_lookback(), abs=timedelta(seconds=5)
    )


def test_the_range_read_returns_epoch_seconds_from_since(conn):
    at = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    for minute, temp in ((0, 15.0), (1, 15.5), (2, 16.0)):
        db.insert_basement_reading(
            conn,
            {
                "ts": at.replace(minute=minute).isoformat(),
                "received_at": at.replace(minute=minute).isoformat(),
                "temp": temp,
                "humid": None,
            },
        )
    assert db.basement_readings_since(conn, at.replace(minute=1)) == [
        (at.replace(minute=1).timestamp(), 15.5),
        (at.replace(minute=2).timestamp(), 16.0),
    ]
