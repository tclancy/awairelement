# Both mini split heads, setpoints and humidity: scope

*Written 2026-10-31. Tom thinks the bedroom mini split "may be way oversized
for heating the space" and is "driving the space at something like 5 degrees
higher than it needs to". He'd rather try the cheap route before putting a
sensor back in the bedroom: record what the heads and the climate bridge
already report. That means both heads, with their setpoints, and "anything
even possibly useful down the road", humidity especially, because the wood
stove is about to dry the house out. This is a scope document and a simple
plan, not a design. It spans two repos. Temperatures are stored in °C;
timestamps are UTC.*

## Facts this rests on

| Fact | Source |
|---|---|
| The bridge's loopback `GET /status` (`127.0.0.1:8100`) serves dining's zone at the top level and the bedroom zone under `bedroom`. **`bedroom` is null when that zone is `off`** (ADR-025). | parsons-pulse `climatesidecar/server.py` `status_json` |
| Each zone block carries `target {value, source}`, `hold`, `last_sent`, `last_decision {at, reason}`, `next_boundary`, and a `head` with `power, mode, setpoint, temperature, temperature_at, watts, watts_from, watts_at, delta_wh`. Dining's block also has `override`, `awair`, `ecobee`, `room_source`, `estimate` and `blind`. | live `/status`, 2026-10-31 14:43Z |
| At that moment the bedroom target was 55 °F, the bridge had sent 55 °F, and **the head read 57 °F**. Dining's target was 68 °F, the bridge had sent 64 °F, the head read 64 °F and the Ecobee read 67.6 °F. | same |
| `awair/minisplit.py` stores dining only, and only `temp, mode, switch` plus the draw fields. It drops `setpoint`, `target`, the decision, `temperature_at` and the whole `bedroom` block. `minisplit_readings` is keyed on `ts` alone. | `awair/minisplit.py`, `awair/db.py` |
| The bridge fetches each head's **full** SmartThings status every cycle (`Head._raw_status`), then `parse_state` keeps nine fields and throws away the rest. | parsons-pulse `climatesidecar/devices.py` |
| The discarded attributes include **relative humidity** (`relativeHumidityMeasurement`), fan speed, louvre swing and optional mode (WindFree). Both heads have them: homelab's `smartthings.py status` prints `rh=51% fan=auto swing=fixed`. The power report also has a cumulative `energy` counter. **Nobody has looked at a full raw dump**, so the rest of the list is unknown. | homelab `docs/minisplits.md`, `scripts/smartthings.py` |
| The bridge holds the house's only SmartThings grant, and its refresh token is single-use, so a second copy breaks the first (ADR-022). Anything we want from SmartThings has to come out through the bridge. | parsons-pulse ADR-022 |
| The head's `temperature` is measured at the head, not in the room. The bridge steers dining by the Awair and Ecobee, not by the head. The Awair is in a different heating zone from the bedroom, above it and behind a door, so it says nothing about the bedroom. | `devices.HeadState` docstring; Tom |
| The bedroom head has served a stale temperature before; parsons-pulse #194 fixed that. `temperature_at` is how a reader tells a cached reading from a fresh one. | parsons-pulse #194 |

## Goals

1. **Record both heads** in `minisplit_readings`, distinguished by a `head`
   column (`dining`, `bedroom`). Same table, same poller, same unit (Tom,
   2026-10-31).
2. **Record the setpoint and the bridge's target** for each head, along with
   the decision reason and `temperature_at`.
3. **Keep everything else.** The bridge publishes each head's raw SmartThings
   status, and the poller stores the whole zone block as JSON. Nobody has to
   pick fields now, and anything can be pulled out of history later.
4. **Typed humidity column** for each head, parsed from the raw status, ready
   for the winter.
5. **Draw the bedroom** (head temperature against setpoint and target) so the
   oversizing question can be looked at.

## Non-goals

- **A bedroom room sensor.** That comes later, and only if the head's own
  numbers can't settle the question.
- **Deciding the unit is oversized.** This records the evidence and doesn't
  draw conclusions.
- **Any change to how the bridge steers.** It stays read-only: no
  `Follow Me`/`I Feel` setting and no new commands. The bridge still only ever
  sends power on, heat and a setpoint (ADR-023).
- **Outdoor-unit internals** (compressor speed, coil temperatures). The cloud
  doesn't expose them.
- **Alerts on humidity.**

## Decisions the design must make

| Question | Lean |
|---|---|
| **Primary key.** `ts` alone collides once two heads share the table, and their cycles run independently, so their clocks differ. SQLite can't alter a primary key. | Rebuild the table with **`PRIMARY KEY (head, ts)`** through a guarded migration in `_migrate()`, copying existing rows with `head = 'dining'`. It follows the existing rebuild precedent (`_migrate_outdoor_ts_not_null`). |
| **Bedroom off.** `bedroom: null` | No bedroom row, and the status is `unkeyed` for that head, not `error`. Dining's row still stores. One `/status` fetch gives two independent parses. |
| **What "raw" means.** | The **zone block as served**, with the head's raw SmartThings status nested inside it (`head.raw`, new in parsons-pulse). One JSON column, `raw`. That keeps `hold`, `override`, the room inputs and every SmartThings attribute. |
| **Raw size. Not yet measured.** A full Samsung AC status can be tens of KB: at 20 KB, two heads every 5 minutes is about 11 MB/day, about 4 GB/year. | **Measure first** (step 1 publishes it; take `len` of a live body). Under about 5 KB per head, store every row. Larger than that, store the raw block **only when it differs from the head's previous raw** after dropping the per-attribute `timestamp` fields, and null otherwise. Decided on the measured number, not this guess. |
| **Does `raw` leak a credential?** The status body is device state, but that's an assumption. | parsons-pulse adds a test that the published `raw` contains no `Authorization`, no token value and no `access_token` key. Its existing `_redact` discipline applies. |
| **Humidity unit and validation.** | `%`, `_finite`, 0–100 or null. Read from `relativeHumidityMeasurement.humidity` in the raw status **in awairelement**, so the bridge doesn't grow a field it never steers by. |
| **Where the bedroom draws.** The temp card already carries outdoor and basement (4 legend rows). | **A new "Bedroom" card:** head temperature, setpoint and target on one °F axis, and nothing else. Tom to confirm. |
| **Head humidity on the humidity card?** It's cheap now that overlays are a list. | **Tom to confirm.** It's in step 4 if yes. |

## Plan

Each step is its own PR, tests first.

1. **This scope.** Docs only.
2. **parsons-pulse: publish the raw status.** `Head.read()` keeps the status
   body it already fetched, and each zone's `status()["head"]` gains `raw`.
   Tests: `raw` round-trips, a failed read publishes `raw: null`, and the
   no-credential test. Deploy, then **measure the body size** and settle the
   raw-size decision above.
3. **awairelement: record.** Migration to `(head, ts)` with the new columns
   `head`, `setpoint`, `target`, `target_source`, `decision_reason`,
   `temperature_at`, `humidity` and `raw`. The parser becomes per-zone, and
   one poll writes up to two rows. Tests cover the migration (existing rows
   become `dining`, and a fresh table matches a migrated one in column order),
   bedroom null, both heads' keys, and humidity bounds. `/api/series` keeps
   serving `minisplit_*` for dining alone so nothing on the page changes.
   Deploy, then confirm bedroom rows arrive every 5 minutes.
4. **awairelement: draw.** The Bedroom card (and head humidity, if
   confirmed). Checked in a browser at the desktop and phone widths in
   `docs/screenshots/`.

## Done means

- `SELECT head, count(*) FROM minisplit_readings GROUP BY head` shows both
  heads growing about 12 rows an hour while the bedroom zone is on, and the
  rows from before the migration are all `dining`.
- Each new row has `setpoint`, `target` and `humidity` filled in (or null with
  a logged reason), and a `raw` that parses as JSON and contains the head's
  SmartThings `components`.
- The Bedroom card shows the head's temperature against its setpoint on
  today, 7 and 30 days. Screenshots committed.
