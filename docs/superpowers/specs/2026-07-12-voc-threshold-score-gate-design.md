# TVOC threshold raise + score-gated fan latch

**Date:** 2026-07-12
**Status:** Approved
**Issue:** TVOC alerts firing on ordinary living; fans engaging when air is fine.

## Problem

Two coupled complaints, one symptom: the TVOC alert fired again when Tom closed
the windows, which turned the fans on.

### The threshold is below the baseline

`METRICS["voc"].ceiling` is `1000`. Measured against seven days of this house's
own readings (n=4,777):

| percentile | VOC |
|---|---|
| p50 | 428 |
| p75 | 1418 |
| p90 | 1806 |
| p95 | 2475 |
| p99 | 3127 |

VOC exceeds 1000 in **38% of all readings** (1835/4777). The "ceiling" is really
this house's ~65th percentile, so it fires on ordinary living rather than on
spikes. Both VOC events on record opened at the ceiling tier.

### There is no relevance gate

Any open co2/voc event turns the fans on, regardless of whether air quality has
actually degraded. The Awair score — the device's own composite verdict — drops
below 75 in only **4.0% of readings** (190/4777), and **every one of those 190
readings had VOC > 1000**. So the score is a genuine, non-redundant signal of
"this actually matters," and today nothing consults it.

Of the 190 bad-score readings, 164 had VOC > 2200.

## Design

### 1. Raise the ceiling: `spikes.py`

`METRICS["voc"].ceiling`: `1000` → `2200`.

2200 ppb is the conventional TVOC "poor/unhealthy" boundary and independently
lands at ~p93 of this house's data. It fires on 6.5% of readings (309/4777) and
still captures 86% of the moments where the score degrades (164/190).

Chosen over 2500 (~p95, no standards basis — just a percentile) and 3000 (~p99,
which combined with the score gate would mean the fans essentially never run).

Detection is otherwise untouched: the relative tier keeps working off MAD, and
the open/close hysteresis is unchanged.

### 2. Gate fans on the score, with a per-event latch

**The rule:** an open co2/voc event only becomes a fan trigger once the score has
dipped below 75 *while that event was open*. At that moment the event latches. The
score is then **never consulted again for that event's lifetime.**

This latch is the crux. Naively re-checking the score each poll would thrash: the
score distribution has p1=73 and p5=76, so it *lives* astride the 75 line and would
cross it repeatedly, oscillating the fans. Simple hysteresis (on <75, off >80) damps
that but leaves the system holding a live opinion about fan state for the whole event
— so it would re-engage fans after a manual shutoff. Latching means the system forms
its opinion exactly once per event.

**Why this cannot fight a manual override:** `decide()` already no-ops when
`state["last_action"] == action`. If the fans are killed at the wall, the DB still
believes `speed1`, the desired action is still `speed1`, so no command is issued.
The latch preserves this property by never letting the desired action flap.

The one deliberate exception: if a *second* metric opens (co2 joins voc), the target
genuinely changes (speed1 → speed2) and we will command it. That is a materially
worse air event and warrants overriding a manual off.

**pm25 remains supreme.** It still suppresses turn-on and forces a running fan off,
regardless of latch state. Particulate re-suspension outranks everything.

### 3. Components

**`db.py`**
- `alert_events` gains `fans_engaged INTEGER NOT NULL DEFAULT 0`, added to both
  `SCHEMA` and a guarded `_add_column` call in `_migrate()`. `DEFAULT 0` makes the
  ALTER safe against the live table and its currently-open row.
- `get_open_events()` returns `fans_engaged` in each event dict.
- `latest_score(conn, since)` — sibling of `latest_pm25`, same freshness bound, same
  "stale reads as no data" contract.
- `mark_fans_engaged(conn, event_id)` — sets the latch.

**`fans.py`**
- `FAN_SCORE_GATE = 75.0`, `SCORE_FRESHNESS = timedelta(minutes=5)` (mirrors
  `PM25_FRESHNESS`).
- `events_to_engage(open_events, latest_score)` — pure; returns the event ids that
  should latch this poll. No DB, trivially testable.
- `desired_action()` gains one filter: an open co2/voc event counts as a fan trigger
  only if `fans_engaged` is set. Speed laddering and pm25 suppression are untouched.
- `check_fans()` glue order: latch first (persist), then compute the action.

### Data flow, one poll

```
open events + fresh score
  -> events_to_engage()      # which events cross the gate now?
  -> mark_fans_engaged()     # persist the latch
  -> desired_action()        # sees only latched events
  -> decide()                # rate limit + no-op filter
  -> actuate()
```

## Edge cases

1. **Score missing or stale → do not engage.** Absent data means do not act. (The
   score shares a row with VOC, so in practice if we have VOC we have a score.)
2. **The currently-open event survives the migration** with `fans_engaged=0` and its
   threshold frozen at the old 1000 (`_close_reference` reads the stored value). It
   closes naturally when VOC drops below ~906 and will not spuriously drive the fans.
   No manual cleanup.
3. **Latch needs no clearing.** It lives on the event row; `get_open_events()` only
   returns open events, so a closed event's latch is unreachable by construction.

## Testing

- `events_to_engage`: score below / above / exactly at the gate; `None` score;
  already-latched events not re-latched; non-co2/voc events ignored.
- `desired_action`: latched vs unlatched events; unlatched event yields "off";
  pm25 suppression still wins over a latched event.
- Migration: `fans_engaged` added idempotently to a pre-existing `alert_events`.
- `spikes`: the new 2200 ceiling opens/does not open at the right values.

## Non-goals

- Detecting manual wall/remote fan changes. Still unobservable; still a soft-partial.
- Touching the relative tier, the co2 thresholds, or pm25 suppression.
