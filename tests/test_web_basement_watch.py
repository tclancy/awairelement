"""The basement watch on `/api/series`, and its badge on the temp card.

`awair.basement_watch` owns the rule and `tests/test_basement_watch.py` tests
it. What is tested here is the wiring: the query windows, the unit conversion,
the one card that carries the badge, and the structural halves of the badge
that no Python test can otherwise see.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import basement_watch, db, web
from tests._helpers import strip_js_comments
from tests.test_web import _seed_db
from tests.test_web_basement import _seed_basement

REPO = Path(__file__).resolve().parent.parent
DASHBOARD_HTML = REPO / "templates" / "dashboard.html"
DASHBOARD_JS = REPO / "static" / "dashboard.js"

WARM_C = basement_watch.WARM_ABOVE_C + 0.5
COLD_OUTDOOR_C = 5.0


def _seed_outdoor(db_path, samples):
    """samples: [(datetime, celsius)] into outdoor_readings.

    Every column is bound, because `insert_outdoor_reading` binds the whole
    `OUTDOOR_COLUMNS` list by name and a partial dict is a
    `ProgrammingError`, not a row with nulls.
    """
    conn = db.connect(db_path)
    for at, celsius in samples:
        row = dict.fromkeys(db.OUTDOOR_COLUMNS)
        row["ts"] = at.isoformat()
        row["received_at"] = at.isoformat()
        row["temp"] = celsius
        db.insert_outdoor_reading(conn, row)
    conn.close()


def _covered_outdoor(until, celsius):
    """A gap-free outdoor series wide enough for the watch's own lookback."""
    step = timedelta(seconds=900)
    span = timedelta(seconds=basement_watch.attribution_lookback_seconds())
    stamps, at = [], until - span
    while at <= until:
        stamps.append((at, celsius))
        at += step
    return stamps


def _client(tmp_path, monkeypatch, unit="C", basement=(), outdoor=()):
    monkeypatch.setenv("TEMPERATURE_UNIT", unit)
    db_path = tmp_path / f"watch-{unit}.db"
    _seed_db(db_path)
    _seed_basement(db_path, basement)
    _seed_outdoor(db_path, outdoor)
    app = web.create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client()


def _warm_and_cold_now(hours_of_basement=2):
    """A house whose basement is warm in the watch window on a cold day."""
    now = datetime.now(UTC)
    basement = [
        (now - timedelta(minutes=m), WARM_C)
        for m in range(hours_of_basement * 60, -1, -1)
    ]
    return now, basement, _covered_outdoor(now, COLD_OUTDOOR_C)


# --- the payload ------------------------------------------------------------


def test_the_series_payload_carries_the_watch(tmp_path, monkeypatch):
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(tmp_path, monkeypatch, basement=basement, outdoor=outdoor)
    body = client.get("/api/series").get_json()
    assert body["basement_watch"]["status"] in {"on", "clear", "unknown"}


def _always_in_window(monkeypatch):
    """Widen both windows to the whole day, for the hour the suite runs in.

    The endpoint reads `datetime.now()`, so a test that wanted the ON path
    would otherwise pass only between midnight and 07:00 at weekends — and
    pass vacuously the rest of the time, which is the shape of a test that
    has quietly stopped covering its own subject. Widening the *rule's own
    constants* drives the real `in_watch_window` rather than stubbing it.
    """
    monkeypatch.setattr(basement_watch, "WEEKDAY_WINDOW", (0, 24))
    monkeypatch.setattr(basement_watch, "WEEKEND_WINDOW", (0, 24))


def _never_in_window(monkeypatch):
    """The reachability control for the pair above: same data, no window."""
    monkeypatch.setattr(basement_watch, "WEEKDAY_WINDOW", (0, 0))
    monkeypatch.setattr(basement_watch, "WEEKEND_WINDOW", (0, 0))


def test_a_warm_basement_on_a_cold_day_reads_on_with_both_numbers(
    tmp_path, monkeypatch
):
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(tmp_path, monkeypatch, basement=basement, outdoor=outdoor)
    _always_in_window(monkeypatch)
    watch = client.get("/api/series").get_json()["basement_watch"]
    assert watch["status"] == "on"
    assert watch["basement"] == pytest.approx(WARM_C)
    assert watch["outdoor_high"] == pytest.approx(COLD_OUTDOOR_C)
    assert watch["since"] is not None


def test_the_same_house_outside_the_window_reads_clear(tmp_path, monkeypatch):
    """Control: the ON above has to come from the window, not from the data."""
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(tmp_path, monkeypatch, basement=basement, outdoor=outdoor)
    _never_in_window(monkeypatch)
    watch = client.get("/api/series").get_json()["basement_watch"]
    assert watch["status"] == "clear"
    assert watch["outdoor_high"] is None


def test_an_empty_database_reads_unknown_rather_than_clear(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    watch = client.get("/api/series").get_json()["basement_watch"]
    assert watch["status"] == "unknown"
    assert watch["basement"] is None


def test_the_watchs_temperatures_are_converted_like_every_other(tmp_path, monkeypatch):
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(
        tmp_path, monkeypatch, unit="F", basement=basement, outdoor=outdoor
    )
    _always_in_window(monkeypatch)
    watch = client.get("/api/series").get_json()["basement_watch"]
    assert watch["basement"] == pytest.approx(WARM_C * 9 / 5 + 32, abs=0.01)
    # Both numbers, because `outdoor_high` is the one that is None on every
    # status but ON -- an assertion on `basement` alone passes unconverted
    # from a CLEAR verdict and says nothing about this boundary.
    assert watch["outdoor_high"] == pytest.approx(COLD_OUTDOOR_C * 9 / 5 + 32, abs=0.01)


@pytest.mark.parametrize("range_name", ["today", "7d", "30d"])
def test_the_watchs_verdict_does_not_follow_the_range_buttons(
    tmp_path, monkeypatch, range_name
):
    """The one block on this endpoint whose window is its own (#205)."""
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(tmp_path, monkeypatch, basement=basement, outdoor=outdoor)
    # On the ON path, so the comparison is between two real verdicts rather
    # than between two copies of the same silence.
    _always_in_window(monkeypatch)
    first = client.get("/api/series?range=today").get_json()["basement_watch"]
    again = client.get(f"/api/series?range={range_name}").get_json()["basement_watch"]
    assert first["status"] == "on", "the comparison needs a verdict, not silence"
    assert again["status"] == first["status"]
    assert again["outdoor_high"] == first["outdoor_high"]


def test_the_outdoor_read_reaches_back_past_the_oldest_trips_own_window(
    tmp_path, monkeypatch
):
    """A short outdoor query would read as an outage, not as a short query."""
    _now, basement, outdoor = _warm_and_cold_now()
    client = _client(tmp_path, monkeypatch, basement=basement, outdoor=outdoor)
    asked = []
    real = db.outdoor_readings_since

    def spy(conn, columns, since):
        asked.append((columns, since))
        return real(conn, columns, since)

    monkeypatch.setattr(db, "outdoor_readings_since", spy)
    before = datetime.now(UTC)
    client.get("/api/series")
    watch_reads = [
        since
        for columns, since in asked
        if since
        <= before
        - (basement_watch.history_lookback() + basement_watch.ATTRIBUTION_LOOKBACK)
    ]
    assert watch_reads, f"no read reached the watch's window; got {asked}"


# --- the badge --------------------------------------------------------------


@pytest.fixture(scope="module")
def html():
    return DASHBOARD_HTML.read_text()


@pytest.fixture(scope="module")
def js_code():
    return strip_js_comments(DASHBOARD_JS.read_text())


def test_exactly_one_card_carries_a_watch_element(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    page = client.get("/").get_data(as_text=True)
    assert page.count('class="watch"') == 1


def test_the_card_that_carries_it_is_the_one_the_app_names(tmp_path, monkeypatch):
    """Rendered from `web.WATCH_METRIC`, so moving the badge is one edit."""
    client = _client(tmp_path, monkeypatch)
    page = client.get("/").get_data(as_text=True)
    section = re.search(
        r'<section class="card" data-metric="([a-z0-9]+)"[^>]*>(?:(?!</section>).)*'
        r'class="watch"',
        page,
        re.S,
    )
    assert section, "no metric card carries the watch element"
    assert section.group(1) == web.WATCH_METRIC


def test_the_badge_starts_hidden_so_a_dead_fetch_shows_no_verdict(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    page = client.get("/").get_data(as_text=True)
    assert re.search(r'<p class="watch" hidden>\s*</p>', page)


def test_the_hidden_attribute_is_not_defeated_by_the_display_rule(html):
    """`display: flex` beats the `hidden` attribute's UA `display: none`.

    Without the `[hidden]` rule the badge renders on every page load with an
    empty sentence beside the amber square, which reads as a watch that is
    permanently on. Structural, because no Python test can see a cascade.
    """
    assert re.search(r"\.card \.watch\s*\{[^}]*display:\s*flex", html, re.S)
    assert re.search(r"\.card \.watch\[hidden\]\s*\{[^}]*display:\s*none", html, re.S)


def test_the_badge_is_only_drawn_for_the_on_status(js_code):
    """`clear` and `unknown` are both silence; neither may draw."""
    body = re.search(r"function renderWatch\([^)]*\)\s*\{(.*?)\n  \}", js_code, re.S)
    assert body, "renderWatch is gone"
    assert 'status === "on"' in body.group(1)
    assert "badge.hidden = !on" in body.group(1)


def test_the_badge_states_both_numbers_and_not_only_the_basement(js_code):
    body = re.search(r"function renderWatch\([^)]*\)\s*\{(.*?)\n  \}", js_code, re.S)
    assert "watch.basement" in body.group(1)
    assert "watch.outdoor_high" in body.group(1)


def test_the_badge_is_refreshed_from_the_payload_on_every_render(js_code):
    assert "renderWatch(card, seriesPayload.basement_watch" in js_code
