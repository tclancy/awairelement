# ADR-003: the basement watch attributes warmth to a trailing 24 hours

**Status:** accepted
**Date:** 2026-11-14
**Deciders:** tclancy (the rule and its answers), agent:opus (the basis, measured)

## Context

parsons-pulse [#205](https://github.com/tclancy/parsons-pulse/issues/205) asks
for a yellow when "the basement is above 62 degrees on a day where the highest
outdoor temperature is below that". Tom's clarifications on the issue settle
the window (weekdays 23:00–17:00, weekends narrowed to 00:00–07:00), the
source (this repo, which holds both the basement sensor and the Open-Meteo
feed) and the clear condition (the basement drops to 62 °F or below). They
leave one thing open: *which* outdoor high. His own gloss — "we can't
attribute this to the warmth of the day up 'til now" — reads as the calendar
day, and `/api/outdoor-today` already publishes extremes since local midnight,
so the calendar day was the cheap answer as well as the literal one.

It is also the wrong one, and the record says so rather than an argument.

## Decision

The watch compares the basement reading against the highest outdoor
temperature in the **trailing 24 hours ending at that reading**
(`basement_watch.ATTRIBUTION_LOOKBACK`), not against the local calendar day.

Measured on the homelab database on 2026-11-14, over the whole basement record
(2026-10-30 → 2026-11-14, 22,300 readings, 13,522 of them inside a watch
window):

| | readings |
|---|---|
| in-window and above 62 °F | 898, on two days only |
| — Fri 2026-11-06 (window 23:00–17:00) | 479 |
| — Sat 2026-11-07 00:00–06:59 | 419 |
| flagged on a calendar-day basis | **419** (all of the Saturday) |
| flagged on a trailing-24-hour basis | **0** |

Saturday's own outdoor high was 58.5 °F, so "the warmth of the day" cannot
account for a 62.4 °F basement and the calendar basis flags every reading of
that morning. The three days before it ran 76.5 °F, 72.5 °F and 67.5 °F — and
Friday, the other 479 readings, reached 67.5 °F. The basement was warm on
Saturday morning because the house had been through a warm spell. That is
exactly the attribution #205 exists to make, and a calendar-day basis cannot
make it: at 02:00, "the day so far" is two hours of night, and the watch
window is mostly night by construction.

Two supporting rules come with it:

- **The attribution window must be covered** (`MAX_ATTRIBUTION_GAP`, three
  hours). A `max` over a window with the afternoon missing returns the night's
  high, which in heating season is always below the threshold — so an outdoor
  poller outage would otherwise raise a yellow about the house. An uncovered
  window is *no verdict*, never a low one.
- **The rule fires per reading, not on the window's mean.** #205 did not say;
  the window is "when the basement is unoccupied" rather than a measurement
  interval, and a mean across 18 hours lets two hours at 66 °F hide inside
  sixteen at 60 °F.

## Consequences

- **Enables:** a yellow that means "something inside the house is warming the
  basement" rather than "it is November." The suppressed episode above is the
  proof: it is the only disagreement in the record, and the calendar basis is
  confidently wrong about it.
- **Blocks / makes harder:** the watch has **never fired on real data** — 0 of
  898 candidate readings. Its first firing is therefore untested against the
  house, and the badge's first appearance is a thing to go and look at rather
  than a thing to trust. A rule with no observed positive also cannot be
  tuned: if 62 °F turns out to be the wrong threshold, nothing in the record
  will say so.
- **Bounds, not hidden:** the latch is stateless, so it holds only as far
  back as `history_lookback()` reads — 42 hours of continuous warmth. Past
  that the original trip is outside every row the query returns, and the
  watch answers `unknown` rather than `clear`: no reading was ever at or
  below 62 °F, and clearing is the only thing #205 says that means. Raising
  the bound costs quadratically (one `attributable_high` per in-window
  reading); storing the trip is the fix if the case is ever observed.
- **Reverses if:** the watch fires during a long outdoor-poller outage (then
  `MAX_ATTRIBUTION_GAP` is too generous), or Tom reads a suppressed warm-spell
  morning as a yellow he wanted (then the basis goes back to the calendar day,
  which is one constant: delete `ATTRIBUTION_LOOKBACK`'s use in
  `attributable_high` in favour of local midnight).

## Alternatives considered

- **The local calendar day, as #205 literally says.** Rejected on the
  measurement above: it flags a warm-spell residue, which is the one thing the
  rule is for not doing.
- **The calendar day's *full* high rather than the high so far.** No better —
  Saturday 2026-11-07's whole-day high was 58.5 °F, so it flags the same 419
  readings. The problem is the day boundary, not the "so far".
- **The forecast high.** Would let the card flag in the morning rather than
  after the fact, and Tom's answer to that question on #205 pointed at the
  sensor record rather than a forecast. This repo also fetches Open-Meteo's
  *current* conditions only, so a forecast is new plumbing for a rule with no
  observed positive yet.
- **A latch stored in a table.** Rejected: the clear condition is a property
  of the rows, so the latch derives from them (`_uncleared_run`) with no state
  to get stuck and nothing to migrate.
- **Colouring the whole card, the way the hub colours a row.** Rejected: this
  app has no card verdicts, and inventing one here would put a second colour
  system beside parsons-pulse's. A badge states the fact and leaves the colour
  decision to the hub, the same way `/api/latest` ships open events rather
  than a colour.
