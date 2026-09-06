# ADR-001: retire automatic fan mitigation

**Status:** superseded by [ADR-002](002-co2-only-fan-mitigation.md) (2026-09-05)
**Date:** 2026-07-31
**Deciders:** tclancy (issue [#61](https://github.com/tclancy/awairelement/issues/61)), agent:opus

> **Superseded in part.** ADR-002 un-retires fan mitigation on an absolute CO2
> trigger, having measured the "reverses if" clause below as satisfied: a
> median episode of ten minutes and a 0.62% duty cycle. What survives from this
> ADR is the analysis of *why* the spike-event trigger failed, the
> `MITIGATION_RETIRED` kill switch, and the release-on-disable behaviour — all
> still live. What no longer holds is the retirement itself and the claim that
> `awair/fans.py` has no production caller.

## Context

Fan mitigation shipped from the [#10](https://github.com/tclancy/awairelement/issues/10)
design memo via [#14](https://github.com/tclancy/awairelement/issues/14): open a
co2/voc spike event, latch it once the Awair score agrees, run the ceiling fans
until the event clears. The design assumed spike events are transient bursts.

They are not, in this house. Measured on the homelab DB over the 303 hours
ending 2026-07-31 01:10 ET, the fans ran for **97.3 hours — a 32% duty cycle**,
across 13 unbroken spans. **25.2 of those hours fell between 22:00 and 08:00 ET.**
The longest single span was **34 hours** (07-18 09:48 → 07-19 19:49 ET); a
voc-ceiling event opened 07-30 14:33 ET was still open, with both fans at
speed1, 10.6 hours later when #61 was filed. Tom's verdict on #61: "let's kill
that behavior entirely because it's just annoying."

The gap is the trigger's *duration*, not its *accuracy*. A voc-ceiling event is
a true reading of degraded air; it just stays true for half a day, so
"mitigate the spike" became "run the ceiling fans a third of the time."

## Decision

We retire automatic fan mitigation **in place** rather than deleting it. A
module constant `awair.fans.MITIGATION_RETIRED = True` forces `config_from_env()`
to report `enabled=False` regardless of `AWAIR_FAN_MITIGATION_ENABLED`, logging a
warning when the environment still asks for fans so the dead variable is loud
rather than silent. The poller's startup banner reads
`fan mitigation: retired (#61)` — a third state, not a synonym for `off`, since
once the Ansible variable below is also `false` that warning never fires again
and nothing else would tell this decision apart from someone having simply left
fans off.

Everything from `events_to_engage` to `_log_pm25_observability` therefore has
**no production caller**. It stays intact and fully tested — the drive-path tests
build a `FansConfig` directly, and
`test_lifting_the_retirement_restores_the_whole_drive_loop` exercises the full
loop through `config_from_env` with the constant lifted — but it is not *live*,
and nothing outside the test suite will notice if it rots.

We also change what *disabled* means. `check_fans` used to early-return, so
flipping the kill switch mid-event stranded both fans at whatever speed the last
command set — exactly the live state on the box. It now calls `release_fans`,
which commands each fan off **once**, through the same `decide` no-op filter, and
then goes quiet. The release is unconditional rather than `desired_action`'s
verdict: on the live box the open voc event is latched, so that verdict is still
`speed1` and asking for it would no-op against `last_action` and strand the fans
exactly as the early return did. If the NodeMCU cannot be reached it gives up
after `RELEASE_MAX_ATTEMPTS` and sends one high-priority ntfy, so the retired
path does not become the only thing in the poller still talking to the network.

The homelab Ansible variable `awair_fan_mitigation_enabled` also flips to
`false` (homelab PR) so config and code agree, but neither change depends on the
other: the code retirement alone is sufficient on the box.

## Consequences

- **Enables:** the poller stops driving the fans on the next
  `git pull && ./restart.sh`, with no homelab deploy required, and any fan it
  left running is released off rather than frozen.
- **Blocks / makes harder:** `awair/fans.py` is now a large subsystem with no
  production caller. It is fully tested, but tests are the only thing keeping it
  honest — a future refactor can quietly break it without any deploy noticing.
  Issue [#23](https://github.com/tclancy/awairelement/issues/23) (rain as a
  second suppressor) is moot while this stands.
- **Reverses if:** we find a trigger whose *duration* matches a tolerable
  fan-on window — most likely a duration cap (fans off after N minutes
  regardless of the event) or a quiet-hours window, both of which #14 explicitly
  decided against. Reversal is **three edits, not one**, deliberately:
  1. `awair/fans.py` — `MITIGATION_RETIRED = False`.
  2. `tests/test_fans.py` — `test_fan_mitigation_ships_retired`, which pins the
     shipped value precisely so un-retiring cannot happen without a reviewer
     seeing it in the diff.
  3. homelab `ansible/group_vars/homelab/vars.yml` —
     `awair_fan_mitigation_enabled: true`, plus a deploy.

  Plus whichever of the duration cap or quiet-hours window made it worth having
  again.

## Alternatives considered

- **Flip `awair_fan_mitigation_enabled` to false and change no code.** This is
  what the kill switch was built for, but it needs a homelab deploy Tom has to
  run, and on its own it would have frozen both fans at speed1 permanently. It
  also leaves the README telling the next reader to flip it back on.
- **Delete `awair/fans.py`, its tests, and the `fan_state` table.** The
  cleanest end state, and the wrong one for "for now": it discards the #10
  design memo's whole implementation and the `fans_engaged` latch history in
  `alert_events`, so bringing fans back would mean re-deriving all of it.
- **Add a duration cap or quiet-hours window instead of retiring.** Tuning was
  already tried once — `FAN_SCORE_GATE` (the latch) was added in response to the
  earlier "closed the windows, fans came on" complaint. This is the second
  escalation on the same feature, so tuning it a third time is not what was
  asked for. Recorded above as the most likely reversal path.
