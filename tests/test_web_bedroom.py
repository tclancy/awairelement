"""The Bedroom card's data: `/api/bedroom-series` (scope 2026-10-31, step 4).

The bedroom head's own temperature, its setpoint, the bridge's cycle target and
its metered draw, on the head's own rows. What these tests hold:

- Temperatures move with `TEMPERATURE_UNIT`; the draw does not.
- The head temperature is hidden once the head has been off longer than
  `BEDROOM_TEMP_GRACE_SECONDS`. The setpoint, target and draw are never hidden.
- Dining rows never reach this endpoint, and nothing from `raw` does either.
- A gap in the rows becomes a break in the traces, not a straight line.
"""

from datetime import UTC, datetime, timedelta

import pytest

from awair import db, minisplit, web
from awair.series import break_gaps

RAW_MARKER = "RAW-STATUS-MARKER-a1b2"


def _row(
    cycle_at,
    *,
    head="bedroom",
    temp=14.0,
    switch="on",
    setpoint=12.78,
    target=12.78,
    delta_wh=5.0,
    window_seconds=600,
    raw=None,
):
    return {
        "head": head,
        "ts": cycle_at.isoformat(),
        "received_at": cycle_at.isoformat(),
        "window_start": (cycle_at - timedelta(seconds=window_seconds)).isoformat(),
        "window_close": cycle_at.isoformat(),
        "delta_wh": delta_wh,
        "watts_spot": 30.0,
        "temp": temp,
        "mode": "heat",
        "switch": switch,
        "setpoint": setpoint,
        "target": target,
        "target_source": "schedule",
        "decision_reason": "step",
        "temperature_at": None,
        "raw": raw,
    }


@pytest.fixture
def seed(tmp_path):
    db_path = tmp_path / "bedroom.db"

    def _seed(*rows):
        conn = db.connect(db_path)
        for row in rows:
            db.insert_minisplit_reading(conn, row)
        conn.close()
        return db_path

    db.connect(db_path).close()
    return _seed


@pytest.fixture
def client_for(tmp_path, monkeypatch):
    def _client(unit="C"):
        monkeypatch.setenv("TEMPERATURE_UNIT", unit)
        app = web.create_app(db_path=str(tmp_path / "bedroom.db"))
        app.testing = True
        return app.test_client()

    return _client


def _get(client, range_name="7d"):
    response = client.get(f"/api/bedroom-series?range={range_name}")
    assert response.status_code == 200
    return _without_anchors(response.get_json())


TRACES = ("t", "temp", "setpoint", "target", "watts")


def _without_anchors(payload):
    """The payload minus the two range-end stamps, after checking they are bare."""
    for name in TRACES[1:]:
        assert payload[name][0] is None and payload[name][-1] is None
    return {
        **payload,
        **{name: payload[name][1:-1] for name in TRACES},
        "anchors": (payload["t"][0], payload["t"][-1]),
    }


def _recent(minutes_ago):
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def test_an_empty_table_is_a_200_with_empty_traces(seed, client_for):
    seed()
    payload = _get(client_for())
    assert payload["t"] == []
    for name in ("temp", "setpoint", "target", "watts"):
        assert payload[name] == []
    assert payload["watts_unit"] == "W"


def test_the_trace_is_anchored_at_both_ends_of_the_range(seed, client_for):
    """So the card's x-axis spans the range, as every other card's does."""
    seed(_row(_recent(10)))
    payload = _get(client_for())
    since, until = payload["anchors"]
    assert until - since == pytest.approx(7 * 24 * 3600, abs=5)
    assert since < payload["t"][0] < until


def test_an_empty_table_still_draws_the_whole_range(seed, client_for):
    seed()
    since, until = _get(client_for())["anchors"]
    assert until - since == pytest.approx(7 * 24 * 3600, abs=5)


def test_dining_rows_never_reach_the_bedroom_card(seed, client_for):
    seed(_row(_recent(10), head="dining"), _row(_recent(5), head="dining"))
    assert _get(client_for())["t"] == []


def test_bedroom_rows_are_published_and_dining_ones_beside_them_are_not(
    seed, client_for
):
    seed(
        _row(_recent(10), temp=14.0),
        _row(_recent(10) + timedelta(seconds=1), head="dining", temp=30.0),
    )
    payload = _get(client_for())
    assert payload["temp"] == [14.0]


def test_temperatures_follow_the_display_unit_and_the_draw_does_not(seed, client_for):
    seed(_row(_recent(10), temp=15.0, setpoint=12.78, target=20.0, delta_wh=5.0))
    celsius = _get(client_for("C"))
    fahrenheit = _get(client_for("F"))
    assert fahrenheit["temp_unit_symbol"] == "°F"
    assert fahrenheit["temp"] == [pytest.approx(59.0)]
    assert fahrenheit["setpoint"] == [pytest.approx(55.0, abs=0.01)]
    assert fahrenheit["target"] == [pytest.approx(68.0)]
    assert fahrenheit["watts"] == celsius["watts"]


def test_the_draw_is_the_metered_mean_over_the_window_not_the_spot_reading(
    seed, client_for
):
    # 5 Wh over 600 s is 30 W; 10 Wh over 600 s is 60 W. The spot is 30.0 on both.
    seed(_row(_recent(10), delta_wh=10.0, window_seconds=600))
    assert _get(client_for())["watts"] == [60.0]


@pytest.mark.parametrize(
    ("off_minutes", "shown"),
    [(20, True), (30, True), (40, False)],
)
def test_the_head_temperature_hides_once_the_head_has_been_off_past_the_grace(
    seed, client_for, off_minutes, shown
):
    on_at = _recent(60)
    seed(
        _row(on_at, temp=14.0, switch="on"),
        _row(on_at + timedelta(minutes=off_minutes), temp=13.0, switch="off"),
    )
    payload = _get(client_for(), "7d")
    assert payload["temp"][-1] == (13.0 if shown else None)
    # The rule is the temperature's alone.
    assert payload["setpoint"][-1] == 12.78
    assert payload["target"][-1] == 12.78
    assert payload["watts"][-1] is not None


def test_an_on_reading_just_before_the_range_still_starts_the_grace(seed, client_for):
    since = datetime.now(UTC) - timedelta(days=7)
    seed(
        _row(since - timedelta(minutes=10), switch="on"),
        _row(since + timedelta(minutes=5), temp=13.0, switch="off"),
    )
    payload = _get(client_for(), "7d")
    assert payload["temp"] == [13.0]
    assert len(payload["t"]) == 1, "the look-back row must not be drawn"


def test_an_off_head_with_no_on_reading_in_reach_is_hidden(seed, client_for):
    seed(_row(_recent(10), switch="off"))
    assert _get(client_for())["temp"] == [None]


def test_an_unreported_switch_is_not_an_off_switch(seed, client_for):
    seed(_row(_recent(10), switch=None, temp=14.0))
    assert _get(client_for())["temp"] == [14.0]


def test_a_gap_in_the_rows_is_a_break_not_a_line(seed, client_for):
    first = _recent(120)
    seed(_row(first), _row(first + timedelta(minutes=60)))
    payload = _get(client_for(), "7d")
    assert len(payload["t"]) == 3
    assert payload["temp"][1] is None
    assert payload["watts"][1] is None


def test_raw_status_never_reaches_the_browser(seed, client_for):
    seed(_row(_recent(10), raw='{"components": {"note": "' + RAW_MARKER + '"}}'))
    response = client_for().get("/api/bedroom-series?range=7d")
    assert RAW_MARKER not in response.get_data(as_text=True)
    assert "raw" not in response.get_json()


def test_the_endpoint_never_reads_the_raw_column():
    assert "raw" not in web._BEDROOM_SERIES_COLUMNS
    assert set(web._BEDROOM_SERIES_COLUMNS) <= set(db.MINISPLIT_COLUMNS)


def test_it_reads_the_bedroom_head_by_the_shared_constant(
    monkeypatch, seed, client_for
):
    """The head name is `minisplit.BEDROOM`, the one the poller writes."""
    seed(_row(_recent(10)))
    monkeypatch.setattr(minisplit, "BEDROOM", "nobody")
    assert _get(client_for())["t"] == []


def test_an_unknown_range_is_refused(seed, client_for):
    seed()
    assert client_for().get("/api/bedroom-series?range=1y").status_code == 400


class TestBreakGaps:
    def test_rows_within_the_gap_pass_through(self):
        assert break_gaps([0, 10, 20], {"a": [1, 2, 3]}, 10) == {
            "t": [0, 10, 20],
            "a": [1, 2, 3],
        }

    def test_a_wide_gap_gets_one_none_stamp_just_after_the_earlier_row(self):
        assert break_gaps([0, 100], {"a": [1, 2], "b": [3, 4]}, 10) == {
            "t": [0, 1, 100],
            "a": [1, None, 2],
            "b": [3, None, 4],
        }

    def test_each_wide_gap_breaks_separately(self):
        result = break_gaps([0, 100, 105, 300], {"a": [1, 2, 3, 4]}, 10)
        assert result["t"] == [0, 1, 100, 105, 106, 300]
        assert result["a"] == [1, None, 2, 3, None, 4]

    def test_empty_in_empty_out(self):
        assert break_gaps([], {"a": []}, 10) == {"t": [], "a": []}


def test_every_payload_key_the_card_reads_is_one_the_endpoint_ships(seed, client_for):
    """Rename either side and the card draws nothing, silently."""
    import re
    from pathlib import Path

    from tests._helpers import strip_js_comments

    js = strip_js_comments(
        (Path(__file__).resolve().parent.parent / "static" / "dashboard.js").read_text()
    )
    body = js[js.index("function makeBedroomPlot") :]
    body = body[: body.index("async function load")]
    read = set(re.findall(r"payload\.(\w+)", body))
    bedroom_block = js[js.index("const BEDROOM_SERIES") :]
    bedroom_block = bedroom_block[: bedroom_block.index("];")]
    read |= set(re.findall(r'trace:\s*"(\w+)"', bedroom_block))
    assert {"t", "temp", "setpoint", "target", "watts"} <= read
    seed(_row(_recent(10)))
    shipped = set(_get(client_for()))
    assert read <= shipped, f"card reads {sorted(read - shipped)} the endpoint lacks"


def test_the_bedroom_card_is_on_the_page_after_precipitation(seed, client_for):
    seed()
    html = client_for().get("/").get_data(as_text=True)
    precipitation = html.index('data-outdoor="precipitation"')
    bedroom = html.index('data-minisplit="bedroom"')
    assert precipitation < bedroom
    assert html.rindex('<section class="card"') < bedroom, (
        "the Bedroom card must be the last card in the grid"
    )
