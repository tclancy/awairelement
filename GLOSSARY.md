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
- **decide** — In `awair.fans`, the rate-limit + no-op filter around a **desired action**. Returns a `MitigationDecision` or `None`.
- **desired action** — The verdict `awair.fans.desired_action` derives from open **event**s + latest pm25: `"off" | "speed1" | "speed2" | "speed3"`.
- **DeviceHealth** — Snapshot of last-successful-fetch state used to detect the transition between healthy and stale/unreachable readings; owns the `ok`, `since`, and `last_status` fields on `awair.monitor.DeviceHealth`.
- **event** — A row in the `events` table representing an open or closed spike/threshold violation. Rows are opened by `spikes.evaluate` and closed by `db.close_event`.
- **fan mitigation** — The whole loop: `desired_action` → `decide` → `actuate`. Turns ceiling fans on when co2/voc spike and off when air clears; gated by `AWAIR_FAN_MITIGATION_ENABLED` (default off).
- **fan_state** — SQLite row (one per fan) tracking `last_action` (last known / last confirmed physical state) and `last_command_at` (when the poller last tried to command the fan — used for the 1-cmd/min rate limit).
- **FansConfig** — Immutable config for fan mitigation: `enabled`, `fan_host`, `fan_ids`. Built from env by `awair.fans.config_from_env`.
- **fetch** — The single-shot HTTP GET against the Awair Element Local API that returns one reading payload; built by `poller.make_fetch(url)`.
- **metric** — A named channel on a reading (`co2`, `voc`, `pm25`, `temp`, `humid`, etc.); the `MetricConfig` dataclass in `awair.spikes` binds a metric to its thresholds.
- **MetricConfig** — Per-metric threshold + hysteresis config used by `spikes.evaluate` to decide whether to open, close, or renotify an event.
- **MitigationDecision** — Immutable dataclass emitted by `awair.fans.decide`: `fan_id`, target `action`, and human-readable `reason`. Consumed by `actuate`.
- **notifier** — The `awair.alerts.Notifier` object that fans an event out to ntfy; injected into `poller.handle_device_health` and `monitor.check_metrics`.
- **poll** — One iteration of the poller loop: `fetch` → `parse_reading` → `insert_reading` → `check_metrics` → `check_fans`. Distinct from **fetch** — a poll wraps a fetch with DB + monitor side effects.
- **reading** — One row in the `readings` table; produced by `poller.parse_reading(payload, received_at)`.
- **series** — A bucketed time-window of readings for the dashboard, produced by `awair.series.bucket(points, bucket_seconds)`. **Not** a synonym for `metric_history` (which returns raw points).
- **spike** — An event triggered by threshold + hysteresis logic in `awair.spikes`; distinct from a **stale device**, which is the health-check equivalent handled by `monitor` + `DeviceHealth`.
- **suppressor** — A metric that *blocks* fan mitigation rather than triggering it. PM2.5 is the current sole suppressor (fans re-suspend particulate); an elevated pm25 forces fans off regardless of co2/voc.

## Related decisions

Load-bearing terminology choices go in `docs/decisions/` as ADRs. Link them
here when a term is contested or has a non-obvious rationale. (None yet.)
