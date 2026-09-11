"""Structural guards on the dashboard's CSS and JS (#86, #87).

The dashboard is the one part of this project with no runtime under test:
`templates/dashboard.html` and `static/dashboard.js` are shipped verbatim to a
browser, and a regression in either is invisible to every other test here.

These are deliberately structural rather than cosmetic. Each one pins the
*mechanism* a fix depends on, not the wording around it:

- uPlot's own stylesheet sizes the chart root with `width: min-content`. A
  legend locked to one line therefore makes the whole chart as wide as the
  populated legend string, which is what pushed a 604px precipitation legend
  out of a 334px card (#86). Both halves of that — the width pin and the
  absence of the nowrap lock — have to hold together, so both are asserted.
- uPlot binds its cursor to mouse events only, so every chart needs the touch
  plugin explicitly. Adding a third chart factory and forgetting it is the
  realistic regression, so the test counts plots rather than naming them (#87).
- uPlot nulls its cursor index for an out-of-range left, and with sync
  publishing on that blanks every card at once, so the touch read has to
  clamp. That is asserted here because it is invisible in a mouse-only test:
  a mouse cannot leave the element while still reporting moves to it.
- `cursor.sync.key` IS the subscription — uPlot's constructor ends with
  `syncGroup.sub(self)` — so an explicit `sync.sub(plot)` beside it registers
  every chart twice and doubles peer cursor work (#90). Both halves are pinned
  together: the key must be present *and* nothing may subscribe by hand.

Browser-side behaviour (does the crosshair actually track a finger, does the
card stay the same height on hover) is verified against a headless Chromium at
several viewports; that run is captured in the PR that landed this file. What
lives here is the part CI can hold without a browser.
"""

import re
from pathlib import Path

import pytest

from tests._helpers import strip_js_comments

REPO = Path(__file__).resolve().parent.parent
DASHBOARD_HTML = REPO / "templates" / "dashboard.html"
DASHBOARD_JS = REPO / "static" / "dashboard.js"
UPLOT_CSS = REPO / "static" / "uplot.min.css"


@pytest.fixture(scope="module")
def html():
    return DASHBOARD_HTML.read_text()


@pytest.fixture(scope="module")
def js():
    return DASHBOARD_JS.read_text()


def _rule_body(css, selector):
    """The declarations of the first `selector { ... }` rule, or None."""
    match = re.search(
        r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.MULTILINE
    )
    return match.group(1) if match else None


def test_uplot_still_sizes_its_root_with_min_content():
    """The premise #86's fix rests on, checked against the vendored library.

    If a future uPlot drops `width: min-content`, the `.plot .uplot` override
    below stops being load-bearing and this whole file needs re-reading rather
    than quietly continuing to pass.
    """
    body = _rule_body(UPLOT_CSS.read_text(), ".uplot")
    assert body is not None, "no `.uplot` rule in the vendored uplot.min.css"
    assert "width: min-content" in body, (
        "vendored uPlot no longer sizes its root with min-content; re-derive "
        "#86 before trusting the `.plot .uplot { width: 100% }` override"
    )


def test_chart_root_is_pinned_to_the_card_width(html):
    body = _rule_body(html, ".plot .uplot")
    assert body is not None, "`.plot .uplot` rule is gone — see #86"
    assert "width: 100%" in body


def test_legend_is_not_locked_to_one_line(html):
    """`white-space: nowrap` on the legend is what caused #86.

    It reads like a clipping instruction and is the opposite: it makes the
    legend's min-content width the entire populated string, which uPlot then
    grows the chart root to match.
    """
    body = _rule_body(html, ".u-legend")
    assert body is not None, "`.u-legend` rule is gone"
    assert "nowrap" not in body, (
        "the one-line lock is back; it grows the chart root past the card (#86)"
    )


def test_legend_reserves_a_fixed_height_and_clips(html):
    """No layout shift on hover, and an extra row can never spill the card.

    The `height:` here has to be anchored rather than substring-matched:
    `min-height: var(--legend-rows)...` contains the same text and is exactly
    the regression, since a min-height lets the legend grow on hover again.
    """
    body = _rule_body(html, ".u-legend")
    assert body is not None, "`.u-legend` rule is gone"
    assert "overflow: hidden" in body
    assert re.search(r"(?:^|;)\s*height:\s*calc\(var\(--legend-rows\)", body), (
        "legend height must be a plain `height`, not a min-/max- variant"
    )
    root = _rule_body(html, ":root")
    assert root is not None, "`:root` custom properties are gone"
    assert re.search(r"--legend-rows:\s*\d+\s*;", root), (
        "--legend-rows must be a whole number of rows; a content-derived "
        "height re-introduces the hover shift the nowrap lock was there to stop"
    )
    assert re.search(r"--legend-row-height:\s*[\d.]+em\s*;", root), (
        "the row height must scale with the font, or a browser-imposed "
        "minimum font size clips the last row instead of growing the box"
    )


def test_a_card_carrying_an_overlay_reserves_an_extra_legend_row(html):
    """An overlay card carries a 5th legend entry — #42's pressure, #109's outdoor.

    Measured at 375px and 390px it wraps to three rows where every other card
    needs two, so without its own reservation `overflow: hidden` clips the
    fifth reading away entirely — on the phone #87 exists to serve.

    Asserted on the *attribute* selector rather than on either card's name.
    The rule shipped for #42 named `[data-outdoor="precipitation"]`, and the
    day #109 put an overlay on the indoor temp card that selector went stale
    silently: the new card drew its fifth entry into a two-row box and clipped
    it, with nothing failing. A selector keyed on the marker covers the next
    one without an edit.
    """
    body = _rule_body(html, ".card[data-overlay] .u-legend")
    assert body is not None, (
        "no legend-height override keyed on `data-overlay` — a per-card "
        "selector goes stale the moment another card grows an overlay"
    )
    assert re.search(r"--legend-rows:\s*3\s*;", body)
    assert not re.search(r'\.card\[data-outdoor="[a-z]+"\]\s*\.u-legend', html), (
        "a legend reservation is still keyed to one named card; key it on "
        "`[data-overlay]` so every overlay card is covered"
    )


def test_the_overlay_series_shares_the_cards_own_y_axis(js_code):
    """#109's outdoor trace must NOT get a scale of its own.

    Both series are a temperature in the same unit, and the gap between them IS
    the thing the chart was asked for — "how outdoor affects indoor". Give the
    outdoor line its own auto-fitted axis and the two are drawn to different
    rulers: a 1° indoor drift and a 20° outdoor swing become the same stroke on
    screen, and a reader cannot tell a well-insulated house from a leaky one.

    This is a real temptation rather than a hypothetical, because the pattern
    beside it does exactly that on purpose — #42 puts pressure on `scale:
    "pressure"` with a fixed range, correctly, since inHg and inches of rain
    are different quantities. Copying that line onto a second temperature is
    the likeliest way this regresses.
    """
    match = re.search(
        r"seriesConfig\.push\(\{[^}]*label:\s*OUTDOOR_METRICS\.temp\.name.*?\}\)",
        js_code,
        re.S,
    )
    assert match, (
        "no outdoor series pushed with `label: OUTDOOR_METRICS.temp.name` — "
        "if the overlay moved, this guard has to move with it"
    )
    assert not re.search(r"\bscale\s*:", match.group(0)), (
        "the outdoor temperature series declares its own `scale:`, so the two "
        "temperatures are drawn against different rulers (#109)"
    )


def test_plot_container_clips_its_contents(html):
    """The outer guarantee: nothing inside a card paints outside it."""
    body = _rule_body(html, ".plot")
    assert body is not None
    assert "overflow: hidden" in body


def test_every_chart_registers_the_touch_cursor_plugin(js):
    """Counted, not named — a new chart factory that forgets it fails here."""
    plots = len(re.findall(r"\bnew uPlot\(", js))
    registrations = len(re.findall(r"(?<!function )\btouchCursorPlugin\(\)", js))
    assert plots >= 2, f"expected at least two uPlot instantiations, found {plots}"
    assert registrations == plots, (
        f"{plots} charts but {registrations} touchCursorPlugin() registrations — "
        "a chart without it has no touch path to the crosshair (#87)"
    )


def test_touch_cursor_publishes_to_the_sync_group(js):
    """`setCursor(opts)` moves one chart; the third argument moves all of them.

    Without `_pub` the scrubbed card updates and the other five stay at "--",
    which is a subtler version of the bug #87 is about.
    """
    call = re.search(r"u\.setCursor\(\s*\{[^}]*\},\s*([^)]*)\)", js)
    assert call is not None, "touch handler no longer calls u.setCursor"
    args = [a.strip() for a in call.group(1).split(",") if a.strip()]
    assert args == ["true", "true"], (
        f"expected setCursor(opts, _fire, _pub) with both true, got {args}"
    )


def test_touch_handlers_track_the_finger_on_this_chart(js):
    """`touches` is every finger on the screen; `targetTouches` is ours.

    With a finger already resting anywhere on the page, `e.touches[0]` is that
    stationary finger: `start` records its coordinates, every move re-reads it,
    dx/dy stay at zero, the axis never locks and the drag silently does
    nothing. The same wrong-finger read turns a pinch into a scrub.
    """
    assert "e.touches[" not in js, "touch handlers must read e.targetTouches"
    assert js.count("e.targetTouches[0]") == 2, (
        "touchstart and touchmove both read the finger on this chart"
    )
    assert "e.changedTouches[0]" in js, (
        "touchend has no live touches — the lifted finger is in changedTouches"
    )


def test_touch_listeners_stay_passive(js):
    """`touch-action: pan-y` does the scroll arbitration, not preventDefault.

    A non-passive touch listener here would let a future edit block scrolling
    on every chart — the exact failure #36 fixed.
    """
    handlers = re.findall(
        r'addEventListener\(\s*"(touch\w+)"(.*?)\{\s*passive:\s*(\w+)\s*\}',
        js,
        re.DOTALL,
    )
    assert handlers, "no touch listeners found in dashboard.js"
    # Superset, not equality: the three below are what the crosshair needs, and
    # a fourth (touchcancel) is hardening, not a regression.
    assert {name for name, _, _ in handlers} >= {
        "touchstart",
        "touchmove",
        "touchend",
    }
    for name, body, passive in handlers:
        assert passive == "true", f"{name} listener is not passive"
        assert "preventDefault" not in body, f"{name} calls preventDefault"


def test_touch_reads_are_clamped_to_the_plot_area(js):
    """An unclamped left below zero blanks every synced card at once.

    Implicit touch capture keeps delivering touchmove after the finger leaves
    the element, and the plot area starts ~65px into the card, so scrubbing
    left toward older data crosses zero well before the finger leaves the
    screen. uPlot then nulls the cursor index and — because the read publishes
    to the sync group — every legend on the page snaps back to "--" and stays
    there, since touchend only re-reads for a tap.
    """
    assert re.search(r"const clamp = \(", js), "the clamp helper is gone"
    call = re.search(r"u\.setCursor\(\s*\{(.*?)\},", js, re.DOTALL)
    assert call is not None, "touch handler no longer calls u.setCursor"
    args = call.group(1)
    assert "left: clamp(" in args, "left is passed to setCursor unclamped"
    assert "top: clamp(" in args, "top is passed to setCursor unclamped"


@pytest.fixture(scope="module")
def js_code(js):
    """dashboard.js with comments stripped — see `strip_js_comments`."""
    return strip_js_comments(js)


def test_the_comment_stripper_keeps_strings_and_drops_comments():
    """The helper the two sync guards rest on, checked against its own traps."""
    stripped = strip_js_comments(
        'const u = "http://x//y";\n'
        "sync.sub(a); // sync.sub(b)\n"
        "/* sync.sub(c)\n   sync.sub(d) */\n"
        "const t = `//not a comment`;\n"
        "const esc = 'it\\'s // fine';\n"
        "const apos = /it's/;\n"
        "const slashes = s.replace(/\\/\\//g, ''); \n"
        "const cls = /[/*]/;\n"
        "const div = total / count; // trailing\n"
    )
    assert '"http://x//y"' in stripped, "a URL in a string was eaten as a comment"
    assert "`//not a comment`" in stripped, "template literal truncated"
    assert "'it\\'s // fine'" in stripped, "escaped quote ended the string early"
    assert "/it's/" in stripped, "an apostrophe in a regex opened a phantom string"
    assert "/\\/\\//g" in stripped, "a regex containing // was read as a comment"
    assert "/[/*]/" in stripped, "`/` inside a character class ended the regex"
    assert "total / count" in stripped, "division was mistaken for a regex literal"
    assert "trailing" not in stripped, "a comment after division survived"
    assert stripped.count("sync.sub(") == 1, (
        f"expected only the live call to survive, got {stripped.count('sync.sub(')}"
    )


def test_every_chart_joins_the_sync_group_through_its_cursor_config(js_code):
    """`cursor.sync.key` is the *whole* mechanism — uPlot subscribes on construct.

    Counted rather than named, like the touch-plugin guard above: the realistic
    regression is a new chart factory that renders fine and silently never joins
    the group, so its crosshair neither follows the other cards nor leads them.

    The key's *value* is pinned, not just the `sync: { key: ... }` shape. A
    shape-only match passes on `key: null` — and `uPlot.sync(null)` returns a
    fresh, uncached group every call, so that chart is subscribed to a private
    group of one. It renders correctly and its crosshair silently stands alone,
    which is this test's whole subject. A typo'd literal (`"awiar"`) does the
    same thing. Both were green against a shape-only match.

    This is also the reachability control for the absence assertion below. That
    one asserts something is *missing* from this file, and a file that stopped
    instantiating charts would satisfy it vacuously; pinning the positive
    mechanism to the chart count is what keeps the pair honest.
    """
    plots = len(re.findall(r"\bnew uPlot\(", js_code))
    keyed = len(re.findall(r"sync:\s*\{\s*key:\s*sync\.key\b", js_code))
    assert plots >= 2, f"expected at least two uPlot instantiations, found {plots}"
    assert keyed == plots, (
        f"{plots} charts but {keyed} `cursor: {{ sync: {{ key: sync.key }} }}` "
        "configs — a chart without one is absent from the shared sync group and "
        "its crosshair stands alone. If you hoisted the cursor config into a "
        "shared object or spread it in, count that instead of loosening this "
        "(#90)"
    )


def test_no_chart_subscribes_to_the_sync_group_a_second_time(js_code):
    """uPlot's constructor already ran `syncGroup.sub(self)`; a manual sub doubles it.

    Verified against the vendored v1.6.32, whose constructor ends
    `return Gi.sub(k), ...` with `Gi = uPlot.sync(cursor.sync.key)` — so passing
    a key *is* the subscription. An explicit `sync.sub(plot)` on the next line
    put every chart in the group's `plots[]` twice, and `pub()` walks that array,
    so each cursor move ran `updateCursor` twice on all seven peers. Measured on
    the live dashboard: 16 subscriptions for 8 cards and 2 pub calls per peer per
    move, against 8 and 1 after removal — with every peer's `cursor.idx` and
    `cursor.left` identical at both probe positions (#90).

    Harmless while a mouse was the only cursor source. #87 added a touch path
    that publishes on every `touchmove`, which is where the doubling started
    costing something on the device least able to absorb it.

    Bans *any* explicit `.sub(`, not the literal `sync.sub(`: a group reached as
    `uPlot.sync("awair").sub(plot)` or held in another local is the same defect
    in a different spelling, and a one-literal ban waves both through.
    """
    plots = len(re.findall(r"\bnew uPlot\(", js_code))
    assert plots >= 2, f"expected at least two uPlot instantiations, found {plots}"
    explicit = re.findall(r"\.sub\s*\(", js_code)
    assert not explicit, (
        f"{len(explicit)} explicit sync-group subscription(s) in dashboard.js — "
        "`cursor.sync.key` already subscribes each chart at construction, so "
        "this registers every plot twice and doubles peer cursor work (#90)"
    )


def test_the_opening_range_is_read_from_the_dom_not_written_into_the_js(js):
    """`state.range` must be derived from the pressed button, never a literal (#108).

    The default used to be written down three times — twice in `web.py`, once
    as `aria-pressed="true"` in the template, once here — and a change to any
    one of them left the other two behind. `web.DEFAULT_RANGE` is now the only
    copy, and this is the half of that no Flask test can reach: `dashboard.js`
    is shipped verbatim to a browser.

    Asserted on the *mechanism* rather than the absent string. A ban on the
    literal `"7d"` would pass the day somebody wrote `"today"` here instead —
    which is the same defect, and the one a reader implementing #108 by hand
    is most likely to introduce.
    """
    match = re.search(r"const state\s*=\s*\{(.*?)\}\s*;", js, re.S)
    assert match, "no `const state = { ... };` in dashboard.js"
    body = match.group(1)
    initialiser = re.search(r"\brange:\s*([^,]+),", body)
    assert initialiser, f"no `range:` key in the state initialiser: {body!r}"
    expression = initialiser.group(1)
    assert "dataset.range" in expression, (
        "`state.range` is initialised from "
        f"{expression.strip()!r} rather than from a button's `dataset.range` — "
        "the opening range belongs to `web.DEFAULT_RANGE` and is read back out "
        "of the rendered markup (#108)"
    )
    assert not re.search(r"""["'`]""", expression), (
        f"`state.range` is initialised from {expression.strip()!r}, which "
        "contains a string literal -- that is a second copy of a default that "
        "lives in `web.DEFAULT_RANGE` (#108). Scoped to the captured "
        "expression rather than to the whole state object, and scoped to ANY "
        "quote rather than to one immediately after `range:`: a literal in the "
        "SECOND position of a fallback -- `pressed ? pressed.dataset.range : "
        '"7d"` -- satisfies both the mechanism assertion above and a '
        "`range:\\s*[\"']` ban, and is the same defect."
    )


def test_the_template_presses_a_range_button_by_derivation_not_by_hand(html):
    """No range name and no pressed state is typed into `dashboard.html` (#108).

    The `<button>` row is rendered from `web.RANGE_LABELS` and pressed from
    `web.DEFAULT_RANGE`. Both halves are asserted, because either one alone
    leaves a working copy of the defect: a hand-written button list drifts from
    the ranges the endpoints accept, and a hand-written `aria-pressed="true"`
    drifts from the range they default to.

    The CSS rule `.ranges button[aria-pressed="true"]` is a *selector*, not a
    pressed button, so the search is scoped to the `<nav class="ranges">`
    element rather than run over the whole file.
    """
    nav = re.search(r'<nav class="ranges".*?</nav>', html, re.S)
    assert nav, 'no `<nav class="ranges">` in dashboard.html'
    markup = nav.group(0)
    assert 'aria-pressed="true"' not in markup, (
        "a range button is pressed by hand in the template — press it from "
        "`web.DEFAULT_RANGE` instead (#108)"
    )
    assert not re.search(r'data-range="[a-z0-9]', markup), (
        "a range name is typed into the template — render the row from "
        "`web.RANGE_LABELS` so it cannot drift from the ranges the endpoints "
        "accept (#108)"
    )
    assert "range_labels" in markup and "default_range" in markup


def test_the_overlay_trace_breaks_rather_than_spanning_a_stale_gap(js_code):
    """`spanGaps: false` on the outdoor series, and the reason is not cosmetic.

    `web._outdoor_temp_on_grid` nulls the trace once the last outdoor
    observation is older than `_OUTDOOR_CARRY_MAX_AGE_SECONDS`, so a dead
    outdoor poller arrives at the browser as a run of nulls. Spanned, uPlot
    joins the two live ends into one straight line across the outage — and a
    flat outdoor trace beside a moving indoor one reads as "the weather held
    steady", which is the single most misleading thing this chart can say.

    uPlot's default is `spanGaps: false`, so this asserts the key is present
    and false rather than merely absent: an explicit `true` is the regression,
    and "absent" and "present and false" are different edits to review.
    """
    match = re.search(
        r"seriesConfig\.push\(\{[^}]*label:\s*OUTDOOR_METRICS\.temp\.name.*?\}\)",
        js_code,
        re.S,
    )
    assert match, "the outdoor temperature series is no longer pushed here"
    assert re.search(r"spanGaps:\s*false", match.group(0)), (
        "the outdoor trace spans its gaps, so an outdoor-poller outage draws "
        "as a flat line across the outage instead of a break (#109)"
    )


def test_the_overlay_series_is_actually_given_a_data_column(js_code):
    """A 5th series config with no 5th data array is a blank chart, not a bug report.

    uPlot indexes `data` by series position, so the config and the data array
    have to grow together. They are built in two separate places here — the
    ternary on `data` and the `push` onto `seriesConfig` — under one flag, and
    editing one without the other is the realistic slip. Neither structural
    guard above can see it: both read the series config alone.
    """
    match = re.search(
        r"const data = overlayOutdoor\s*\?\s*\[(.*?)\]\s*:\s*\[(.*?)\]", js_code, re.S
    )
    assert match, "no `overlayOutdoor` branch building the temp card's data array"
    columns = [
        [part.strip() for part in group.split(",") if part.strip()]
        for group in match.groups()
    ]
    with_overlay, without = columns
    # ORDER, not membership. uPlot maps `data[i]` to `series[i]`, so
    # `[series.t, outdoorTemp, series.min, series.max, series.avg]` contains
    # every column, differs by exactly one comma, and silently makes the
    # outdoor trace the hidden `low` band while the real average is drawn as
    # the overlay — every number on the card wrong, nothing red. A membership
    # assertion cannot see that, and it was the mutant this test missed on
    # its first draft (#109 review).
    assert without == ["series.t", "series.min", "series.max", "series.avg"], (
        f"the non-overlay data columns changed shape: {without}"
    )
    assert with_overlay == [*without, "outdoorTemp"], (
        "the overlay branch must be the base columns with `outdoorTemp` "
        f"appended, in that order — got {with_overlay}. The pushed series is "
        "appended to `seriesConfig` last, so its data column has to be last "
        "too, or uPlot pairs every series with the wrong array."
    )
