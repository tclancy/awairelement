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


def _overlay_scales(js=None):
    """`{payload-key: declared scale or None}` for every `TEMP_OVERLAYS` entry.

    The partition the two admission tests below read. Keyed on the payload key
    rather than the `key` word because what the guards check is the *series*:
    whether `/api/series`'s `minisplit_watts` converts with the temperature
    unit is a question about that list, not about a CSS marker.
    """
    scales = {}
    for entry in _overlay_entries(js).values():
        payload = re.search(r'payload:\s*"(\w+)"', entry)
        assert payload, f"a TEMP_OVERLAYS entry has no payload key: {entry!r}"
        declared = re.search(r'scale:\s*"(\w+)"', entry)
        scales[payload.group(1)] = declared.group(1) if declared else None
    assert scales, "TEMP_OVERLAYS lists no payload keys"
    return scales


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
    """Flip `TEMPERATURE_UNIT` and every SCALE-LESS overlay trace must follow.

    An overlay that declares no `scale:` is drawn against the indoor
    temperature's own y axis, and that is the claim omitting it makes. A series
    that does not convert is a series in some other unit sharing that axis, and
    the chart then draws whichever of the two has the smaller range as a flat
    line -- a wrong answer, with nothing red anywhere.

    Scoped to the scale-less entries since #139 put the mini split's draw on
    this card with `scale: "watts"` and a right-hand axis of its own. The
    scoping is where this test could go quietly blind, so note what holds it
    open: `test_the_unit_guard_would_actually_catch_a_non_temperature` proves
    the predicate still fails on a real non-temperature, and
    `test_the_two_admission_rules_partition_every_overlay` proves no entry
    escapes both halves. An entry that declared a scale it did not need would
    buy itself out of this test -- the axis guard in
    `test_dashboard_assets.py` is what makes that a visible change rather than
    a silent one.

    Harvested from `TEMP_OVERLAYS` rather than listed, so an overlay added
    tomorrow is covered without an edit here.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")
    shared_axis = [k for k, scale in _overlay_scales().items() if scale is None]
    assert len(shared_axis) >= 3, (
        f"only {shared_axis} share the card's axis; this guard was written "
        "against three temperatures and an empty list would pass it silently"
    )
    for key in shared_axis:
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


def test_the_unit_guard_would_actually_catch_a_non_temperature(tmp_path, monkeypatch):
    """Reachability control for the test above (#139).

    `minisplit_watts` is a real non-temperature travelling in the same payload
    one key away, so it is the honest probe for "would that conversion check
    fail on something?" Asserted against the same two responses: it is a real
    number in both and the *same* number in both, so the check above would go
    red on it. Without this, an always-passing version of that test -- one
    whose traces were all null, or whose key list came back empty -- reads
    exactly the same.

    **What this test used to assert, and why it had to change.** It ended with
    `"minisplit-watts" not in METRIC_OVERLAYS["temp"]` -- the draw must never
    be an overlay at all. #139 asked for the draw on this card and that is
    what shipped, on its own `scale: "watts"` axis, so the old assertion is
    now false and the guarantee moved: the draw is admitted *because* it
    declares a scale, and what it must not do is sit on the degree axis. That
    is `test_the_two_admission_rules_partition_every_overlay` plus the
    scale-less scoping above, not an exclusion list here.
    """
    in_c = _series(tmp_path, monkeypatch, "C")
    in_f = _series(tmp_path, monkeypatch, "F")
    pairs = _non_null_pairs(in_c["minisplit_watts"], in_f["minisplit_watts"])
    assert pairs, "the fixture stored no measurable draw; the control is blind"
    for celsius_side, fahrenheit_side in pairs:
        assert celsius_side == fahrenheit_side
        assert celsius_side != pytest.approx(units.from_celsius(celsius_side, "F"))
    assert _overlay_scales()["minisplit_watts"] == "watts", (
        "the draw is on the temp card without a scale of its own, so it is "
        "drawn to the degree ruler (#139)"
    )


def test_the_two_admission_rules_partition_every_overlay():
    """Every entry is held to exactly one of the two rules, and both are live.

    The admission test is no longer "is it a temperature". It is: **either the
    trace is in the card's own unit, or it declares its own scale.** Two tests
    enforce the two halves, so the failure this guards against is an entry that
    falls between them -- or, worse, a partition that collapses, since a test
    scoped to an empty half passes every time it runs.

    Asserted with both halves non-empty and against `METRIC_OVERLAYS`, which is
    the surface that actually admits an overlay onto the card: an entry in the
    JS that the constant never marks is never drawn, and vice versa.
    """
    scales = _overlay_scales()
    shared = {k for k, v in scales.items() if v is None}
    own = {k for k, v in scales.items() if v is not None}
    assert shared and own, (
        f"one half of the partition is empty (shared={shared}, own={own}); the "
        "test scoped to it cannot fail"
    )
    assert shared | own == set(scales)
    assert not shared & own
    keys = {
        re.search(r'key:\s*"([\w-]+)"', e).group(1): e
        for e in _overlay_entries().values()
    }
    assert set(keys) == set(web.METRIC_OVERLAYS["temp"]), (
        f"TEMP_OVERLAYS declares {sorted(keys)} and METRIC_OVERLAYS marks "
        f"{sorted(web.METRIC_OVERLAYS['temp'])} -- an entry on one surface "
        "only is either never drawn or never marked"
    )


def test_an_overlay_off_the_cards_axis_brings_its_own_unit():
    """A scaled overlay cannot borrow the card's `METRICS` unit or precision.

    The hover formatter reads `meta.unit`/`meta.digits` for a scale-less
    overlay and the overlay's own `unit`/`digits` for a scaled one. An entry
    that declares a scale and omits either renders `undefined` in the legend
    (for the unit) or throws inside `toFixed` (for the digits) -- and only
    while the cursor is over that card, which is the kind of break a test
    suite never sees and a hover does.
    """
    by_key = _overlay_entries()
    scaled = [
        key for key, entry in by_key.items() if re.search(r'scale:\s*"\w+"', entry)
    ]
    assert scaled, "no scaled overlay to check; this guard is inert"
    for key in scaled:
        entry = by_key[key]
        assert re.search(r'unit:\s*"[^"]+"', entry), (
            f"overlay `{key}` declares a scale and no `unit`, so its hover "
            "value prints `undefined`"
        )
        assert re.search(r"digits:\s*\d+", entry), (
            f"overlay `{key}` declares a scale and no `digits`, so its hover "
            "formatter calls toFixed(undefined)"
        )


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


def test_the_mini_split_draw_is_declared_on_all_four_surfaces():
    """`minisplit-watts` in the constant, the JS, the stylesheet and the footer.

    Three of those are the same contract the temperature line has. The fourth
    is the one this entry adds: it is the first line on this card that is NOT
    read against the card's own axis, so the footer has to say which axis it
    IS read against. A reader who takes its height for a temperature gets a
    wrong answer, and nothing on the chart tells them otherwise.

    The stylesheet surface is load-bearing here in a way the temperature's was
    not. `.card[data-overlay~="minisplit-temp"]` is forward insurance -- it
    sets the same four rows the basement rule already sets. This one sets
    *five*, and only below 380px, because eight populated entries want a fifth
    row at a 335px card and `overflow: hidden` would otherwise clip the draw's
    own legend entry. So it is asserted together with its breakpoint: move the
    rule out of the media query and the card is ~18.7px taller at every width;
    widen the query and the entry comes back clipped at 390px.
    """
    assert "minisplit-watts" in web.METRIC_OVERLAYS["temp"]
    assert "minisplit-watts" in _overlay_entries()
    rule = '.card[data-overlay~="minisplit-watts"] .u-legend'
    assert rule in DASHBOARD_HTML
    query = re.search(
        r"@media \(max-width:\s*(\d+)px\)\s*\{([^{]*\{[^}]*\}\s*)*\}",
        DASHBOARD_HTML[DASHBOARD_HTML.index(rule) - 2000 :],
    )
    scoped = re.search(
        r"@media \(max-width:\s*379px\)\s*\{\s*"
        + re.escape(rule)
        + r"\s*\{\s*--legend-rows:\s*5;\s*\}\s*\}",
        DASHBOARD_HTML,
    )
    assert scoped, (
        "the draw's legend rule is not `--legend-rows: 5` inside "
        "`@media (max-width: 379px)`. Measured: eight populated entries want "
        "five rows at a 335px card (375px viewport) and four at 340px (380px "
        "viewport), so both the value and the breakpoint are the measurement "
        f"(query found near the rule: {query.group(1) if query else None})"
    )
    footer = DASHBOARD_HTML[DASHBOARD_HTML.index("<footer>") :]
    assert "right-hand axis" in footer, (
        "the footer does not say the draw is read against the right-hand "
        "axis, so its height reads as a temperature"
    )


def test_the_temp_cards_legend_reservation_is_four_rows_by_default():
    """No rule the temp card matches may reserve fewer than four rows, or more.

    Every `[data-overlay~=...]` rule here is specificity (0,3,0), so the last
    one the card matches wins rather than the largest -- which means a new
    rule setting three rows would silently clip two entries, and one setting
    five unconditionally would make the card permanently taller than its
    row-mates and grow the whole grid row.

    Four is the measured default: eight populated entries at a 340px card are
    71.9px against the 74.8px four rows reserve. The one exception is the
    draw's rule, which is inside a media query and asserted with its
    breakpoint by the test above; it is excluded here by that nesting rather
    than by name.
    """
    unscoped = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", DASHBOARD_HTML)
    rules = re.findall(
        r'\.card\[data-overlay~="([\w-]+)"\] \.u-legend \{\s*--legend-rows:\s*(\d+)',
        unscoped,
    )
    on_card = {
        name: int(rows) for name, rows in rules if name in web.METRIC_OVERLAYS["temp"]
    }
    assert on_card, (
        "no unscoped --legend-rows rule is keyed to a temp-card overlay, so "
        "the card falls back to the three rows `.card[data-overlay]` sets "
        "and clips every entry past the sixth"
    )
    assert set(on_card.values()) == {4}, (
        f"temp-card legend reservations are {on_card}; every unscoped rule the "
        "card matches must say 4 -- fewer clips an entry, more is permanent "
        "blank space on a card whose height sets its grid row's"
    )


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
