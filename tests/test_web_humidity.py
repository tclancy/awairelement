"""`/api/humidity` and the two cards it draws (#161).

The properties here are the ones that would fail silently otherwise:

* **The dew point card covers every feed**, derived from `awair.feeds.FEEDS`
  rather than from a list somebody keeps up to date. A seventh feed with no
  colour fails a test here instead of quietly not appearing.
* **The target line moves with the outdoor temperature**, and holds the warm
  target when there is no outdoor reading.
* **The week's hours below target do not follow the range buttons.** A figure
  that shrank when somebody pressed "Today" would be a different statistic
  under the same label.
* **Both cards' payload keys are the ones the browser reads.** Rename either
  side and uPlot draws a chart with an undefined series -- no error, no line.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import db, feeds, main_zone, web
from awair.web import create_app
from tests._helpers import strip_js_comments

REPO = Path(__file__).resolve().parent.parent
DASHBOARD_JS = (REPO / "static" / "dashboard.js").read_text()
DASHBOARD_HTML = (REPO / "templates" / "dashboard.html").read_text()

NOW = datetime.now(UTC).replace(microsecond=0)


def _iso(at):
    return at.isoformat()


def seed(path, *, minutes=180, outdoor_c=-2.0, humidifier=True, **rows):
    """A database holding every feed over the last `minutes`, at its own cadence.

    Every feed, because the dew point card draws every feed and a fixture
    that seeded four of six would leave the coverage assertions testing less
    than they read.

    **Each feed at something near its real interval**, which matters for one
    figure in particular: `hours_below_target` refuses to count across a gap
    wider than the feed's own staleness window (6 polls), so an Awair seeded
    every five minutes would read as 36 separate isolated readings and total
    zero hours. A fixture coarser than the feed it stands for would have made
    that figure look broken -- or, worse, made a broken one look fine.
    """
    conn = db.connect(path)
    for minute in range(minutes):
        at = NOW - timedelta(minutes=minutes - minute)
        stamp = _iso(at)
        conn.execute(
            "INSERT INTO readings (ts, received_at, temp, humid, dew_point)"
            " VALUES (?, ?, ?, ?, ?)",
            (db.iso_z(at), stamp, 23.0, rows.get("awair_humid", 36.0), 7.5),
        )
    for step in range(minutes // 5):
        at = NOW - timedelta(minutes=minutes - step * 5)
        stamp = _iso(at)
        conn.execute(
            "INSERT INTO basement_readings (ts, received_at, temp, humid)"
            " VALUES (?, ?, 16.0, 55.0)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO outdoor_readings (ts, received_at, temp, humid)"
            " VALUES (?, ?, ?, 70.0)",
            (stamp, stamp, outdoor_c),
        )
        for head, temp in (("dining", 22.5), ("bedroom", 18.0)):
            conn.execute(
                "INSERT INTO minisplit_readings (head, ts, received_at, temp, humid)"
                " VALUES (?, ?, ?, ?, 38.0)",
                (head, stamp, stamp, temp),
            )
        if humidifier:
            conn.execute(
                "INSERT INTO humidifier_readings (ts, received_at, humid, temp,"
                " target_humid, mist_level, mode, switch, water_lacks,"
                " water_tank_lifted, filter_life) VALUES (?, ?, ?, 21.9, 45, 5,"
                " 'humidity', ?, ?, ?, 92)",
                (
                    stamp,
                    stamp,
                    rows.get("humidifier_humid", 39.0),
                    rows.get("switch", "on"),
                    rows.get("water_lacks", 0),
                    rows.get("tank_lifted", 0),
                ),
            )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded(tmp_path):
    return seed(tmp_path / "awair.db")


def get(path, query="?range=today"):
    return create_app(str(path)).test_client().get("/api/humidity" + query).get_json()


# --- the dew point card ----------------------------------------------------


def test_every_feed_has_a_place_on_the_dew_point_card():
    """One line per feed, derived -- so a seventh feed cannot be forgotten.

    The card is "every place that records a temperature and a humidity", and
    every feed's table carries both columns. A feed with no entry in
    `PLACE_SERIES` would draw nothing and say nothing.
    """
    assert {feed.name for feed in feeds.FEEDS} == set(web.PLACE_SERIES)
    assert [place["key"] for place in web.DEWPOINT_PLACES] == [
        feed.name for feed in feeds.FEEDS
    ]


def test_every_place_colour_is_defined_in_both_palettes():
    """An undefined custom property draws an invisible line (#156's rule).

    The dew point card's colours reach `dashboard.js` through the markup
    rather than as literals in the JS, so the existing harvest in
    `test_web_overlay_cards.py` cannot see them.
    """
    dark = re.search(
        r"prefers-color-scheme:\s*dark\s*\)\s*\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL
    )
    light = DASHBOARD_HTML.replace(dark.group(0), "")
    for _label, variable in web.PLACE_SERIES.values():
        pattern = re.escape(variable) + r":\s*#[0-9a-fA-F]{3,8}\s*;"
        assert re.search(pattern, light), f"{variable} has no light value"
        assert re.search(pattern, dark.group(1)), f"{variable} has no dark value"


def test_the_card_draws_one_series_per_feed(seeded):
    payload = get(seeded)

    assert set(payload["dew_points"]) == {feed.name for feed in feeds.FEEDS}
    for name, trace in payload["dew_points"].items():
        assert len(trace) == len(payload["t"]), name
        assert any(value is not None for value in trace), name


def test_the_markup_renders_one_place_span_per_feed(seeded):
    """The server owns the list and the DOM carries it; the JS reads it back.

    The same arrangement `DEFAULT_RANGE` uses (#108), and the reason is the
    same: a copy of this list in `dashboard.js` is a copy nothing keeps in
    step with `feeds.FEEDS`.
    """
    html = create_app(str(seeded)).test_client().get("/").get_data(as_text=True)
    rendered = re.findall(r'<span class="place" hidden data-place="([\w-]+)"', html)

    assert rendered == [place["key"] for place in web.DEWPOINT_PLACES]
    assert 'data-color="--series-humidifier"' in html


def test_the_js_reads_the_places_out_of_the_dom_rather_than_listing_them():
    """A literal list of feeds in the JS is the thing this card must not have."""
    assert 'querySelectorAll(".place")' in DASHBOARD_JS
    factory = DASHBOARD_JS[DASHBOARD_JS.index("function makeDewPointPlot") :]
    factory = factory[: factory.index("\n  }")]
    for name in (feed.name for feed in feeds.FEEDS):
        assert f'"{name}"' not in factory, (
            f"the dew point card names {name} in the JS -- its places come "
            "from the markup, which comes from `feeds.FEEDS`"
        )


def test_dew_points_are_in_the_display_unit(tmp_path, monkeypatch):
    """A temperature, so it follows `TEMPERATURE_UNIT` like every other one."""
    path = seed(tmp_path / "awair.db")
    celsius = get(path)["dew_point_latest"]["awair"]

    monkeypatch.setenv("TEMPERATURE_UNIT", "F")
    fahrenheit = get(path)["dew_point_latest"]["awair"]

    assert fahrenheit == pytest.approx(celsius * 9 / 5 + 32, abs=0.2)


def test_a_place_with_no_rows_draws_nothing_rather_than_breaking(tmp_path):
    """A box with no humidifier still gets the card, with that line empty."""
    path = seed(tmp_path / "awair.db", humidifier=False)

    payload = get(path)

    assert payload["dew_points"]["humidifier"] == [None] * len(payload["t"])
    assert payload["dew_point_latest"]["humidifier"] is None
    assert payload["dew_point_latest"]["awair"] is not None


def test_the_awairs_dew_point_is_computed_not_read_from_the_device(tmp_path):
    """The stored `dew_point` column is deliberately not what the card draws.

    Six places read against six formulas would not be a comparison. The
    fixture stores a deliberately wrong device dew point; the card must
    ignore it.
    """
    path = tmp_path / "awair.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO readings (ts, received_at, temp, humid, dew_point)"
        " VALUES (?, ?, 23.0, 40.0, -99.0)",
        (db.iso_z(NOW), _iso(NOW)),
    )
    conn.commit()
    conn.close()

    assert get(path)["dew_point_latest"]["awair"] == pytest.approx(8.69, abs=0.05)


# --- the main zone card ----------------------------------------------------


def test_the_zone_is_the_awair_by_default(seeded):
    zone = get(seeded)["main_zone"]

    assert zone["name"] == "awair"
    assert zone["label"] == "Awair Element"
    assert zone["color_var"] == web.PLACE_SERIES["awair"][1]


def test_the_zone_follows_its_environment_variable(seeded, monkeypatch):
    """One variable names the sensor, and the card's colour follows it."""
    monkeypatch.setenv(main_zone.FEED_ENV, "humidifier")

    zone = get(seeded)["main_zone"]

    assert zone["name"] == "humidifier"
    assert zone["color_var"] == web.PLACE_SERIES["humidifier"][1]
    assert zone["humid"] == zone["humidifier_humid"]


def test_all_three_great_room_sensors_are_drawn(seeded):
    """The comparison is the card: three sensors in one room that disagree."""
    zone = get(seeded)["main_zone"]

    for key, expected in (
        ("humid", 36.0),
        ("humidifier_humid", 39.0),
        ("dining_humid", 38.0),
    ):
        drawn = [value for value in zone[key] if value is not None]
        assert drawn, key
        assert drawn[-1] == pytest.approx(expected, abs=0.1), key


def test_a_mild_day_draws_the_warm_target(seeded):
    assert set(get(seeded)["main_zone"]["target"]) == {40.0}


def test_a_cold_day_draws_the_cold_target(tmp_path):
    """Only where there is an outdoor reading: before the first one it is 40.

    The grid spans the whole range and the fixture seeds the last three
    hours of it, so the earlier stamps are the documented "no outdoor
    reading, hold the warm target" case rather than a bug. The tail is what
    this test is about.
    """
    path = seed(tmp_path / "awair.db", outdoor_c=-18.0)

    target = get(path)["main_zone"]["target"]

    assert target[-1] == 35.0
    assert set(target[-30:]) == {35.0}


def test_the_target_steps_where_the_outdoor_temperature_crosses(tmp_path):
    """One line, two values, and the step is where the weather put it."""
    path = tmp_path / "awair.db"
    conn = db.connect(path)
    for step in range(24):
        at = NOW - timedelta(minutes=120 - step * 5)
        temp = -20.0 if step < 12 else 0.0
        conn.execute(
            "INSERT INTO outdoor_readings (ts, received_at, temp) VALUES (?, ?, ?)",
            (_iso(at), _iso(at), temp),
        )
        conn.execute(
            "INSERT INTO readings (ts, received_at, temp, humid) VALUES (?, ?, 23, 36)",
            (db.iso_z(at), _iso(at)),
        )
    conn.commit()
    conn.close()

    target = get(path)["main_zone"]["target"]
    last_cold = len(target) - 1 - target[::-1].index(35.0)

    assert 35.0 in target, "the cold hour did not reach the target line"
    assert target[-1] == 40.0, "the mild hour did not reach it either"
    assert set(target[last_cold + 1 :]) == {40.0}, (
        "the target line flickers after the step rather than stepping once"
    )


def test_no_outdoor_reading_holds_the_warm_target(tmp_path):
    """A dead outdoor poller must not relax the target by five points."""
    path = tmp_path / "awair.db"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO readings (ts, received_at, temp, humid) VALUES (?, ?, 23, 30)",
        (db.iso_z(NOW), _iso(NOW)),
    )
    conn.commit()
    conn.close()

    assert set(get(path)["main_zone"]["target"]) == {40.0}


def test_the_targets_are_published_for_the_card_to_label(seeded, monkeypatch):
    monkeypatch.setenv(main_zone.TARGET_ENV, "42")
    monkeypatch.setenv(main_zone.COLD_TARGET_ENV, "33")
    monkeypatch.setenv(main_zone.COLD_OUTDOOR_ENV, "-15")

    zone = get(seeded)["main_zone"]

    assert (zone["target_warm"], zone["target_cold"]) == (42.0, 33.0)
    assert zone["cold_outdoor_temp"] == -15.0


def test_the_cold_threshold_is_published_in_the_display_unit(seeded, monkeypatch):
    """It is a temperature on a page whose temperatures are converted."""
    monkeypatch.setenv("TEMPERATURE_UNIT", "F")

    assert get(seeded)["main_zone"]["cold_outdoor_temp"] == pytest.approx(10.4, abs=0.1)


# --- the shading -----------------------------------------------------------


def test_an_out_of_water_stretch_is_shaded(tmp_path):
    path = seed(tmp_path / "awair.db", water_lacks=1)

    spans = get(path)["main_zone"]["spans"]

    assert spans and all(span["reason"] == "dry" for span in spans)


def test_an_off_stretch_is_shaded_differently(tmp_path):
    path = seed(tmp_path / "awair.db", switch="off")

    spans = get(path)["main_zone"]["spans"]

    assert spans and all(span["reason"] == "off" for span in spans)


def test_a_working_humidifier_is_not_shaded(seeded):
    assert get(seeded)["main_zone"]["spans"] == []


# --- the weekly figure -----------------------------------------------------


def test_the_hours_below_target_are_counted(seeded):
    """Three hours of readings at 36 % against a 40 % target."""
    zone = get(seeded)["main_zone"]

    assert zone["hours_below_target"] == pytest.approx(2.9, abs=0.2)
    assert zone["below_target_days"] == main_zone.BELOW_TARGET_DAYS


def test_a_zone_above_target_spends_no_hours_below_it(tmp_path):
    path = seed(tmp_path / "awair.db", awair_humid=48.0)

    assert get(path)["main_zone"]["hours_below_target"] == 0.0


@pytest.mark.parametrize("window", ["?range=today", "?range=7d", "?range=30d"])
def test_the_figure_does_not_follow_the_range_buttons(seeded, window):
    """Its own fixed week, like the basement watch's own windows.

    A figure that shrank when somebody pressed "Today" would be a different
    statistic under the same label -- and the label says "the last 7 days".
    """
    today = get(seeded)["main_zone"]["hours_below_target"]

    assert get(seeded, window)["main_zone"]["hours_below_target"] == today


def test_the_figure_follows_the_zone_sensor(seeded, monkeypatch):
    """Control for the two above: it is the ZONE's hours, not the Awair's.

    The humidifier reads 39 % where the Awair reads 36 %, and the target is
    40 %, so both are below it -- the figures are equal in this fixture by
    construction. What this pins is that moving the zone moves which rows the
    figure is computed from at all, which a seeded difference in cadence
    makes visible: the humidifier is polled every 5 minutes like the Awair
    here, so a figure read off the wrong feed would still be close. Assert
    the sensitivity directly instead, by making the zone's own readings
    comfortable.
    """
    path = seed(Path(str(seeded)).parent / "moved.db", humidifier_humid=48.0)
    monkeypatch.setenv(main_zone.FEED_ENV, "humidifier")

    assert get(path)["main_zone"]["hours_below_target"] == 0.0


# --- the contract with the browser -----------------------------------------


def _humidity_js():
    """The block of `dashboard.js` that draws these two cards, comments gone.

    Stripped, because the harvest below reads `payload.x` out of the text and
    these comments discuss `main_zone.BELOW_TARGET_DAYS` by name -- a prose
    mention would otherwise be asserted against the payload as if it were
    code. The same helper the sync guards use, for the mirror-image reason.
    """
    code = strip_js_comments(DASHBOARD_JS)
    start = code.index("const SPAN_COLORS")
    end = code.index("const ROOM_CARDS")
    return code[start:end]


def test_every_payload_key_the_cards_read_is_one_the_endpoint_ships(seeded):
    """Rename either side and uPlot draws an undefined series: no line, no error.

    Harvested from the JS rather than listed, so a key added to a card
    tomorrow is covered without an edit here.
    """
    block = _humidity_js()
    read = set(re.findall(r"payload\.(\w+)", block))
    zone_read = set(re.findall(r"zone\.(\w+)", block))

    payload = get(seeded)

    assert {"t", "dew_points", "main_zone", "temp_unit_symbol"} <= read
    assert {"humid", "humidifier_humid", "dining_humid", "target", "spans"} <= zone_read
    assert read <= set(payload), f"the JS reads {sorted(read - set(payload))}"
    assert zone_read <= set(payload["main_zone"]), (
        f"the JS reads main_zone.{sorted(zone_read - set(payload['main_zone']))}"
    )


def test_the_shading_reasons_the_js_paints_are_the_ones_python_emits():
    """A reason with no colour falls back to grey and reads as 'switched off'."""
    painted = set(re.findall(r"SPAN_COLORS = \{([^}]*)\}", DASHBOARD_JS)[0].split(","))
    names = {entry.split(":")[0].strip() for entry in painted if entry.strip()}

    assert names == {main_zone.OFF_SPAN, main_zone.DRY_SPAN}


def test_the_two_cards_are_in_the_markup_and_drawn_by_the_js(seeded):
    """A card with no factory is a heading over nothing; a factory with no card
    draws nowhere. Neither says anything at run time."""
    html = create_app(str(seeded)).test_client().get("/").get_data(as_text=True)

    for marker in ('data-zone="main"', 'data-derived="dewpoint"'):
        assert marker in html, marker
        assert marker.replace('"', "'") in DASHBOARD_JS or marker in DASHBOARD_JS


def test_the_page_no_longer_carries_its_own_fine_print(seeded):
    """The explanation moved to docs/reading-the-dashboard.md; the page is charts.

    The configured target still reaches the card through the humidity payload,
    which `test_the_targets_are_published_for_the_card_to_label` pins.
    """
    page = create_app(str(seeded)).test_client().get("/").get_data(as_text=True)

    assert "<footer" not in page
