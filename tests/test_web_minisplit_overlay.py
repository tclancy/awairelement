"""The mini split's room temperature as a temp-card overlay, and what may not be (#139).

The browser half of #139. The data half (#140) already ships `minisplit_temp`
and `minisplit_watts` on `/api/series`; what lands here is the decision that
**only the first of those may ride the temperature card**, and the guards that
hold it.

The admission test for a temp-card overlay is not "is it on the same feed" —
it is "is it a temperature in this card's display unit". The overlays share the
card's one y axis on purpose (`test_the_overlay_series_shares_the_cards_own_y_axis`
pins the absence of a `scale:`), and that is sound exactly while every line is
the same quantity in the same unit. `minisplit_watts` arrives in the same
payload, from the same table, one key away from `minisplit_temp` — which makes
appending it to `TEMP_OVERLAYS` the single likeliest way this regresses, and
the result would be two flat lines rather than anything red.

So the guard below does not name `minisplit_watts`. It asks every overlay the
page declares to *behave* like a temperature: flip `TEMPERATURE_UNIT` and the
trace has to move by the Celsius-to-Fahrenheit relation. A watts series cannot
pass that, and neither can any other non-temperature key somebody adds later.
`test_the_unit_guard_would_actually_catch_the_watts_series` is the reachability
control for it — an always-passing version of this test looks identical.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awair import db, units, web
from tests._helpers import strip_js_comments, strip_markup_comments
from tests.test_web import _seed_db, _seed_outdoor

REPO = Path(__file__).resolve().parent.parent
JS_PATH = REPO / "static" / "dashboard.js"
HTML_PATH = REPO / "templates" / "dashboard.html"

# Stripped, never raw. Every assertion below is a *presence* assertion, and a
# presence assertion over raw text is green on a construct that has been
# commented out -- i.e. on a dashboard where the feature is switched off. The
# first shape of this file read both sources raw, and all six tests passed
# with the `minisplit-temp` entry commented out of `TEMP_OVERLAYS`, with the
# CSS rule commented out, and with the dark-mode colour commented out. The
# control for that is `test_a_commented_out_declaration_reads_as_absent`.
DASHBOARD_JS = strip_js_comments(JS_PATH.read_text())
DASHBOARD_HTML = strip_markup_comments(HTML_PATH.read_text())

# One mini split row's worth of every column `_MINISPLIT_SERIES_COLUMNS` reads,
# with a window that `minisplit.metered_watts` can measure -- so the watts
# series the control below needs is a real number rather than None.
_WINDOW_SECONDS = 800
_DELTA_WH = 7.0
_CELSIUS = 18.89  # 66.0 F, the dining head's reading on 2026-10-30


def _seed_minisplit(db_path, at):
    conn = db.connect(db_path)
    db.insert_minisplit_reading(
        conn,
        {
            "ts": at.isoformat(),
            "received_at": at.isoformat(),
            "window_start": (at - timedelta(seconds=_WINDOW_SECONDS)).isoformat(),
            "window_close": at.isoformat(),
            "delta_wh": _DELTA_WH,
            "watts_spot": 30.0,
            "temp": _CELSIUS,
            "mode": "heat",
            "switch": "on",
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
            "temp": 14.5,
        },
    )
    conn.close()


def _series(tmp_path, monkeypatch, unit):
    """`/api/series?range=today` with a reading from every overlay's source.

    All three sources, not just the mini split: the guard below grades every
    entry `TEMP_OVERLAYS` lists, and an overlay whose source is unseeded comes
    back as a full-length run of nulls -- which is a trace it cannot tell a
    temperature from a watt for. That is a failure here rather than a skip.
    """
    monkeypatch.setenv("TEMPERATURE_UNIT", unit)
    db_path = tmp_path / f"minisplit-overlay-{unit}.db"
    _seed_db(db_path)
    recent = datetime.now(UTC) - timedelta(seconds=60)
    _seed_minisplit(db_path, recent)
    _seed_basement(db_path, recent)
    _seed_outdoor(db_path)
    app = web.create_app(db_path=str(db_path))
    app.testing = True
    return app.test_client().get("/api/series?range=today").get_json()


def _overlay_block(js=None):
    """The body of `const TEMP_OVERLAYS = [ ... ];` in (stripped) dashboard.js.

    One definition rather than three copies of the pattern: the two inline
    copies this file shipped with had no `assert block`, so a reformatted
    closing bracket failed them with a bare `AttributeError` instead of a
    sentence naming the cause.
    """
    block = re.search(
        r"const TEMP_OVERLAYS = \[(.*?)\n  \];", js or DASHBOARD_JS, re.DOTALL
    )
    assert block, "no `const TEMP_OVERLAYS = [...]` in dashboard.js"
    return block.group(1)


def _overlay_entries(js=None):
    """`{key: entry-source}` for every object literal in `TEMP_OVERLAYS`."""
    entries = re.findall(r"\{(.*?)\}", _overlay_block(js), re.DOTALL)
    return {re.search(r'key:\s*"([\w-]+)"', e).group(1): e for e in entries}


def _overlay_payload_keys(js=None):
    """The `/api/series` keys `dashboard.js` draws onto the temp card."""
    keys = re.findall(r'payload:\s*"(\w+)"', _overlay_block(js))
    assert keys, "TEMP_OVERLAYS lists no payload keys"
    return keys


def _non_null_pairs(celsius_trace, fahrenheit_trace):
    """Aligned (C, F) values where both traces have a reading."""
    assert len(celsius_trace) == len(fahrenheit_trace)
    return [
        (c, f)
        for c, f in zip(celsius_trace, fahrenheit_trace, strict=True)
        if c is not None and f is not None
    ]


# --- the admission test ---


def test_every_temp_card_overlay_is_a_temperature_in_the_cards_unit(
    tmp_path, monkeypatch
):
    """Flip `TEMPERATURE_UNIT` and every overlay trace must follow.

    The overlays declare no `scale:`, so they are drawn against the indoor
    temperature's own y axis. A series that does not convert is a series in
    some other unit sharing that axis, and the chart then draws whichever of
    the two has the smaller range as a flat line -- a wrong answer, with
    nothing red anywhere.

    Harvested from `TEMP_OVERLAYS` rather than listed, so an overlay added
    tomorrow is covered without an edit here.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")
    for key in _overlay_payload_keys():
        assert key in in_c, f"TEMP_OVERLAYS draws `{key}`, /api/series does not ship it"
        pairs = _non_null_pairs(in_c[key], in_f[key])
        assert pairs, (
            f"`{key}` has no reading in the seeded range, so this guard "
            "cannot tell a temperature from a watt for it -- seed its source. "
            "If every key is empty and the clock is within a minute of local "
            "midnight, that is the `range=today` window rather than a missing "
            "fixture: the rows are seeded at now-60s and fall in yesterday."
        )
        for celsius, fahrenheit in pairs:
            assert fahrenheit == pytest.approx(units.from_celsius(celsius, "F")), (
                f"overlay `{key}` does not convert with the temperature unit, "
                "so it is not a temperature and must not share the temp "
                "card's y axis"
            )


def test_the_unit_guard_would_actually_catch_the_watts_series(tmp_path, monkeypatch):
    """Reachability control for the test above (#139).

    `minisplit_watts` is the concrete series that must never be an overlay,
    and it travels in the same payload one key away. Asserted here against the
    same two responses: it is a real number in both and the *same* number in
    both, so the conversion check above fails on it. Without this, an
    always-passing version of that test -- one whose traces were all null, or
    whose key list came back empty -- reads exactly the same.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")
    pairs = _non_null_pairs(in_c["minisplit_watts"], in_f["minisplit_watts"])
    assert pairs, "the fixture stored no measurable draw; the control is blind"
    for celsius_side, fahrenheit_side in pairs:
        assert celsius_side == fahrenheit_side
        assert celsius_side != pytest.approx(units.from_celsius(celsius_side, "F"))
    assert "minisplit_watts" not in _overlay_payload_keys()
    assert "minisplit-watts" not in web.METRIC_OVERLAYS["temp"]


# --- the three surfaces the marker binds together ---


def test_the_mini_split_overlay_is_declared_on_all_three_surfaces():
    """`minisplit-temp` in the constant, in the JS, and in the stylesheet.

    The two that are load-bearing **today**: without the constant the card is
    never marked `minisplit-temp`, so `dashboard.js` skips the entry and the
    line is simply absent; without the JS entry the marker is rendered and
    nothing draws from it.

    The CSS rule is the third surface and it is **forward insurance, not a
    live fix** — be clear about that, because the shape invites the opposite
    reading. Both it and the basement rule are specificity (0,3,0) and both set
    `--legend-rows: 4`, and the temp card carries both words, so deleting this
    rule changes nothing rendered right now. What it buys is independence: the
    basement rule is keyed to a different overlay and would take the fourth row
    with it if the basement line were ever dropped, leaving seven entries in
    three rows. That reason is the only one that holds, and it is why the rule
    is asserted here rather than left to the basement's.
    """
    assert "minisplit-temp" in web.METRIC_OVERLAYS["temp"]
    assert "minisplit-temp" in _overlay_entries()
    assert '.card[data-overlay~="minisplit-temp"] .u-legend' in DASHBOARD_HTML


def test_every_overlay_colour_variable_is_defined_in_both_palettes():
    """A `colorVar` with no `--series-*` declaration draws an invisible line.

    `cssVar()` returns the empty string for an undefined custom property and
    uPlot takes that as "no stroke", so the series is in the legend, in the
    data, taking up a reserved row, and not on the chart. Both palettes,
    because the dark block is a separate set of declarations and a light-only
    colour disappears for whoever reads this dashboard at night.

    Harvested from the JS so a new overlay cannot ship without a colour.
    """
    names = re.findall(r'colorVar:\s*"(--[\w-]+)"', _overlay_block())
    # `outdoor-temp` borrows OUTDOOR_METRICS.temp.colorVar, so it is named
    # elsewhere in the file; the literal ones are what this can harvest.
    assert names, "no literal colorVar in TEMP_OVERLAYS"
    assert "--series-minisplit-temp" in names
    dark = re.search(
        r"prefers-color-scheme:\s*dark\s*\)\s*\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL
    )
    assert dark, "no dark-mode block in dashboard.html"
    light = DASHBOARD_HTML.replace(dark.group(0), "")
    for name in names:
        assert re.search(re.escape(name) + r":\s*#[0-9a-fA-F]{3,8}\s*;", light), (
            f"`{name}` has no value in the light palette"
        )
        assert re.search(
            re.escape(name) + r":\s*#[0-9a-fA-F]{3,8}\s*;", dark.group(1)
        ), f"`{name}` has no value in the dark palette"


def test_the_mini_split_line_stays_out_of_the_card_header():
    """`prefix: null`, for the measured reason the basement's is null.

    A fourth number needs ~265px against the ~245px the header has at the
    card's width; the wrapped header pushes this card's chart below its
    row-mates' and grows the whole row. The header loop skips an overlay with
    no prefix, so this is the entire mechanism.
    """
    by_key = _overlay_entries()
    assert re.search(r"prefix:\s*null", by_key["minisplit-temp"])
    prefixed = [k for k, e in by_key.items() if not re.search(r"prefix:\s*null", e)]
    assert prefixed == ["outdoor-temp"], (
        f"{prefixed} would each print a number in the temp card header; only "
        "the outdoor one fits (measured at 1440 and 390)"
    )


def test_the_footer_tells_a_reader_what_a_break_in_the_line_means():
    """Each overlay's gap has a different cause, and the footer is where it says so.

    The traces are nulled rather than spanned once their source goes stale, so
    a break is the normal rendering of an outage -- and three lines on one card
    means three different outages with the same appearance.
    """
    footer = DASHBOARD_HTML[DASHBOARD_HTML.index("<footer>") :]
    assert "mini split" in footer
    assert "mini-split-poller gap" in footer
    # The draw is on the same feed and not on this card; say so where the
    # reader is, not only in the README.
    assert "watts" in footer


# --- the control on every presence assertion above ---


def test_a_commented_out_declaration_reads_as_absent():
    """Comment out each of the three surfaces and the guards must see nothing.

    This is the control the rest of this file rests on, and it is here because
    the first shape of the file failed it. Reading `dashboard.js` and
    `dashboard.html` raw, all six tests above passed with the `minisplit-temp`
    entry commented out of `TEMP_OVERLAYS` — the line gone from the browser,
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
        r'    \{\n      key: "minisplit-temp",.*?\n    \},\n', js_raw, re.DOTALL
    )
    assert entry, "no `minisplit-temp` object literal in dashboard.js to comment out"
    commented_js = js_raw.replace(
        entry.group(0),
        "".join(f"//{line}\n" for line in entry.group(0).splitlines()),
    )
    assert "minisplit-temp" in _overlay_entries(strip_js_comments(js_raw))
    assert "minisplit-temp" not in _overlay_entries(strip_js_comments(commented_js))

    # 2. The legend-row rule, block-commented.
    rule = '.card[data-overlay~="minisplit-temp"] .u-legend'
    assert rule in strip_markup_comments(html_raw)
    hidden_rule = html_raw.replace(rule, f"/* {rule}", 1).replace(
        "{ --legend-rows: 4; }\n.u-legend .u-value",
        "{ --legend-rows: 4; } */\n.u-legend .u-value",
        1,
    )
    assert rule not in strip_markup_comments(hidden_rule)

    # 3. The dark-mode colour, block-commented in place.
    decls = list(
        re.finditer(r"--series-minisplit-temp:\s*#[0-9a-fA-F]{3,8};", html_raw)
    )
    assert len(decls) == 2, (
        f"expected one `--series-minisplit-temp` per palette, found {len(decls)}"
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
    assert "--series-minisplit-temp" not in block.group(1)


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
