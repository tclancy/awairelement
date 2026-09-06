# Glossary

<!--
Canonical vocabulary for this project. Grep this file before naming a new
domain concept (variable, class, PR title, README prose). If a term is here,
use it verbatim. If you're introducing a new term, add a one-line row in the
same PR that lands the code.
-->

## Terms

<!-- Alphabetical by canonical name. One line per term. -->

- **actuate** — Fire the intended fan state at the NodeMCU endpoint (`awair.fans.actuate`). Distinct from **decide**: `decide` produces the intent, `actuate` sends it.
- **ceiling** — Per-metric absolute alert threshold on `MetricConfig` (`spikes.MetricConfig.ceiling`). A `ceiling`-tier event opens when the last 2 samples exceed it (bypasses the relative-baseline path). Dashboard renders it as a dashed reference line so autoscaled Y-axes don't visually collapse "still elevated" into "cleared" (#25). Values: co2=1200, voc=2200, pm25=35. Unrelated to the ceiling **fan** hardware; naming collision is intentional-and-fine.
- **decide** — In `awair.fans`, the rate-limit + no-op filter around a **desired action**. Returns a `MitigationDecision` or `None`.
- **desired action** — The verdict `awair.fans.desired_action` derives from the latest co2, the latest pm25 and the fan's **run**: a `Verdict` carrying `"off"` or `"speed1"`, a reason, and the **capped** flag to persist. Since ADR-002 it never consults **event**s.
- **DeviceHealth** — Snapshot of last-successful-fetch state used to detect the transition between healthy and stale/unreachable readings; owns the `ok`, `since`, and `last_status` fields on `awair.monitor.DeviceHealth`.
- **engaged** — *Historical (removed in ADR-002).* An open co2/voc **event** whose `fans_engaged` latch was set, i.e. one the Awair score had agreed with. Fans no longer read events at all, so nothing writes the latch; the `alert_events.fans_engaged` column and its history remain.
- **capped** — Per-fan flag set when a **run** hits `FAN_MAX_RUN` (90 min). While set the fans stay off *even though co2 still calls for them*, clearing only once co2 falls below `CO2_FAN_OFF`. Without the latch the cap would defeat itself — it fires while co2 is still high, and the **hysteresis band** would re-engage on the next poll.
- **event** — A row in the `events` table representing an open or closed spike/threshold violation. Rows are opened by `spikes.evaluate` and closed by `db.close_event`.
- **fan mitigation** — The whole loop: `desired_action` → `decide` → `actuate`. Runs the ceiling fans while co2 is high and stops when the air clears or the **cap** fires. Retired in #61, live again on an absolute co2 trigger as of [ADR-002](docs/decisions/002-co2-only-fan-mitigation.md).
- **fan_state** — SQLite row (one per fan) tracking `last_action` (last known / last confirmed physical state), `last_command_at` (when the poller last tried to command the fan — used for the 1-cmd/min rate limit), `run_started_at` (when the current **run** began, NULL when off) and `capped`.
- **hysteresis band** — The range between `CO2_FAN_OFF` (900 ppm) and `CO2_FAN_ON` (1000 ppm), inside which `co2_calls_for_fans` holds whatever the fan is already doing. Fans start at or above 1000 and stop below 900. Without the band a single threshold would command the fans several times a minute at a 30 s poll interval.
- **run** — One unbroken stretch of a fan being driven, from the command that starts it to the command that stops it. Bounded by `FAN_MAX_RUN`; tracked per fan via `fan_state.run_started_at`. A **run** is coarser than a co2 excursion — the **hysteresis band** merges nearby excursions into one run.
- **FansConfig** — Immutable config for fan mitigation: `enabled`, `fan_host`, `fan_ids`. Built from env by `awair.fans.config_from_env`.
- **fetch** — The single-shot HTTP GET against the Awair Element Local API that returns one reading payload; built by `poller.make_fetch(url)`.
- **metric** — A named channel on a reading (`co2`, `voc`, `pm25`, `temp`, `humid`, etc.); the `MetricConfig` dataclass in `awair.spikes` binds a metric to its thresholds.
- **MetricConfig** — Per-metric threshold + hysteresis config used by `spikes.evaluate` to decide whether to open, close, or renotify an event.
- **MitigationDecision** — Immutable dataclass emitted by `awair.fans.decide`: `fan_id`, target `action`, and human-readable `reason`. Consumed by `actuate`.
- **notifier** — The `awair.alerts.Notifier` object that fans an event out to ntfy; injected into `poller.handle_device_health` and `monitor.check_metrics`.
- **outdoor reading** — One row in the `outdoor_readings` table produced by `awair.outdoor.parse_reading(weather, air_quality, received_at)`. Sibling of **reading** (indoor). Different cadence (15 min at source vs. 30 s indoor) and different upstream (Open-Meteo vs. Awair Element local API), so kept in its own table rather than widening `readings`.
- **outdoor poll** — One iteration of `awair.outdoor.poll_once`: `fetch_weather` + `fetch_air_quality` → `parse_reading` → `insert_outdoor_reading`. An AQ-endpoint outage does not wedge the weather write — the row lands with AQ columns NULL and the poll returns `"partial"`.
- **poll** — One iteration of the poller loop: `fetch` → `parse_reading` → `insert_reading` → `check_metrics` → `check_fans`. Distinct from **fetch** — a poll wraps a fetch with DB + monitor side effects.
- **pressure** — Mean sea-level barometric pressure. Stored in the `outdoor_readings.pressure` column in hPa (Open-Meteo's native unit) and converted to inHg at the `/api/outdoor-series` boundary (`_HPA_PER_INHG = 33.8639`). Rendered on the precipitation card as a second Y-axis layer (fixed range 28.5–31.0 inHg) using each bucket's `min` — the trough is the storm-front signal, not the average (#42).
- **reading** — One row in the `readings` table; produced by `poller.parse_reading(payload, received_at)`.
- **release** — Commanding a fan `off` once because **fan mitigation** is not driving it, as opposed to because the air cleared (`awair.fans.release_fans`, reason string `RELEASE_REASON`). A disabled poller releases rather than freezing, and then goes quiet — it does not re-command a fan switched on at the wall afterwards.
- **disabled in code** — The state `awair.fans.MITIGATION_RETIRED = True` produces: `config_from_env()` reports disabled whatever `AWAIR_FAN_MITIGATION_ENABLED` says, and the poller **release**s the fans. Called *retired* while #61 stood; ADR-002 turned it off but kept the switch, so it is now the off-ramp rather than the status quo. Distinct from **disabled**, the ordinary off position of the env var — the two have different fixes, which is why the startup banner names them separately.
- **series** — A bucketed time-window of readings for the dashboard, produced by `awair.series.bucket(points, bucket_seconds)`. **Not** a synonym for `metric_history` (which returns raw points).
- **spike** — An event triggered by threshold + hysteresis logic in `awair.spikes`; distinct from a **stale device**, which is the health-check equivalent handled by `monitor` + `DeviceHealth`.
- **near-miss** — A pm25 reading at or above `PM25_NEAR_MISS_THRESHOLD` (50 µg/m³) but below the **suppressor** threshold (100). Logged at INFO from `check_fans` so we can watch the suppressor's headroom shrink before it ever fires (#15). Behavior-neutral — it does not change the fan verdict.
- **suppressor** — A metric that *blocks* fan mitigation rather than triggering it. PM2.5 is the sole suppressor (fans re-suspend particulate); pm25 at or above 100 µg/m³ forces fans off regardless of co2, and bypasses the rate limit doing so. Raised from 25 in ADR-002, where 25 was measured to veto 36.9% of fan-worthy time because cooking drives co2 and particulate together.
- **TEMPERATURE_UNIT** — Environment variable that flips the display unit for temperature. Accepts `C` (default), `F`, or `K`. Read by `awair.units.get_temperature_unit`; storage in the `readings` table is always Celsius.

## Related decisions

Load-bearing terminology choices go in `docs/decisions/` as ADRs. Link them
here when a term is contested or has a non-obvious rationale.

- [ADR-001](docs/decisions/001-retire-automatic-fan-mitigation.md) — retire
  automatic **fan mitigation** (#61). Defines **disabled in code** and
  **release**. Superseded in part by ADR-002.
- [ADR-002](docs/decisions/002-co2-only-fan-mitigation.md) — un-retire **fan
  mitigation** on an absolute co2 trigger. Defines **hysteresis band**, **run**
  and **capped**; retires **engaged**.
