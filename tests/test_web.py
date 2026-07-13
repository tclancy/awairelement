"""Dashboard Flask app: series/events endpoints and the page itself."""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db
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
    now = datetime.now(timezone.utc)
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


def _seed_outdoor(db_path, temps=(20.0, 22.4, 21.1), precips=None):
    """Three 15-min-cadence outdoor readings ~30 minutes apart.

    precips: optional matching iterable of mm rainfall per sample; None cells
    are stored as SQL NULL to exercise the "some intervals had no precip" case.
    """
    conn = db.connect(db_path)
    now = datetime.now(timezone.utc)
    precips = precips if precips is not None else [None] * len(temps)
    assert len(precips) == len(temps)
    for offset, (temp, precip) in enumerate(zip(temps, precips)):
        ts = (now - timedelta(minutes=15 * (len(temps) - 1 - offset))).isoformat()
        conn.execute(
            "INSERT INTO outdoor_readings (ts, received_at, temp, precipitation)"
            " VALUES (?, ?, ?, ?)",
            (ts, ts, temp, precip),
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


def test_outdoor_series_empty_returns_empty_series(client):
    payload = client.get("/api/outdoor-series?range=7d").get_json()
    temp = payload["metrics"]["temp"]
    precip = payload["metrics"]["precipitation"]
    assert temp == {"t": [], "avg": [], "min": [], "max": []}
    assert precip == {"t": [], "avg": [], "min": [], "max": []}


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
