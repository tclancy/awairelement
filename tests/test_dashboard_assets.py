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

Browser-side behaviour (does the crosshair actually track a finger, does the
card stay the same height on hover) is verified against a headless Chromium at
several viewports; that run is captured in the PR that landed this file. What
lives here is the part CI can hold without a browser.
"""

import re
from pathlib import Path

import pytest

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
    """No layout shift on hover, and a third row can never spill the card."""
    body = _rule_body(html, ".u-legend")
    assert "overflow: hidden" in body
    assert "height: var(--legend-height)" in body
    root = _rule_body(html, ":root")
    assert re.search(r"--legend-height:\s*\d+px", root), (
        "--legend-height must be a fixed length; a content-derived height "
        "re-introduces the hover shift the nowrap lock was there to prevent"
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
    assert {name for name, _, _ in handlers} == {"touchstart", "touchmove", "touchend"}
    for name, body, passive in handlers:
        assert passive == "true", f"{name} listener is not passive"
        assert "preventDefault" not in body, f"{name} calls preventDefault"
