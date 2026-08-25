"""Dashboard Flask app: series/events endpoints and the page itself."""

from datetime import UTC, datetime, timedelta

import pytest

from awair import db, web
from awair.web import METRIC_NAMES, create_app


@pytest.fixture(autouse=True)
def default_celsius(monkeypatch):
    """Isolate each test from any inherited TEMPERATURE_UNIT override."""
    monkeypatch.delenv("TEMPERATURE_UNIT", raising=False)


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _seed_db(db_path):
    """Seed one hour of readings, one open CO2 event, one closed ancient VOC event.

    Temp column set to exactly 22.5 C so unit-conversion tests can assert on a
    known value on either side of the API boundary.
    """
    conn = db.connect(db_path)
    now = datetime.now(UTC)
    rows = []
    for i in range(120):  # one hour of 30s readings, newest last
        ts = iso_z(now - timedelta(seconds=30 * (119 - i)))
        rows.append((ts, ts, 500 + i, 200, 5.0, 22.5, 45.0, 88))
    conn.executemany(
        "INSERT INTO readings (ts, received_at, co2, voc, pm25, temp, humid, score)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    db.open_event(
        conn,
        metric="co2",
        tier="ceiling",
        opened_at=now - timedelta(minutes=30),
        value=1400.0,
        baseline=500.0,
        threshold=1200.0,
        notified=True,
    )
    # Temp event with round Celsius values so F conversion (30 C → 86 F,
    # baseline 22 C → 71.6 F, threshold 28 C → 82.4 F) is easy to assert on.
    db.open_event(
        conn,
        metric="temp",
        tier="ceiling",
        opened_at=now - timedelta(minutes=20),
        value=30.0,
        baseline=22.0,
        threshold=28.0,
        notified=True,
    )
    ancient = db.open_event(
        conn,
        metric="voc",
        tier="relative",
        opened_at=now - timedelta(days=60),
        value=900.0,
        baseline=200.0,
        threshold=500.0,
        notified=True,
    )
    db.close_event(conn, ancient, closed_at=now - timedelta(days=59), notified=True)
    conn.close()


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "web.db"
    _seed_db(db_path)
    app = create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory: build a client with a specific TEMPERATURE_UNIT env override."""

    def _make(unit):
        monkeypatch.setenv("TEMPERATURE_UNIT", unit)
        db_path = tmp_path / f"web-{unit}.db"
        _seed_db(db_path)
        app = create_app(db_path=str(db_path))
        app.testing = True
        return app.test_client()

    return _make


def test_series_7d_buckets_all_metrics(client):
    payload = client.get("/api/series?range=7d").get_json()
    assert payload["bucket_seconds"] == 300
    assert set(payload["metrics"]) == set(METRIC_NAMES)
    co2 = payload["metrics"]["co2"]
    assert len(co2["t"]) >= 12  # an hour of data → ≥12 five-minute buckets
    assert co2["min"][0] <= co2["avg"][0] <= co2["max"][0]


def test_series_30d_uses_15_minute_buckets(client):
    payload = client.get("/api/series?range=30d").get_json()
    assert payload["bucket_seconds"] == 900


def test_series_rejects_unknown_range(client):
    assert client.get("/api/series?range=1y").status_code == 400


def test_series_today_uses_60s_buckets(client):
    # #46: single-day detail view — bucket down to 60 s (2 samples per bucket
    # at the 30 s poll cadence) so the finer resolution actually shows up.
    payload = client.get("/api/series?range=today").get_json()
    assert payload["bucket_seconds"] == 60
    assert set(payload["metrics"]) == set(METRIC_NAMES)


def test_since_for_today_lands_on_local_midnight():
    # `today` == since local midnight, not last 24 h — the value shown as
    # "today" on the dashboard should match what Tom sees on the wall clock.
    since = web._since_for({"days": "today", "bucket_seconds": 60})
    assert since.tzinfo is UTC
    now = datetime.now(UTC)
    # Since is at most 24 h ago and no later than now.
    assert now - timedelta(days=1) <= since <= now
    # And its local-tz projection is exactly midnight.
    local = since.astimezone()
    assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)


def test_events_returns_open_event_and_excludes_ancient(client):
    payload = client.get("/api/events?range=7d").get_json()
    events = payload["events"]
    metrics = {ev["metric"] for ev in events}
    assert metrics == {"co2", "temp"}  # ancient VOC event excluded
    co2 = next(ev for ev in events if ev["metric"] == "co2")
    assert co2["tier"] == "ceiling"
    assert co2["closed_at"] is None
    assert isinstance(co2["opened_at"], (int, float))


def test_dashboard_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for name in METRIC_NAMES:
        assert f'data-metric="{name}"' in html
    assert 'data-outdoor="temp"' in html
    assert 'data-outdoor="precipitation"' in html
    assert "uplot" in html


def test_dashboard_page_offers_all_three_range_buttons(client):
    # #46: Today button sits before 7d/30d — narrowest-to-widest reads
    # left-to-right.
    html = client.get("/").get_data(as_text=True)
    for value in ("today", "7d", "30d"):
        assert f'data-range="{value}"' in html
    assert html.index('data-range="today"') < html.index('data-range="7d"')


def test_dashboard_stamps_ceilings_for_alerting_metrics(client):
    # data-ceiling on the card feeds the JS reference-line plugin (#25).
    # Metrics without an alert ceiling (temp, humid, score) get no attribute.
    from awair import spikes

    html = client.get("/").get_data(as_text=True)
    for metric, cfg in spikes.METRICS.items():
        assert f'data-metric="{metric}" data-ceiling="{cfg.ceiling}"' in html
    for silent in ("temp", "humid", "score"):
        assert f'data-metric="{silent}" data-ceiling' not in html


# --- /api/outdoor-series ---


def _seed_outdoor(db_path, temps=(20.0, 22.4, 21.1), precips=None, pressures=None):
    """Three 15-min-cadence outdoor readings ~30 minutes apart.

    precips: optional matching iterable of mm rainfall per sample; None cells
    are stored as SQL NULL to exercise the "some intervals had no precip" case.
    pressures: optional matching iterable of hPa MSL pressure per sample; None
    cells are stored as SQL NULL (mirrors the precipitation-mid-flight case).
    """
    conn = db.connect(db_path)
    now = datetime.now(UTC)
    precips = precips if precips is not None else [None] * len(temps)
    pressures = pressures if pressures is not None else [None] * len(temps)
    assert len(precips) == len(temps)
    assert len(pressures) == len(temps)
    # strict=True: the three lists are asserted equal-length just above, so a
    # future caller that breaks that should get a loud error rather than have
    # zip() silently truncate the fixture it is building.
    for offset, (temp, precip, pressure) in enumerate(
        zip(temps, precips, pressures, strict=True)
    ):
        ts = (now - timedelta(minutes=15 * (len(temps) - 1 - offset))).isoformat()
        conn.execute(
            "INSERT INTO outdoor_readings"
            " (ts, received_at, temp, precipitation, pressure)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, ts, temp, precip, pressure),
        )
    conn.commit()
    conn.close()


def test_outdoor_series_returns_temp_buckets(client, tmp_path):
    db_path = tmp_path / "web.db"
    _seed_outdoor(db_path)
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    assert payload["bucket_seconds"] == 900
    temp = payload["metrics"]["temp"]
    assert temp["t"]
    for value in temp["avg"]:
        # 20.0 <= v <= 22.4 across the seeded samples
        if value is not None:
            assert 20.0 <= value <= 22.4


def test_outdoor_series_30d_uses_hourly_buckets(client):
    payload = client.get("/api/outdoor-series?range=30d").get_json()
    assert payload["bucket_seconds"] == 3600


def test_outdoor_series_rejects_unknown_range(client):
    assert client.get("/api/outdoor-series?range=1y").status_code == 400


def test_outdoor_series_today_uses_15min_buckets(client):
    # #46: outdoor source cadence is 15 min — no point sub-bucketing below
    # what Open-Meteo produces, so `today` matches the source (900 s).
    payload = client.get("/api/outdoor-series?range=today").get_json()
    assert payload["bucket_seconds"] == 900


def test_outdoor_series_empty_returns_empty_series(client):
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    temp = payload["metrics"]["temp"]
    precip = payload["metrics"]["precipitation"]
    pressure = payload["metrics"]["pressure"]
    assert temp == {"t": [], "avg": [], "min": [], "max": []}
    assert precip == {"t": [], "avg": [], "min": [], "max": []}
    assert pressure == {"t": [], "avg": [], "min": [], "max": []}


def test_outdoor_series_returns_pressure_in_inhg(client, tmp_path):
    # #42: pressure layered on the precipitation chart. Open-Meteo stores hPa;
    # API converts to inHg (33.8639 hPa/inHg) at the boundary to match the
    # imperial unit convention on the rest of the dashboard.
    db_path = tmp_path / "web.db"
    _seed_outdoor(
        db_path,
        temps=(20.0, 20.0),
        pressures=(1013.25, 1000.0),  # standard atm, storm-approaching
    )
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    pressure = payload["metrics"]["pressure"]
    values = [v for v in pressure["avg"] if v is not None]
    assert 29.92 in values  # 1013.25 hPa → 29.92 inHg
    assert 29.53 in values  # 1000.0 hPa → 29.53 inHg


def test_outdoor_series_pressure_none_stays_none(client, tmp_path):
    # Older rows have NULL pressure (column added mid-flight, same shape as
    # precipitation). Absent samples must not crash the bucketer or the
    # hPa→inHg map.
    db_path = tmp_path / "web.db"
    _seed_outdoor(
        db_path,
        temps=(20.0, 20.0, 20.0),
        pressures=(None, 1013.25, None),
    )
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    pressure = payload["metrics"]["pressure"]
    non_null = [v for v in pressure["avg"] if v is not None]
    assert non_null == [29.92]


def test_outdoor_series_returns_precipitation_in_inches(client, tmp_path):
    # #31: precipitation graph. Open-Meteo stores mm; API converts to inches
    # at the boundary so the dashboard's display unit stays consistent with
    # Tom's expected scale ("tenths of an inch").
    db_path = tmp_path / "web.db"  # match the `client` fixture's path
    _seed_outdoor(db_path, temps=(20.0, 20.0), precips=(25.4, 12.7))
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    precip = payload["metrics"]["precipitation"]
    values = [v for v in precip["avg"] if v is not None]
    assert 1.0 in values  # 25.4 mm → 1.0 in
    assert 0.5 in values  # 12.7 mm → 0.5 in


def test_outdoor_series_precipitation_none_stays_none(client, tmp_path):
    # Older rows have NULL precipitation (column added mid-flight). Absent
    # samples must not crash the bucketer or the mm→in map.
    db_path = tmp_path / "web.db"
    _seed_outdoor(db_path, temps=(20.0, 20.0, 20.0), precips=(None, 5.08, None))
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    precip = payload["metrics"]["precipitation"]
    non_null = [v for v in precip["avg"] if v is not None]
    assert non_null == [0.2]  # 5.08 mm → 0.2 in


def test_outdoor_series_includes_daily_events_when_coords_set(client, monkeypatch):
    # #32: sunrise/sunset markers computed from AWAIR_LAT/AWAIR_LON + AWAIR_TZ,
    # threaded into /api/outdoor-series so the dashboard renders them without a
    # second fetch.
    monkeypatch.setenv("AWAIR_LAT", "43.0")
    monkeypatch.setenv("AWAIR_LON", "-70.8")
    monkeypatch.setenv("AWAIR_TZ", "America/New_York")
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    assert "daily_events" in payload
    events = payload["daily_events"]
    assert len(events) > 0
    for ev in events:
        assert set(ev.keys()) == {"ts", "kind"}
        assert ev["kind"] in ("sunrise", "sunset")


def test_outdoor_series_daily_events_empty_when_coords_unset(client, monkeypatch):
    for var in ("AWAIR_LAT", "AWAIR_LON"):
        monkeypatch.delenv(var, raising=False)
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    assert payload["daily_events"] == []


def test_outdoor_series_honors_fahrenheit(make_client, tmp_path):
    client = make_client("F")
    db_path = tmp_path / "web-F.db"
    _seed_outdoor(db_path, temps=(0.0,))
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    assert payload["temp_unit_symbol"] == "°F"
    temp = payload["metrics"]["temp"]
    avg = [v for v in temp["avg"] if v is not None]
    assert avg == [32.0]  # 0 C → 32 F


# --- TEMPERATURE_UNIT env-var driven display conversion ---


def test_default_temperature_unit_is_celsius(client):
    payload = client.get("/api/series?range=7d").get_json()
    assert payload["temp_unit_symbol"] == "°C"
    temp_series = payload["metrics"]["temp"]
    # Seeded value is 22.5 C — round-trips through bucket avg unchanged.
    assert all(v == 22.5 for v in temp_series["avg"] if v is not None)


def test_dashboard_page_stamps_default_unit_symbol(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-temp-unit-symbol="°C"' in html


def test_fahrenheit_converts_series_and_symbol(make_client):
    client = make_client("F")
    payload = client.get("/api/series?range=7d").get_json()
    assert payload["temp_unit_symbol"] == "°F"
    temp_avg = [v for v in payload["metrics"]["temp"]["avg"] if v is not None]
    assert temp_avg, "expected non-empty temp series"
    # 22.5 C = 72.5 F exactly.
    assert all(v == 72.5 for v in temp_avg)
    # A non-temp metric is unaffected by the conversion path.
    humid_avg = [v for v in payload["metrics"]["humid"]["avg"] if v is not None]
    assert all(v == 45.0 for v in humid_avg)


def test_fahrenheit_converts_temp_event_fields(make_client):
    client = make_client("F")
    payload = client.get("/api/events?range=7d").get_json()
    assert payload["temp_unit_symbol"] == "°F"
    temp = next(ev for ev in payload["events"] if ev["metric"] == "temp")
    # 30 C → 86 F, 22 C → 71.6 F, 28 C → 82.4 F
    assert temp["peak_value"] == 86.0
    assert temp["baseline"] == 71.6
    assert temp["threshold"] == 82.4
    # Non-temp event stays untouched.
    co2 = next(ev for ev in payload["events"] if ev["metric"] == "co2")
    assert co2["peak_value"] == 1400.0
    assert co2["baseline"] == 500.0


def test_fahrenheit_dashboard_stamps_symbol(make_client):
    client = make_client("F")
    html = client.get("/").get_data(as_text=True)
    assert 'data-temp-unit-symbol="°F"' in html


def test_kelvin_converts_series(make_client):
    client = make_client("K")
    payload = client.get("/api/series?range=7d").get_json()
    assert payload["temp_unit_symbol"] == "K"
    # 22.5 C = 295.65 K
    assert all(v == 295.65 for v in payload["metrics"]["temp"]["avg"] if v is not None)


def test_invalid_temperature_unit_fails_at_startup(monkeypatch, tmp_path):
    """Typos in the env var raise at create_app rather than silently defaulting."""
    monkeypatch.setenv("TEMPERATURE_UNIT", "R")
    with pytest.raises(ValueError, match="TEMPERATURE_UNIT"):
        create_app(db_path=str(tmp_path / "unused.db"))


# --- /api/latest (#70) ------------------------------------------------------
#
# The house hub polls this every ~5 minutes and stores nothing. Three of the
# tests below guard traps the ticket named explicitly, and each one guards a
# failure that is silent in production: a temperature off by 30 with nothing in
# the payload to say so, a staleness clock that can never fire, and this app's
# notification bookkeeping becoming someone else's contract.


@pytest.fixture
def make_raw_client(tmp_path):
    """Factory: a client over a DB containing exactly what `seed` writes.

    The shared `client` fixture seeds an hour of readings, which is the wrong
    shape for the empty-table and clock-divergence cases below.
    """

    def _make(name, seed=None):
        db_path = tmp_path / f"raw-{name}.db"
        conn = db.connect(db_path)
        if seed is not None:
            seed(conn)
        conn.close()
        app = create_app(db_path=str(db_path))
        app.testing = True
        return app.test_client()

    return _make


def _insert_reading(conn, *, ts, received_at, **values):
    columns = ("ts", "received_at", *values)
    conn.execute(
        f"INSERT INTO readings ({', '.join(columns)})"
        f" VALUES ({', '.join('?' * len(columns))})",
        (ts, received_at, *values.values()),
    )
    conn.commit()


def test_latest_returns_the_newest_reading_with_both_clocks(client):
    payload = client.get("/api/latest").get_json()
    reading = payload["reading"]
    assert set(reading) == {"ts", "received_at", *web.LATEST_METRICS}
    # _seed_db writes co2 as 500 + i over 120 rows, newest last.
    assert reading["co2"] == 619
    assert reading["ts"].endswith("Z")
    assert reading["received_at"].endswith("Z")


def test_latest_is_always_celsius_whatever_the_display_unit_says(make_client):
    """The trap #70 names first, and the one with no visible symptom.

    `_seed_db` writes temp as exactly 22.5 C. Every sibling endpoint would hand
    back 72.5 under `TEMPERATURE_UNIT=F`. This one must not: the consumer
    formats for itself, so inheriting the setting is a silent add-30 the day
    the config flips — the payload would still look entirely well-formed.
    """
    payload = make_client("F").get("/api/latest").get_json()
    assert payload["reading"]["temp"] == 22.5
    assert payload["temp_unit"] == "C"


def test_latest_does_not_convert_temp_event_values_either(make_client):
    """The same trap one layer down.

    `/api/events` converts `peak_value`/`baseline`/`threshold` for a temp event
    (30/22/28 C → 86/71.6/82.4 F). Those fields carry the metric's own unit, so
    an endpoint that forgot them would be Celsius in one half of its payload
    and Fahrenheit in the other — worse than being wrong consistently.
    """
    payload = make_client("F").get("/api/latest").get_json()
    temp_event = next(e for e in payload["open_events"] if e["metric"] == "temp")
    assert temp_event["peak_value"] == 30.0
    assert temp_event["baseline"] == 22.0
    assert temp_event["threshold"] == 28.0


def test_latest_reports_the_two_clocks_separately_when_they_disagree(make_raw_client):
    """The trap that makes the staleness clock work at all.

    A device clock running fast is the case: `ts` is four hours in the future
    while `received_at` says the last poll landed four hours ago. If the
    consumer had only `ts`, `now - ts` would be *negative* — the card could
    never go stale and a dead poller would render healthy indefinitely. Both
    values must survive the boundary as distinct numbers.
    """
    now = datetime.now(UTC)
    fast_device = now + timedelta(hours=4)
    stale_arrival = now - timedelta(hours=4)

    def seed(conn):
        _insert_reading(
            conn,
            ts=iso_z(fast_device),
            received_at=stale_arrival.isoformat(),
            score=88,
            temp=22.5,
        )

    payload = make_raw_client("skew", seed).get("/api/latest").get_json()
    reading = payload["reading"]
    assert reading["ts"] != reading["received_at"]
    assert datetime.fromisoformat(reading["ts"]) > now
    assert datetime.fromisoformat(reading["received_at"]) < now


def test_latest_normalises_both_clocks_to_one_iso_spelling(make_raw_client):
    """`ts` is stored as `...Z` and `received_at` as `...+00:00` — two formats
    for two fields whose entire purpose is to be compared to each other."""
    now = datetime.now(UTC)

    def seed(conn):
        _insert_reading(
            conn, ts=iso_z(now), received_at=now.isoformat(), score=88, temp=22.5
        )

    reading = make_raw_client("iso", seed).get("/api/latest").get_json()["reading"]
    assert reading["ts"].endswith("Z")
    assert reading["received_at"].endswith("Z")
    assert "+00:00" not in reading["received_at"]


def test_latest_publishes_only_the_agreed_open_event_fields(client):
    """`db.get_open_events` carries `id`, `fans_engaged`, `notified_value` and
    `renotified_at` — this app's own notification bookkeeping. Publishing them
    would make them a contract with a consumer that has no use for them."""
    payload = client.get("/api/latest").get_json()
    assert payload["open_events"]
    for event in payload["open_events"]:
        assert set(event) == set(web._OPEN_EVENT_FIELDS)


def test_latest_omits_closed_events(client):
    """_seed_db closes an ancient VOC event; only co2 and temp are open."""
    payload = client.get("/api/latest").get_json()
    assert {e["metric"] for e in payload["open_events"]} == {"co2", "temp"}


def test_latest_publishes_open_event_times_in_the_same_iso_spelling(client):
    payload = client.get("/api/latest").get_json()
    for event in payload["open_events"]:
        assert event["opened_at"].endswith("Z")
        assert datetime.fromisoformat(event["opened_at"]).tzinfo is not None


def test_latest_on_an_empty_table_is_a_200_with_a_null_reading(make_raw_client):
    """Not a 5xx. The consumer tells "answered, but the poller is dead" from
    "unreachable", and those are different colours on its card — an error
    status collapses the first into the second."""
    response = make_raw_client("empty").get("/api/latest")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reading"] is None
    assert payload["open_events"] == []
    assert payload["temp_unit"] == "C"


def test_latest_hands_back_an_ancient_reading_rather_than_null(make_raw_client):
    """`latest_reading` is deliberately unbounded, unlike `latest_score`.

    A bounded query would return None for a reading a week old, which is the
    same answer as an empty table — and telling those apart is the whole reason
    the consumer is given `received_at`. Staleness is its judgement, not ours.
    """
    ancient = datetime.now(UTC) - timedelta(days=7)

    def seed(conn):
        _insert_reading(
            conn,
            ts=iso_z(ancient),
            received_at=ancient.isoformat(),
            score=88,
            temp=22.5,
        )

    reading = make_raw_client("ancient", seed).get("/api/latest").get_json()["reading"]
    assert reading is not None
    assert reading["score"] == 88


def test_latest_excludes_derived_and_raw_sensor_columns(client):
    """`LATEST_METRICS` is a whitelist, not `READING_COLUMNS`. `abs_humid`,
    `dew_point`, `co2_est*`, `voc_*_raw` and `pm10_est` are internal."""
    reading = client.get("/api/latest").get_json()["reading"]
    for internal in ("abs_humid", "dew_point", "co2_est", "voc_h2_raw", "pm10_est"):
        assert internal not in reading


def test_latest_reads_a_stored_timestamp_without_an_offset_as_utc(make_raw_client):
    """`_iso_utc` promises this in prose; nothing held it to the promise.

    Every writer in this app stamps UTC with an offset, so the naive branch is
    unreachable today — but it is the branch that runs if a row is ever
    restored, migrated, or hand-inserted, and getting it wrong is silent. A
    naive value handed to `astimezone` is read as *local* time, so on this
    machine a reading that arrived at noon UTC would publish as 16:00Z and the
    consumer would measure a four-hour-old poll as fresh — or as negative.
    """
    arrived = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

    def seed(conn):
        _insert_reading(
            conn,
            ts=iso_z(arrived),
            received_at=arrived.replace(tzinfo=None).isoformat(),
            score=88,
            temp=22.5,
        )

    reading = make_raw_client("naive", seed).get("/api/latest").get_json()["reading"]
    assert reading["received_at"] == "2026-08-25T12:00:00Z"


def test_latest_publishes_open_events_oldest_first(make_raw_client):
    """`db.get_open_events` has no ORDER BY, so its rows arrive in insertion
    order — which is the order events happened to be *written*, not the order
    they opened. Seeded here so the two disagree: a consumer rendering the list
    should not have the longest-standing problem show up second."""
    now = datetime.now(UTC)

    def seed(conn):
        _insert_reading(
            conn, ts=iso_z(now), received_at=now.isoformat(), score=88, temp=22.5
        )
        for metric, minutes in (("co2", 10), ("temp", 30)):
            db.open_event(
                conn,
                metric=metric,
                tier="ceiling",
                opened_at=now - timedelta(minutes=minutes),
                value=1400.0,
                baseline=500.0,
                threshold=1200.0,
                notified=True,
            )

    payload = make_raw_client("order", seed).get("/api/latest").get_json()
    assert [event["metric"] for event in payload["open_events"]] == ["temp", "co2"]
