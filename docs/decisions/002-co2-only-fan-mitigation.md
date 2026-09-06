# ADR-002: un-retire fan mitigation on an absolute CO2 trigger

**Status:** accepted
**Date:** 2026-09-05
**Deciders:** tclancy, agent:opus
**Supersedes:** the retirement in [ADR-001](001-retire-automatic-fan-mitigation.md)
(the kill switch it introduced is kept)

## Context

ADR-001 retired automatic fan mitigation because the trigger's *duration*, not
its accuracy, was wrong: co2/voc spike events are true readings of degraded air
that stay true for half a day, so "mitigate the spike" meant a 32% duty cycle,
25 of those hours overnight. It named its own reversal condition:

> **Reverses if:** we find a trigger whose *duration* matches a tolerable
> fan-on window.

The proposal that prompted this was to trigger on CO2 instead — correlated
r = 0.80 with the metric of interest, and with a defensible absolute threshold,
"1000 ppm is a real number that means the same thing in July and January",
firing 13 times in eight weeks rather than 76.

That argument was made about **alerting**. It does not transfer to fans
unexamined: 13 alerts is 13 notifications, but 13 fan triggers is 13 runtime
*episodes* of unmeasured length, and length is exactly what ADR-001 retired the
feature over. So the reversal condition was tested against data rather than
assumed.

### Measurements

All figures replay the homelab DB over the 1365 covered hours from 2026-07-11
to 2026-09-06 (163,487 readings at 30 s). CO2 distribution: p50 = 534,
p90 = 754, p99 = 965, max = 1408.

A bare `co2 >= 1000` threshold:

| threshold | duty | fan-hours | episodes | median | longest | overnight h |
|-----------|------|-----------|----------|--------|---------|-------------|
| 800 ppm   | 7.6% | 103.9     | 69       | 10 min | 12.8 h  | 61.5        |
| 1000 ppm  | 0.7% |   9.6     | 13       | 10 min |  4.6 h  |  1.5        |
| 1100 ppm  | 0.3% |   4.7     |  4       |  1.1 h |  3.3 h  |  0.5        |

The reversal condition is met at 1000 ppm: a median episode of ten minutes is
the transient burst [#14](https://github.com/tclancy/awairelement/issues/14)
assumed and voc never was. Two cautions come with it:

- **The cliff is steep and 1000 sits on its edge**, 35 ppm above p99. At 800 the
  same recording gives a 7.6% duty cycle and 61 overnight hours. A winter
  baseline shift — windows shut — moves the house up this table without
  anything announcing it.
- **Hysteresis and the cap change the numbers**, so the table above is not what
  ships. Holding a run down to 900 rather than 1000 merges nearby episodes and
  raises duty to 1.29% (7 runs, longest 6.2 h, 1.6 h overnight). The 90-minute
  cap then brings it to **0.62%** (7 runs, longest 1.5 h, **no overnight
  running**), ending 3 of the 7 runs.

### The pm25 suppressor is not free

Cooking drives co2 and particulate together, so the suppressor vetoes the fans
in the conditions that call for them. Of the readings above 1000 ppm:

| pm25 threshold | share of fan-worthy time vetoed |
|----------------|---------------------------------|
| 25 (ADR-001)   | 36.9%                           |
| 100 (shipped)  | 21.2%                           |
| 150            |  6.7%                           |
| no suppressor  |  0%                             |

pm25 here is p99 = 44, p99.9 = 142, max = 364: ordinary cooking is intense
enough that a threshold of 25 switched mitigation off for over a third of its
working life.

## Decision

Mitigation is live again. `awair.fans.MITIGATION_RETIRED = False`.

1. **Trigger: absolute co2 with hysteresis.** On at `CO2_FAN_ON` (1000 ppm),
   off below `CO2_FAN_OFF` (900), hold between. The band is not optional at a
   30 s poll interval — a bare threshold commands the fans several times a
   minute while co2 wanders across the line. Spike events, the Awair-score gate
   and the `fans_engaged` latch are no longer consulted at all.
2. **One speed.** `FAN_SPEED = "speed1"`. The old speed1/2/3 ladder ranked
   co2+voc combinations and has nothing left to rank; max observed co2 is 1408,
   so a magnitude ladder would leave speed3 unreachable.
3. **A duration cap.** `FAN_MAX_RUN` (90 min) bounds one run regardless of co2,
   and `capped` latches until co2 recovers below `CO2_FAN_OFF` — without the
   latch the cap defeats itself, firing at 90 minutes while co2 is still 1100
   and letting the next poll's hysteresis turn the fans straight back on. This
   is ADR-001's suggested "duration cap" reversal path, and it is a working part
   of the controller rather than standby insurance.
4. **pm25 suppressor raised 25 → 100**, keeping its precedence over everything
   and its rate-limit bypass. Tom's stated goal includes fans dispersing cooking
   smoke *before* the smoke alarm trips, which any suppressor works against;
   100 is the compromise, and it still vetoes the smokiest 21%.
5. **Near-miss watchpoint re-based 15 → 50**, above p99 = 44 and half the new
   suppressor. At 15 it would fire on 3.4% of all polls; a watchpoint nobody
   reads never warns.
6. **Thresholds are env-tunable** (`AWAIR_CO2_FAN_ON`, `AWAIR_CO2_FAN_OFF`,
   `AWAIR_PM25_SUPPRESS`, `AWAIR_PM25_NEAR_MISS`, `AWAIR_FAN_MAX_RUN_MINUTES`)
   because 1000 sits close to p99: correcting for a seasonal shift should be an
   env edit and a restart, not a code deploy.
7. **`fan_state` gains `run_started_at` and `capped`**, migrated in place. A fan
   already running at migration time has no observed start, so `run_exhausted`
   reads NULL as not-yet-exhausted and the next poll adopts it into a fresh run
   rather than capping it on a start nobody saw.
8. **The kill switch stays.** `MITIGATION_RETIRED` remains the one-constant
   off-ramp ADR-001 built, and the poller banner keeps "disabled in code" as a
   third state distinct from "off" — they have different fixes.

ADR-001 specified reversal as three deliberate edits. Two are in this change:
the constant, and `test_fan_mitigation_ships_retired`, which becomes
`test_fan_mitigation_ships_live` and pins the shipped value just as firmly in
the other direction. **The third — homelab's `awair_fan_mitigation_enabled:
true` plus a deploy — is not, and until it lands the fans do not move.**

### The measurement is now a test

`test_replay_summer_2026_stays_under_two_percent_duty` replays
`tests/fixtures/co2_summer_2026.txt` — the real co2 series, snapped to a
5-minute grid — through the shipped rules and asserts the duty cycle stays under
2%. `test_replay_would_fail_at_a_lower_threshold` drops the threshold to 800 and
asserts the same replay exceeds it, so the gate is known to be able to fail.

This is summer data: windows open, low baseline. It gates the rules against
drift, and is not a promise about January.

## Consequences

- **Enables:** the fans run again, on a trigger measured at a 0.62% duty cycle
  with no overnight running, and `awair/fans.py` has a production caller again —
  ADR-001's "tests are the only thing keeping it honest" no longer applies.
- **Costs:** the pm25 suppressor still vetoes about a fifth of fan-worthy time,
  and it is the smokiest fifth — the case Tom most wants the fans for. Lowering
  or removing `AWAIR_PM25_SUPPRESS` is the lever, and it is an env edit.
- **Blocks / makes harder:** the `fans_engaged` latch, `FAN_SCORE_GATE` and the
  score-gate design from
  [#57](https://github.com/tclancy/awairelement/issues/57) are gone from the
  live path. The column and its history stay in `alert_events`; the code that
  wrote it does not. Reinstating a score gate means rebuilding it.
- **Watch for:** winter. 1000 ppm is 35 ppm above the observed p99, and the
  table above shows what a lower effective threshold costs. The near-miss log
  and the duty-cycle gate are the instruments; neither fires on a *seasonal*
  shift on its own, so a re-measurement after the first cold month is owed.
- **Reverses if:** the winter duty cycle climbs toward the double digits, or the
  cap starts ending most runs rather than the tail (at 60 min it already ends 6
  of 7). First lever is `AWAIR_CO2_FAN_ON`; second is the kill switch.

## Alternatives considered

- **Keep the spike-event machinery, narrow `FAN_TRIGGERS` to `("co2",)`.** The
  smallest diff, and it reuses tested code. Rejected: it keeps the
  baseline-relative threshold the proposal argues against — the whole point of
  "means the same thing in July and January" — and the measurements above would
  then describe something other than what ships.
- **Drop the pm25 suppressor entirely.** Tom was explicitly open to this, and it
  would fully serve the smoke-alarm goal. Rejected as the default because fans
  re-suspend settled dust and the veto is a safety property, not a tuning knob;
  raising it to 100 keeps the protection for genuine smoke events while
  returning most normal cooking to the fans. Reachable in one env variable.
- **Quiet hours (22:00–08:00).** ADR-001's other suggested reversal path, and
  the original complaint included 25 overnight hours. Rejected as redundant: the
  90-minute cap already takes overnight running to zero on the measured data,
  and quiet hours would additionally forfeit real mitigation for a problem that
  no longer exists.
- **No cap, trigger alone.** 1.29% duty and a 6.2 h longest run. Defensible, but
  it leaves the failure mode ADR-001 retired the feature over — a trigger that
  stays true for hours — with nothing bounding it.
