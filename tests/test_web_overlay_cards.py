"""The two overlay cards: temperatures on one, humidity on the other (#156).

The browser half of what may ride a **metric** card. The data half already
ships every trace on `/api/series`; what lands here is the rule and the guards
that hold it.

**The rule is one sentence: an overlay is its card's own quantity, in its
card's own unit, drawn against its card's own axis.** That is what admits the
outdoor temperature onto the temp card -- the distance between an indoor line
and an outdoor one is the answer #109 asked for, where two auto-fitted axes
would draw a 1 degree indoor drift and a 20 degree outdoor swing as the same
stroke -- and it is what admits the basement sensor's and both heads' humidity
onto the humidity card.

**That rule has been round a loop and is back where it started, which is the
thing to understand before editing this file.** It began as "only a temperature
in this card's display unit may ride this card", with `minisplit_watts` named as
the series that must never be an overlay. #139 asked for the dining head's
metered draw on the temp card anyway and shipped it the way #42's pressure
trace rides the precipitation card -- `scale: "watts"` and a `side: 1` axis --
which made the admission test a disjunction: in the card's unit, **or** it
declares its own scale. #156 took the draw off the temp card (Tom: the card is
"barely readable") and gave it a card beside the head that produces it, so no
overlay on any metric card declares a scale and the single sentence is the
whole rule again.

A second unit on a *card* was never the problem and two cards still do it --
the precipitation card's pressure and the room cards' draw -- but each declares
its second axis in its own chart factory, where that claim is legible.
`tests/test_dashboard_assets.py::test_the_overlay_loop_names_no_scale_at_all`
and its neighbour
`test_a_card_drawing_a_second_quantity_gives_it_its_own_right_hand_axis` are
the two halves of that split.

What each test here is for:

- `test_every_overlay_is_its_cards_own_quantity_in_its_cards_unit` holds the
  rule by behaviour rather than by name: flip `TEMPERATURE_UNIT` and a
  temperature overlay has to move by the Celsius-to-Fahrenheit relation while a
  humidity overlay has to stay exactly where it was.
- `test_the_unit_guard_would_actually_catch_the_wrong_quantity` is the
  reachability control for both halves of it. An always-passing version of that
  test looks identical from here.
- `test_the_js_overlay_lists_and_the_constant_agree` stops an entry existing on
  one surface only -- never drawn, or never marked.
- `test_the_draw_is_off_the_temp_card` pins #156's actual ask.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import db, minisplit, units, web
from tests._helpers import strip_js_comments, strip_markup_comments
from tests.test_web import _seed_db, _seed_outdoor

REPO = Path(__file__).resolve().parent.parent
JS_PATH = REPO / "static" / "dashboard.js"
HTML_PATH = REPO / "templates" / "dashboard.html"

# Stripped, never raw. Every assertion below is a *presence* assertion, and a
# presence assertion over raw text is green on a construct that has been
# commented out -- i.e. on a dashboard where the feature is switched off. The
# first shape of this file read both sources raw, and all six tests passed
# with the mini split entry commented out of the overlay list, with the CSS
# rule commented out, and with the dark-mode colour commented out. The control
# for that is `test_a_commented_out_declaration_reads_as_absent`.
DASHBOARD_JS = strip_js_comments(JS_PATH.read_text())
DASHBOARD_HTML = strip_markup_comments(HTML_PATH.read_text())

# One mini split row's worth of every column the series endpoints read, with a
# window `minisplit.metered_watts` can measure -- so the watts series the
# control below needs is a real number rather than None.
_WINDOW_SECONDS = 800
_DELTA_WH = 7.0
_CELSIUS = 18.89  # 66.0 F, the dining head's reading on 2026-10-30
_HEAD_HUMID = 46.0
_BEDROOM_CELSIUS = 13.89  # 57.0 F, the bedroom head's reading on 2026-10-31
_BEDROOM_HUMID = 44.0
_BASEMENT_CELSIUS = 14.5
_BASEMENT_HUMID = 61.0


def _seed_minisplit(db_path, at, head=minisplit.DINING, **overrides):
    conn = db.connect(db_path)
    db.insert_minisplit_reading(
        conn,
        {
            "head": head,
            "ts": at.isoformat(),
            "received_at": at.isoformat(),
            "window_start": (at - timedelta(seconds=_WINDOW_SECONDS)).isoformat(),
            "window_close": at.isoformat(),
            "delta_wh": _DELTA_WH,
            "watts_spot": 30.0,
            "temp": _CELSIUS,
            "mode": "heat",
            "switch": "on",
            "setpoint": None,
            "target": None,
            "target_source": None,
            "decision_reason": None,
            "temperature_at": None,
            "raw": None,
            "humid": _HEAD_HUMID,
            **overrides,
        },
    )
    conn.close()


def _seed_basement(db_path, at):
    conn = db.connect(db_path)
    db.insert_basement_reading(
        conn,
        {
            "ts": at.replace(second=0, microsecond=0).isoformat(),
            "received_at": at.isoformat(),
            "temp": _BASEMENT_CELSIUS,
            "humid": _BASEMENT_HUMID,
        },
    )
    conn.close()


def _series(tmp_path, monkeypatch, unit):
    """`/api/series?range=today` with a reading from every overlay's source.

    Every source, not just one: the guard below grades every entry the JS
    lists, and an overlay whose source is unseeded comes back as a full-length
    run of nulls -- which is a trace it cannot tell a temperature from a
    percent for. That is a failure here rather than a skip.
    """
    monkeypatch.setenv("TEMPERATURE_UNIT", unit)
    db_path = tmp_path / f"overlay-cards-{unit}.db"
    _seed_db(db_path)
    recent = datetime.now(UTC) - timedelta(seconds=60)
    _seed_minisplit(db_path, recent)
    _seed_minisplit(
        db_path,
        recent,
        head=minisplit.BEDROOM,
        temp=_BEDROOM_CELSIUS,
        humid=_BEDROOM_HUMID,
    )
    _seed_basement(db_path, recent)
    _seed_outdoor(db_path)
    app = web.create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client().get("/api/series?range=today").get_json()


def _overlay_block(metric, js=None):
    """The body of one metric's list inside `const CARD_OVERLAYS = { ... };`.

    One definition rather than a copy of the pattern per test: the two inline
    copies this file shipped with had no `assert block`, so a reformatted
    closing bracket failed them with a bare `AttributeError` instead of a
    sentence naming the cause.
    """
    whole = re.search(r"const CARD_OVERLAYS = \{(.*?)\n  \};", js or DASHBOARD_JS, re.S)
    assert whole, "no `const CARD_OVERLAYS = {...}` in dashboard.js"
    block = re.search(r"\n    " + metric + r": \[(.*?)\n    \],", whole.group(1), re.S)
    assert block, f"`CARD_OVERLAYS` has no `{metric}` list"
    return block.group(1)


def _overlay_entries(metric, js=None):
    """`{key: entry-source}` for every object literal in one metric's list."""
    entries = re.findall(r"\{(.*?)\n      \}", _overlay_block(metric, js), re.S)
    assert entries, f"`CARD_OVERLAYS.{metric}` lists no entries"
    return {re.search(r'key:\s*"([\w-]+)"', e).group(1): e for e in entries}


def _overlay_payload_keys(metric, js=None):
    """The `/api/series` keys `dashboard.js` draws onto one metric's card."""
    keys = re.findall(r'payload:\s*"(\w+)"', _overlay_block(metric, js))
    assert keys, f"`CARD_OVERLAYS.{metric}` lists no payload keys"
    return keys


def _overlay_metrics(js=None):
    """The metrics whose cards carry overlays, as `dashboard.js` has them."""
    whole = re.search(r"const CARD_OVERLAYS = \{(.*?)\n  \};", js or DASHBOARD_JS, re.S)
    assert whole, "no `const CARD_OVERLAYS = {...}` in dashboard.js"
    return set(re.findall(r"^    (\w+): \[", whole.group(1), re.MULTILINE))


def _around(needle, before=80, after=60):
    """The stripped stylesheet either side of `needle`, for a failure message.

    A diagnostic, not an assertion: when a scoped-rule guard below fails it is
    usually because the rule moved, and the next question is always "what is it
    now". Quoting the neighbourhood answers that without a second run.
    """
    at = DASHBOARD_HTML.index(needle)
    return " ".join(DASHBOARD_HTML[at - before : at + len(needle) + after].split())


def _non_null_pairs(celsius_trace, fahrenheit_trace):
    """Aligned (C-config, F-config) values where both traces have a reading."""
    assert len(celsius_trace) == len(fahrenheit_trace)
    return [
        (c, f)
        for c, f in zip(celsius_trace, fahrenheit_trace, strict=True)
        if c is not None and f is not None
    ]


def _seeded_pairs(in_c, in_f, key):
    pairs = _non_null_pairs(in_c[key], in_f[key])
    assert pairs, (
        f"`{key}` has no reading in the seeded range, so this guard cannot "
        "tell one quantity from another for it -- seed its source. If every "
        "key is empty and the clock is within a minute of local midnight, that "
        "is the `range=today` window rather than a missing fixture: the rows "
        "are seeded at now-60s and fall in yesterday."
    )
    return pairs


# --- the admission rule ---


def test_every_overlay_is_its_cards_own_quantity_in_its_cards_unit(
    tmp_path, monkeypatch
):
    """Flip `TEMPERATURE_UNIT` and every overlay must behave like its card.

    An overlay declares no scale, so it is drawn against its card's own y axis,
    and that is the claim omitting the scale makes. A series in some other unit
    sharing that axis makes the chart draw whichever of the two has the smaller
    range as a flat line -- a wrong answer, with nothing red anywhere.

    The two halves are two different behaviours under one flip, which is what
    makes this one test rather than two:

    - a temp-card overlay must CONVERT, by the Celsius-to-Fahrenheit relation.
    - a humidity-card overlay must NOT MOVE. A percent is a percent, and a
      humidity trace that changed with the temperature unit would be a
      temperature wearing a `%`.

    Harvested from the JS rather than listed, so an overlay added tomorrow is
    covered without an edit here. `test_the_unit_guard_would_actually_catch_
    the_wrong_quantity` is the reachability control for both halves.

    **One mutant this does not kill, measured rather than assumed.** Publishing
    the basement's *unconverted Celsius* temperature as `basement_humid`
    survives it: an unconverted number does not move under the flip, which is
    exactly what this test asks a percent to do. A percent on the temp card
    IS caught (that one has to convert and cannot). The gap is covered by
    `test_every_humidity_source_reaches_the_card`, which asserts the seeded
    humidity *values* arrive rather than merely that they hold still -- so read
    the two together, and add to that one when a source is added.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")

    temps = _overlay_payload_keys("temp")
    assert len(temps) >= 3, (
        f"only {temps} ride the temp card; this guard was written against "
        "three temperatures and an empty list would pass it silently"
    )
    for key in temps:
        assert key in in_c, f"the temp card draws `{key}`, /api/series lacks it"
        for celsius, fahrenheit in _seeded_pairs(in_c, in_f, key):
            assert fahrenheit == pytest.approx(units.from_celsius(celsius, "F")), (
                f"overlay `{key}` does not convert with the temperature unit, "
                "so it is not a temperature and must not share the temp "
                "card's y axis"
            )

    humids = _overlay_payload_keys("humid")
    assert len(humids) >= 3, (
        f"only {humids} ride the humidity card; this guard was written "
        "against three sources and an empty list would pass it silently"
    )
    for key in humids:
        assert key in in_c, f"the humidity card draws `{key}`, /api/series lacks it"
        for under_c, under_f in _seeded_pairs(in_c, in_f, key):
            assert under_c == under_f, (
                f"overlay `{key}` moves with the temperature unit, so it is "
                "not a humidity and must not share the humidity card's y axis"
            )


def test_the_unit_guard_would_actually_catch_the_wrong_quantity(tmp_path, monkeypatch):
    """Reachability control for the test above, in both directions (#139, #156).

    Without it, an always-passing version of that test -- one whose traces were
    all null, or whose key lists came back empty -- reads exactly the same.

    Two probes, each a real series travelling in the same payload one key away
    from the entries being graded:

    - `minisplit_watts` is a non-temperature: a real number in both configs and
      the *same* number in both, so the temp half above would go red on it.
    - `outdoor_temp` is a non-percent: a real number in both and a *different*
      number in each, so the humidity half would go red on it.

    Which is also to say the two halves cannot both be vacuous: each one's
    failure case is the other one's passing case.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")

    for celsius_side, fahrenheit_side in _seeded_pairs(in_c, in_f, "minisplit_watts"):
        assert celsius_side == fahrenheit_side
        assert celsius_side != pytest.approx(units.from_celsius(celsius_side, "F"))

    for under_c, under_f in _seeded_pairs(in_c, in_f, "outdoor_temp"):
        assert under_c != under_f


def test_no_overlay_declares_a_scale_a_unit_or_a_precision(tmp_path, monkeypatch):
    """The three keys #139 added to these entries, and #156 removed with the draw.

    `overlayFormatter` reads the CARD's unit and digits now, and the push loop
    names no scale, so an entry still carrying any of the three would be a
    declaration the dashboard silently ignores -- a line claiming its own axis
    and drawn on the shared one, with the claim sitting in the file as
    documentation of something untrue.
    """
    for metric in _overlay_metrics():
        for key, entry in _overlay_entries(metric).items():
            for field in ("scale", "unit", "digits"):
                assert not re.search(field + r"\s*:", entry), (
                    f"overlay `{key}` declares `{field}:`, which nothing reads "
                    "since #156 -- an overlay is its card's own quantity in "
                    "its card's own unit"
                )


def test_the_draw_is_off_the_temp_card(tmp_path, monkeypatch):
    """#156's actual ask, pinned on every surface that could put it back.

    The temp card is four lines: the Awair's room, outdoors, the basement and
    the bedroom head. The dining head's temperature and its metered draw are on
    the Great room card, which is where the question "what is the head doing to
    this room" is answered.

    `/api/series` still publishes both of those keys and that is not an
    oversight -- the Great room card reads them from there, which is why it
    needs no endpoint of its own.
    """
    marked = {key for keys in web.METRIC_OVERLAYS.values() for key in keys}
    assert "minisplit-watts" not in marked
    assert "minisplit-temp" not in marked
    assert "minisplit_watts" not in _overlay_payload_keys("temp")
    assert "minisplit_temp" not in _overlay_payload_keys("temp")
    payload = _series(tmp_path, monkeypatch, "F")
    assert "minisplit_watts" in payload and "minisplit_temp" in payload, (
        "the Great room card reads the dining head off /api/series; dropping "
        "those keys leaves that card with nothing to draw"
    )


def test_every_humidity_source_reaches_the_card(tmp_path, monkeypatch):
    """The three sources #156 asked for, each with a reading of its own.

    A trace of all nulls draws as nothing, and for the two heads that is a real
    possibility rather than a bug (the capability is unverified) -- so this
    seeds each source and checks the number arrives, which is the only way to
    tell "no such sensor" from "the server dropped it on the way".
    """
    payload = _series(tmp_path, monkeypatch, "F")
    readings = {
        key: [v for v in payload[key] if v is not None]
        for key in _overlay_payload_keys("humid")
    }
    assert set(readings) == {"basement_humid", "minisplit_humid", "bedroom_humid"}
    assert _BASEMENT_HUMID in readings["basement_humid"]
    assert _HEAD_HUMID in readings["minisplit_humid"]
    assert _BEDROOM_HUMID in readings["bedroom_humid"]
    # The card's own series is the Awair's, which is the fourth source #156
    # names and is not an overlay at all.
    assert payload["metrics"]["humid"]["avg"]


def test_an_off_bedroom_head_draws_neither_line(tmp_path, monkeypatch):
    """The grace rule covers the humidity too, not just the temperature.

    `/status` republishes an off head's last reading in every field, so a
    humidity drawn past the grace window is exactly as stale as the temperature
    beside it -- and the bedroom head is off for long stretches by design.
    """
    monkeypatch.setenv("TEMPERATURE_UNIT", "F")
    db_path = tmp_path / "off-head.db"
    _seed_db(db_path)
    long_off = datetime.now(UTC) - timedelta(seconds=60)
    _seed_minisplit(
        db_path,
        long_off,
        head=minisplit.BEDROOM,
        temp=_BEDROOM_CELSIUS,
        humid=_BEDROOM_HUMID,
        switch="off",
    )
    app = web.create_app(db_path=str(db_path))
    app.testing = True
    payload = app.test_client().get("/api/series?range=today").get_json()
    assert set(payload["bedroom_temp"]) == {None}
    assert set(payload["bedroom_humid"]) == {None}


# --- the surfaces the marker binds together ---


def test_the_js_overlay_lists_and_the_constant_agree():
    """`web.METRIC_OVERLAYS` admits an overlay; `CARD_OVERLAYS` draws it.

    An entry in the JS that the constant never marks is never drawn -- the
    card's `data-overlay` is the gate. An entry the constant marks with no JS
    entry is a reserved legend row with nothing in it. Asserted per card and
    over the card list itself, so a third overlay card cannot appear on one
    surface alone either.
    """
    assert _overlay_metrics() == set(web.METRIC_OVERLAYS), (
        f"dashboard.js carries overlays for {sorted(_overlay_metrics())} and "
        f"web.METRIC_OVERLAYS for {sorted(web.METRIC_OVERLAYS)}"
    )
    for metric, marked in web.METRIC_OVERLAYS.items():
        drawn = set(_overlay_entries(metric))
        assert drawn == set(marked), (
            f"the {metric} card draws {sorted(drawn)} and is marked for "
            f"{sorted(marked)} -- an entry on one surface only is either never "
            "drawn or never marked"
        )


def test_every_overlay_card_reserves_four_rows():
    """Seven legend entries per overlay card, four reserved rows, no exceptions.

    Every `[data-overlay~=...]` rule is specificity (0,3,0), so the LAST one a
    card matches wins rather than the largest -- which means a new rule setting
    three rows would silently clip two entries, and one setting five would make
    the card permanently taller than its row-mates and grow the whole grid row.

    Four is the measured default: eight populated entries at a 340px card were
    71.9px against the 74.8px four rows reserve, and both cards carry seven
    now. Asserted per card rather than globally, because "some rule somewhere
    says four" is true of a page where one card has none.
    """
    unscoped = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", DASHBOARD_HTML)
    rules = {
        name: int(rows)
        for name, rows in re.findall(
            r'\.card\[data-overlay~="([\w-]+)"\] \.u-legend \{\s*--legend-rows:\s*(\d+)',
            unscoped,
        )
    }
    for metric, overlays in web.METRIC_OVERLAYS.items():
        on_card = {name: rules[name] for name in overlays if name in rules}
        assert on_card, (
            f"no unscoped --legend-rows rule is keyed to a {metric}-card "
            "overlay, so the card falls back to the three rows "
            "`.card[data-overlay]` sets and clips every entry past the sixth"
        )
        assert set(on_card.values()) == {4}, (
            f"{metric}-card legend reservations are {on_card}; every unscoped "
            "rule the card matches must say 4 -- fewer clips an entry, more is "
            "permanent blank space on a card whose height sets its grid row's"
        )


def test_each_overlay_card_has_a_rule_keyed_to_more_than_one_of_its_overlays():
    """Two rules at four rows per card, not one, and the reason is independence.

    Both rules on a card set the same four rows, so deleting either changes no
    pixel today. What the pair buys is that neither is load-bearing alone: a
    rule keyed to `basement-temp` takes the fourth row with it if the basement
    line is ever dropped, leaving seven entries in three rows and two of them
    clipped. Forward insurance, written down as such.
    """
    keyed = set(
        re.findall(r'\.card\[data-overlay~="([\w-]+)"\] \.u-legend', DASHBOARD_HTML)
    )
    for metric, overlays in web.METRIC_OVERLAYS.items():
        covered = keyed & set(overlays)
        assert len(covered) >= 2, (
            f"the {metric} card's four-row reservation hangs on {covered} "
            "alone; key a second of its overlays to the same rule so dropping "
            "one line does not clip two entries"
        )


def test_the_metered_draw_is_declared_on_the_surfaces_it_still_needs():
    """The draw moved cards (#156) and kept the one surface that is about it.

    It is the only line on either room card not read against the card's own
    axis, so the footer has to say which axis it IS read against: a reader who
    takes its height for a temperature gets a wrong answer and nothing on the
    chart tells them otherwise.

    What it no longer needs is a legend reservation of its own. #139's
    `@media (max-width: 379px)` rule bought the temp card a fifth row for an
    eighth entry; the Great room card has four entries and the Bedroom card
    five, so nothing on the page carries eight and the rule is gone with the
    card it was measured on.
    """
    assert '"watts"' in DASHBOARD_JS, "no series is drawn on the watts scale"
    footer = DASHBOARD_HTML[DASHBOARD_HTML.index("<footer>") :]
    assert "right-hand axis" in footer, (
        "the footer does not say the draw is read against the right-hand "
        "axis, so its height reads as a temperature"
    )
    assert "minisplit-watts" not in DASHBOARD_HTML.replace(
        "--series-minisplit-watts", ""
    ), (
        "a `minisplit-watts` overlay marker or legend rule survives in the "
        f"stylesheet: {_around('minisplit-watts')!r}"
    )


def test_every_series_colour_variable_is_defined_in_both_palettes():
    """A colour name with no `--series-*` declaration draws an invisible line.

    `cssVar()` returns the empty string for an undefined custom property and
    uPlot takes that as "no stroke", so the series is in the legend, in the
    data, taking up a reserved row, and not on the chart. Both palettes,
    because the dark block is a separate set of declarations and a light-only
    colour disappears for whoever reads this dashboard at night.

    Harvested from every surface of the JS that names one -- the overlay
    entries' `colorVar`, the room cards' lines and dots, and the literals the
    factories pass to `cssVar` -- so a new card cannot ship without a colour.
    """
    names = set(re.findall(r'(?:colorVar|dotVar):\s*"(--[\w-]+)"', DASHBOARD_JS))
    names |= set(re.findall(r'cssVar\("(--series-[\w-]+)"\)', DASHBOARD_JS))
    # `outdoor-temp` borrows OUTDOOR_METRICS.temp.colorVar, so it is named
    # elsewhere in the file; the literal ones are what this can harvest.
    assert {"--series-dining-head", "--series-bedroom-head"} <= names, (
        f"the two heads' colours are no longer named in dashboard.js: {names}"
    )
    dark = re.search(
        r"prefers-color-scheme:\s*dark\s*\)\s*\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL
    )
    assert dark, "no dark-mode block in dashboard.html"
    light = DASHBOARD_HTML.replace(dark.group(0), "")
    for name in sorted(names):
        assert re.search(re.escape(name) + r":\s*#[0-9a-fA-F]{3,8}\s*;", light), (
            f"`{name}` has no value in the light palette"
        )
        assert re.search(
            re.escape(name) + r":\s*#[0-9a-fA-F]{3,8}\s*;", dark.group(1)
        ), f"`{name}` has no value in the dark palette"


def test_the_two_heads_are_told_apart_by_colour():
    """One colour per head, because the two now appear on one card (#156).

    The humidity card draws both heads, so sharing #139's green between them
    would make the card's two head lines indistinguishable -- and worse, the
    same green would mean the bedroom head on the temp card and the dining head
    one card away. The values themselves are chosen by a measured dE76 sweep;
    see the comments on the declarations.
    """
    bedroom = _overlay_entries("humid")["bedroom-humid"]
    dining = _overlay_entries("humid")["minisplit-humid"]
    assert '"--series-bedroom-head"' in bedroom
    assert '"--series-dining-head"' in dining
    assert "--series-minisplit-temp" not in DASHBOARD_HTML, (
        "the old per-trace name is back in the palette; the two heads' "
        "colours are named for the heads (#156)"
    )


def test_the_overlay_lines_stay_out_of_the_card_headers():
    """`prefix: null` on all but one, for a measured reason.

    A fourth number needs ~265px against the ~245px the header has at the
    card's width; the wrapped header pushes the card's chart below its
    row-mates' and grows the whole grid row. The header loop skips an overlay
    with no prefix, so this is the entire mechanism.
    """
    prefixed = [
        key
        for metric in _overlay_metrics()
        for key, entry in _overlay_entries(metric).items()
        if not re.search(r"prefix:\s*null", entry)
    ]
    assert prefixed == ["outdoor-temp"], (
        f"{prefixed} would each print a number in a card header; only the "
        "outdoor one fits (measured at 1440 and 390)"
    )


def test_the_footer_tells_a_reader_what_a_break_in_each_line_means():
    """Each line's gap has a different cause, and the footer is where it says so.

    The traces are nulled rather than spanned once their source goes stale, so
    a break is the normal rendering of an outage -- and several lines on one
    card means several different outages with the same appearance. The humidity
    card adds a cause the temperature cards do not have: a head that does not
    report humidity at all, which draws as nothing and is not an outage.
    """
    footer = DASHBOARD_HTML[DASHBOARD_HTML.index("<footer>") :]
    assert "mini split" in footer
    assert "mini-split-poller gap" in footer
    assert "outdoor-poller gap" in footer
    assert "basement-poller gap" in footer
    assert "does not report" in footer, (
        "the footer does not tell a reader that a missing humidity line may "
        "be a head with no such sensor rather than an outage (#156)"
    )


# --- the control on every presence assertion above ---


def test_a_commented_out_declaration_reads_as_absent():
    """Comment out each of the three surfaces and the guards must see nothing.

    This is the control the rest of this file rests on, and it is here because
    the first shape of the file failed it. Reading `dashboard.js` and
    `dashboard.html` raw, all six tests of the day passed with the mini split
    entry commented out of the overlay list — the line gone from the browser,
    the feature off, and the file that exists to record that decision green.
    The same held for the CSS rule and for the dark-mode colour: the test
    written to stop an invisible-at-night line passed while the line was
    invisible at night.

    A presence assertion over raw text cannot tell shipped from switched off.
    So the assertion is not "the stripper works" — it is "with each surface
    commented out, the thing this file looks for is gone", driven against
    the real sources rather than a synthetic fixture.
    """
    js_raw = JS_PATH.read_text()
    html_raw = HTML_PATH.read_text()

    # 1. The JS entry, line-commented the way an editor does it.
    entry = re.search(
        r'      \{\n        key: "bedroom-humid",.*?\n      \},\n', js_raw, re.DOTALL
    )
    assert entry, "no `bedroom-humid` object literal in dashboard.js to comment out"
    commented_js = js_raw.replace(
        entry.group(0),
        "".join(f"//{line}\n" for line in entry.group(0).splitlines()),
    )
    assert "bedroom-humid" in _overlay_entries("humid", strip_js_comments(js_raw))
    assert "bedroom-humid" not in _overlay_entries(
        "humid", strip_js_comments(commented_js)
    )

    # 2. The legend-row rule, block-commented.
    rule = '.card[data-overlay~="bedroom-humid"] .u-legend'
    assert rule in strip_markup_comments(html_raw)
    hidden_rule = html_raw.replace(rule, f"/* {rule}", 1).replace(
        "{ --legend-rows: 4; }\n/* The Bedroom card has five legend entries",
        "{ --legend-rows: 4; } */\n/* The Bedroom card has five legend entries",
        1,
    )
    assert rule not in strip_markup_comments(hidden_rule)

    # 3. The dark-mode colour, block-commented in place.
    decls = list(re.finditer(r"--series-bedroom-head:\s*#[0-9a-fA-F]{3,8};", html_raw))
    assert len(decls) == 2, (
        f"expected one `--series-bedroom-head` per palette, found {len(decls)}"
    )
    dark_decl = decls[-1]
    hidden_dark = (
        html_raw[: dark_decl.start()]
        + f"/* {dark_decl.group(0)} */"
        + html_raw[dark_decl.end() :]
    )
    block = re.search(
        r"prefers-color-scheme:\s*dark\s*\)\s*\{(.*?)\n\}",
        strip_markup_comments(hidden_dark),
        re.DOTALL,
    )
    assert block, "no dark-mode block survived the stripper"
    assert "--series-bedroom-head" not in block.group(1)


def test_the_markup_stripper_matches_the_file_it_is_used_on():
    """`strip_markup_comments` is simple because this template is simple.

    It is not quote-aware, so a `/*` inside a string literal would eat the
    rest of the file. `templates/dashboard.html` has no inline script body and
    no `//` anywhere, which is what makes that safe — and both of those are
    properties of the file rather than guarantees, so they are asserted here.
    The day this template grows an inline `<script>`, this fails and says to
    give the helper the quote-awareness its JS sibling already has.
    """
    raw = HTML_PATH.read_text()
    tags = re.findall(r"<script\b[^>]*>", raw)
    assert tags, "no <script> tag in dashboard.html at all — read this file again"
    for tag in tags:
        assert "src=" in tag, (
            f"{tag} has no src, so dashboard.html now carries an inline script; "
            "`strip_markup_comments` is not quote-aware and must become so"
        )
    assert "//" not in raw, (
        "dashboard.html gained a `//`; if that is a line comment in an inline "
        "script, `strip_markup_comments` does not remove it"
    )
