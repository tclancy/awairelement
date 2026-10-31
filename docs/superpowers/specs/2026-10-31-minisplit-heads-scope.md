# Both mini split heads and their setpoints: scope

*Written 2026-10-31. Tom thinks the bedroom mini split "may be way oversized
for heating the space" and is "driving the space at something like 5 degrees
higher than it needs to". He'd rather try the cheap route before putting a
sensor back in the bedroom: record what the heads and the climate bridge
already report. That means both heads, with their setpoints, and "anything
even possibly useful down the road". This is a scope document and a simple
plan, not a design. It spans two repos. Temperatures are stored in °C;
timestamps are UTC. An independent agent reviewed it against parsons-pulse
`origin/main` (824bf6c), this repo and the live bridge; its findings are
folded in.*

## Facts this rests on

| Fact | Source |
|---|---|
| The bridge's loopback `GET /status` (`127.0.0.1:8100`) serves dining's zone at the top level and the bedroom zone under `bedroom`. **`bedroom` is null when that zone is configured off**, meaning `CLIMATE_BEDROOM_DEVICE_ID` is empty. That's not the same as the head being switched off (ADR-025). | parsons-pulse `server.py` `status_json` |
| Each zone block carries `target {value, source}`, `hold`, `last_sent`, `last_decision {at, reason}`, `next_boundary`, and a `head` with `power, mode, setpoint, temperature, temperature_at, watts, watts_from, watts_at, delta_wh`. Dining's block also has `override`, `awair`, `ecobee`, `room_source`, `estimate` and `blind`. | live `/status`, 2026-10-31 |
| **`head` and `last_decision` are the last completed cycle** (`_last`). **`target` and `hold` are computed at request time** (`effective_target(now, …)` in `status()`). So a row keyed on `last_decision.at` can pick up a target up to one cycle later, and that matters at schedule boundaries. | `bridge.py` / `zone.py` `status()` |
| At 14:43Z the bedroom target was 55 °F, the bridge had sent 55 °F, and the head read 57 °F. Dining's target was 68 °F, the bridge had sent 64 °F, the head read 64 °F and the Ecobee read 67.6 °F. | live `/status` |
| `awair/minisplit.py` stores dining only: `temp, mode, switch` plus the draw fields. `minisplit_readings` is keyed on `ts` alone, and `insert_minisplit_reading` is `ON CONFLICT(ts) DO NOTHING`. `minisplit_readings_since` has no head filter. **`db.py` explicitly refuses a `setpoint` column** because of the °F-beside-°C trap. Tom has now asked for it, so this scope reverses that comment rather than ignoring it. | `awair/db.py` |
| The bridge fetches each head's **full** SmartThings status every cycle (`Head._raw_status`), then `parse_state` keeps nine fields. `Head.command()` fetches it a **second** time to range-check the setpoint. | parsons-pulse `devices.py` |
| `parse_state` gates the head's temperature on unit `F`, but **not the setpoint**. `minisplit.py`'s comment that the bridge enforces °F on the setpoint is wrong. | `devices._fahrenheit` docstring |
| `_head_json` builds both the `/status` head and the `climate.cycle` / `cycle_skipped` journal lines. `HeadState` is a frozen dataclass whose copies reach the state file through `Hold.observed`. | `zone.py:150,170`; `state.py` |
| SmartThings has a humidity capability (`relativeHumidityMeasurement`), plus fan speed, swing and optional mode. The only evidence that our heads report humidity is one illustrative line in homelab's docs. **Unverified per head.** Nobody has seen a full raw dump. | homelab `docs/minisplits.md:91` |
| The bridge holds the house's only SmartThings grant, and its refresh token is single-use (ADR-022). Anything from SmartThings comes out through the bridge. | ADR-022 |
| The head's `temperature` is measured at the head. SmartThings restamps `temperature_at` only when the value **changes**, so on a head that's on, an old stamp doesn't mean a stale reading. Live dining showed `power: on` with `temperature_at` 39 minutes before the cycle. #194 changed only the bedroom's *decision* (`control.head_measure`). `/status` still publishes an off head's last temperature. | live `/status`; parsons-pulse #194 |

## Goals

1. **Record both heads** in `minisplit_readings`, distinguished by a `head`
   column (`dining`, `bedroom`). Same table, same poller, same unit (Tom,
   2026-10-31).
2. **Record each head's setpoint and the target for that cycle**, in °C, along
   with the decision reason and `temperature_at`.
3. **Keep everything else.** The bridge publishes each head's raw SmartThings
   status, and the poller stores the zone block as JSON. Humidity, fan and
   WindFree live there and can be pulled out of history later.
4. **Draw the bedroom** on its own card in the empty eighth grid slot, after
   Precipitation: head temperature, setpoint and target on one °F axis.

## Non-goals

- **Head humidity as a column or a line.** The Awair stays the house's single
  humidity source (Tom, 2026-10-31). The heads' humidity is kept only inside
  `raw`.
- **A bedroom room sensor.** That comes later, and only if the head's numbers
  can't settle the question.
- **Deciding the unit is oversized.** This records the evidence and doesn't
  draw conclusions.
- **Any change to how the bridge steers.** It sends no new commands, and
  ADR-023 stands.
- **Serving `raw` anywhere.** awairelement's web surface is unauthenticated
  (ADR-019), so `raw` stays in the database and never reaches `/api/*`.
- **Outdoor-unit internals.** The cloud doesn't expose them.

## Decisions the design must make

| Question | Lean |
|---|---|
| **Primary key.** The dedup must work **per head**: each head re-publishes its own cycle until the next one. | **`PRIMARY KEY (head, ts)`**, with `head TEXT NOT NULL` and `ts TEXT NOT NULL` declared explicitly, because a composite non-INTEGER key has #98's missing-NOT-NULL hole. The insert becomes `ON CONFLICT(head, ts) DO NOTHING`. |
| **Migration.** | A guarded rebuild in `_migrate()`, run when the `head` column is missing. It copies the **old** columns plus a literal `'dining'`, unlike `_rebuild_outdoor_readings`, which copies the new list. It has to respect `_migrate`'s "must stay last" ordering. The test is that a migrated table matches a fresh one in column order. **Rehearsed on a copy of the live DB** before deploy. |
| **Where the bridge keeps `raw`.** Caching it on `Head` would let it disagree with the published head, because `command()` fetches again and a skipped cycle leaves `_last` old. | `read()` returns `(HeadState, raw)`, and `raw` is stored in `_LastCycle` beside `head`. It's added **only in `status()`**, never in `_head_json` (or the journal gets the whole status every cycle) and never in `HeadState` (or the state file and hashing). A skipped cycle keeps the previous cycle's raw. |
| **Target skew.** | The bridge also publishes the cycle's own target as `last_decision.target`, and the poller stores that, not the request-time `target`. |
| **Units.** | `setpoint` and `target` are converted to °C at ingest. The setpoint's conversion is gated on `coolingSetpoint.unit` from `raw`: no unit or a different unit means null. Rewrite `db.py`'s no-setpoint comment and `minisplit.py`'s °F claim. |
| **What `raw` holds.** | The zone block as served, **with dining's `bedroom` key removed**, and the head's SmartThings status nested at `head.raw`. A body with no `head.raw` (a bridge rollback) is still accepted, and `raw` is stored as whatever came. |
| **Raw size. Not yet measured.** A full Samsung status could be tens of KB: at 20 KB, two heads every 5 minutes is about 4 GB a year. | **Measure in step 2.** Under about 5 KB per head, store every row. Larger than that, store `raw` only when it differs from the head's previous raw after dropping per-attribute `timestamp` fields, and null otherwise. |
| **What `raw` exposes.** `_redact` covers error text, not 2xx bodies. A Samsung status can carry a serial number and MAC (`samsungce.deviceIdentification`, `ocf`). | parsons-pulse tests pin which components and capabilities are published, and that no `Authorization`, token value or `access_token` key appears. The device identifiers are fine on loopback, and awairelement never serves them. |
| **Poll shape.** `poll_once` returns one status, and nine test assertions depend on that string. | One fetch, then **per head**: parse, insert and status, each in its own guard, each logged. `poll_once` returns `{head: status}`. A missing bedroom is `unkeyed`. |
| **Dining unchanged on the page.** | `minisplit_readings_since` gains a `head` argument, and `/api/series` passes `'dining'`. |
| **Stale head temperature on the card.** | Draw the head's temperature only while `switch = 'on'`, or within 30 minutes of the head switching off. |

## Plan

Each step is its own PR, tests first.

1. **This scope.** Docs only.
2. **parsons-pulse: publish `head.raw` and `last_decision.target`**, shaped as
   the decisions above describe. Tests cover the round trip, the skipped
   cycle, journal lines unchanged, the state file unchanged, and the exposure
   pin. Deploy, then **measure each head's raw size and confirm
   `relativeHumidityMeasurement` exists on both**. Settle the raw-size
   decision.
3. **awairelement: record.** The migration (rehearsed on a live-DB copy), the
   new columns (`head`, `setpoint`, `target`, `target_source`,
   `decision_reason`, `temperature_at`, `raw`), per-head polling, and a
   head-filtered range read. Deploy, then confirm bedroom rows arrive.
4. **awairelement: draw.** The Bedroom card in the eighth slot. Checked in a
   browser at the desktop and phone widths in `docs/screenshots/`.

## Done means

- `SELECT head, count(*) FROM minisplit_readings GROUP BY head` shows both
  heads growing about 12 rows an hour while the bedroom zone is configured,
  and every row from before the migration is `dining`.
- New rows have `setpoint` and `target` in °C (null only with a logged
  reason). `raw` is valid JSON with `head.raw.components` on every row the
  raw-size decision says to keep it on, and it never contains a `bedroom` key.
- The bridge's journal lines are the same length as before step 2.
- The temp card's dining data is unchanged. The Bedroom card shows the head's
  temperature against setpoint and target on today, 7 and 30 days.
  Screenshots committed.
