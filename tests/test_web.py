"""Dashboard Flask app: series/events endpoints and the page itself."""

from datetime import datetime, timedelta, timezone

import pytest

from awair import db
from awair.web import METRIC_NAMES, create_app


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "web.db"
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
    db.open_event(conn, metric="co2", tier="ceiling", opened_at=now - timedelta(minutes=30),
                  value=1400.0, baseline=500.0, threshold=1200.0, notified=True)
    ancient = db.open_event(conn, metric="voc", tier="relative",
                            opened_at=now - timedelta(days=60),
                            value=900.0, baseline=200.0, threshold=500.0, notified=True)
    db.close_event(conn, ancient, closed_at=now - timedelta(days=59), notified=True)
    conn.close()
    app = create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client()


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
    assert len(events) == 1
    event = events[0]
    assert event["metric"] == "co2"
    assert event["tier"] == "ceiling"
    assert event["closed_at"] is None  # still open
    assert isinstance(event["opened_at"], (int, float))  # epoch seconds


def test_dashboard_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for name in METRIC_NAMES:
        assert f'data-metric="{name}"' in html
    assert "uplot" in html
