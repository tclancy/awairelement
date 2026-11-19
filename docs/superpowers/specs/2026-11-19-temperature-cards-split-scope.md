# Splitting the temperature card, and a unified humidity card: scope

*Written 2026-11-19 for #156. Tom: "The single Temperature graph is barely
readable after I added everything to it." He asks for three things — the temp
card reduced to one temperature per place, a per-room card like the Bedroom one
for the main room, and a unified humidity graph fed by the basement sensor and
the mini splits as well as the Awair. This is a scope document and a simple
plan, not a design. Temperatures are stored in °C and converted at the API
boundary; timestamps are UTC.*

## Facts this rests on

| Fact | Source |
|---|---|
| The temp card carries **five** lines today: the Awair's bucket average plus four overlays — `outdoor-temp`, `basement-temp`, `minisplit-temp` (the **dining** head) and `minisplit-watts` (the dining head's metered draw, on its own right-hand axis). Eight legend entries, four reserved rows, five below 380 px. | `web.METRIC_OVERLAYS`, `dashboard.js` `TEMP_OVERLAYS`, `dashboard.html` |
| The Bedroom card is drawn by `makeBedroomPlot` from `/api/bedroom-series`: head temperature, setpoint, cycle target on the degree axis, metered draw on a right-hand watts axis. It is hand-written markup (`data-minisplit="bedroom"`), like the precipitation card. | `dashboard.js`, `dashboard.html` |
| `/api/series` already publishes everything the main room's card needs: `metrics.temp` (the Awair, bucketed), `minisplit_temp` and `minisplit_watts` (dining, carried onto `metrics.temp.t`). | `web.series` |
| The bedroom head's temperature needs the off-head grace rule — `/status` keeps publishing an off head's last reading, so past 30 minutes it is a stale number drawn as a live one (`web.BEDROOM_TEMP_GRACE_SECONDS`). Dining's trace has never needed it; that head runs. | `web._bedroom_temp_shown` |
| The basement sensor is an Apollo ESPHome kit with an **AHT-family temperature/humidity module**. The poller reads one entity, `GET /sensor/Temperature`. Humidity was an explicit non-goal in the basement scope ("a second GET with its own failures and clock, and nobody asked to see it"). **The humidity entity's name is unverified** — the path is keyed on the entity, so a rename is a 404. | `docs/superpowers/specs/2026-10-28-basement-temperature-scope.md`, `awair/basement.py` |
| SmartThings has a humidity capability (`relativeHumidityMeasurement`). **Whether our heads report it is still unverified** — the heads scope recorded "the only evidence is one illustrative line in homelab's docs; nobody has seen a full raw dump", kept head humidity out as a column and a line, and left it inside `minisplit_readings.raw`. | `docs/superpowers/specs/2026-10-31-minisplit-heads-scope.md` |
| The bridge publishes each head's full raw status every cycle, and the poller already reaches into it for the setpoint's unit (`_setpoint_celsius`). `raw` is *stored* only when it changes, but it is *parsed* every cycle. | `awair/minisplit.py` |
| `basement_readings.temp` is `NOT NULL`; `minisplit_readings` is all-nullable past its key. Both inserts bind every published column by name, so a new column is a new key in every seeding helper. `_rebuild_minisplit_readings` refuses a column it does not know, so an ALTER on that table must run **after** the heads rebuild. | `awair/db.py` |

## Requirements, one per line, with the check for each

Tom's prose is three bullets; this is what it says, and nobody is awake to
confirm it, so each line carries the check that would fail if it were not done.

1. The temp card draws exactly four lines: the Awair's indoor temperature, the
   outdoor temperature, the basement sensor, and the **bedroom** head.
   *Check:* `web.METRIC_OVERLAYS["temp"] == ("outdoor-temp", "basement-temp",
   "bedroom-temp")`, and the JS overlay list for `temp` matches it key for key.
2. The dining head's temperature and its metered draw are no longer on the temp
   card. *Check:* neither `minisplit-temp` nor `minisplit-watts` appears in
   `METRIC_OVERLAYS` or in the JS overlay lists; no `watts` scale is declared on
   a metric card.
3. A Great room card, built like the Bedroom one, shows the great room's
   temperature from the average we already have, the dining head's own
   temperature, and the dining head's draw.
   *Check:* `/` renders `data-room="great-room"`; the card's three traces resolve
   to `metrics.temp.avg`, `minisplit_temp` and `minisplit_watts`; a browser shot.
4. The bedroom head's temperature on the temp card obeys the same off-head grace
   rule the Bedroom card uses. *Check:* a bedroom row switched off more than 30
   minutes after its last on reading contributes no `bedroom_temp`.
5. The basement sensor's humidity is recorded. *Check:* `basement_readings.humid`
   exists; a poll against a stubbed `/sensor/Humidity` stores it; a failing or
   renamed humidity entity still stores the temperature.
6. Each mini split head's humidity is recorded when the head reports it.
   *Check:* `minisplit.parse_reading` fills `humid` from
   `relativeHumidityMeasurement` and leaves it NULL when the capability is
   absent or carries another unit.
7. One humidity card carries the Awair's humidity, the basement sensor's and
   both heads'. *Check:* `METRIC_OVERLAYS["humid"] == ("basement-humid",
   "minisplit-humid", "bedroom-humid")`; each trace is published on the humid
   card's own grid and is invariant under `TEMPERATURE_UNIT`.

## Decisions

- **"The bedroom mini split temp" on the temp card means the bedroom head, and
  the dining head's line moves off it.** Read as one line per *place*: the
  Awair's room, outdoors, the basement, the bedroom. The dining head reads the
  same room as the Awair, so comparing the two is the Great room card's job,
  which is exactly what requirement 3 asks for.
- **The Great room card is drawn from `/api/series`, not from a new endpoint.**
  All three of its traces are already on that payload, on one grid, converted
  once. A `/api/great-room-series` would re-run the heaviest query on the page
  (30 days of 30-second readings) to publish numbers the browser already holds,
  and would give the indoor temperature a second conversion path.
- **One room-card factory, not two.** `makeBedroomPlot` becomes `makeRoomPlot`,
  configured by `ROOM_CARDS`; the attribute becomes `data-room` because the base
  line of the great room's card is the Awair, not a mini split.
- **The `scale` machinery comes off the metric-card overlay loop.** With the
  draw gone from the temp card, every metric-card overlay is its card's own
  quantity in its card's own unit, and the admission rule collapses back to
  that one sentence. Two-unit cards each declare their second axis in their own
  factory (precipitation's pressure, the room cards' draw), which is where the
  guards move with it.
- **The bedroom head gets a colour of its own** (`--series-bedroom-head`), used
  on all three cards it appears on. Without it, green would mean the bedroom
  head on the temp card and the dining head one card away on the humidity card.
- **Head humidity ships as a line even though the capability is unverified.** A
  head that reports nothing renders as a break, which is already how this
  dashboard draws a dead source. The alternative — record the column, hold the
  line back — answers Tom's question with a database query rather than with the
  graph he asked for, and the line costs nothing if the data never arrives.
- **No backfill of head humidity out of stored `raw`.** `raw` is ~13.7 KB a row
  and NULL means "same as the last stored one", so a backfill is a carry-forward
  over a 30-day scan of blobs. The card fills in from the next poll onward.
- **Outdoor humidity stays off the humidity card.** `outdoor_readings.humid` is
  recorded and would be the obvious fifth line, and nobody asked for it. The
  card is the three sources #156 names.

## Non-goals

- Alerting on humidity, in either direction.
- A `humid` entry in `spikes.METRICS` (so the card gets no ceiling line).
- Publishing basement or head humidity on `/api/latest` or any hub-facing
  endpoint. Nothing consumes it yet.
- Re-measuring the response-time table in the README. `/api/series` gains one
  query against a small table; the published numbers were measured on the box
  and are not re-derived here.
- The basement sensor's own health escalation, which it still does not have.

## Plan

1. **Storage.** `basement_readings.humid`, `minisplit_readings.humid`, their
   migrations, and a `columns` argument on `db.basement_readings_since`.
2. **Record.** The basement poller's second GET (derived from the configured
   temperature URL, overridable by `AWAIR_BASEMENT_HUMIDITY_URL`) and the mini
   split poller's `relativeHumidityMeasurement` read.
3. **Publish.** `/api/series` gains `bedroom_temp`, `basement_humid`,
   `minisplit_humid`, `bedroom_humid`; `METRIC_OVERLAYS` gains the humid card
   and loses the dining head and the draw from the temp card.
4. **Draw.** The room-card factory, the Great room card, the humidity overlays,
   the legend reservations, the new palette pair, the footer.
5. **Check.** The suite, the linters, the radon gate, and the dashboard in a
   browser at 1440 and 390 against a seeded database.
