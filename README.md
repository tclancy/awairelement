# awairelement

Continuous local logging, spike alerting, and a small trend dashboard for an
[Awair Element](https://uk.getawair.com/products/element) air quality monitor.

Two long-running processes, plus three optional feeds:

- **Poller** — hits the Awair Element's Local API every 30 seconds, stores every
  reading in SQLite, runs spike detection, and fires [ntfy](https://ntfy.sh)
  notifications when CO2 / TVOC / PM2.5 climb above their baseline or a hard
  ceiling.
- **Dashboard** — a small Flask app that renders today / 7d / 30d
  small-multiple charts of every metric, with detected spikes overlaid. It
  opens on **today** (`web.DEFAULT_RANGE`).

Each feed is its own process writing into the same SQLite file, so one source
going down cannot wedge the others:

- **Outdoor** (`awair.outdoor`) — Open-Meteo weather and air quality every
  15 minutes, plus NWS alerts for the parcel. Needs `AWAIR_LAT` / `AWAIR_LON`.
- **Mini split** (`awair.minisplit`) — both Samsung heads, dining and bedroom,
  read from the parsons-pulse climate bridge's loopback `/status` (#139; the
  heads scope, `docs/superpowers/specs/2026-10-31-minisplit-heads-scope.md`).
  Each row is one head's bridge *cycle*, keyed on `(head, ts)` with `ts` the
  cycle's own clock rather than our poll clock. It carries the head's
  temperature, metered draw, setpoint and the cycle's target (both in °C), its
  own relative humidity where the head reports one (#156), and the head's raw
  SmartThings status as JSON, stored only when a value in it
  changes. Loopback-only, so it runs on the bridge's box or not at all. Since
  #156 **both heads are charted, each on the card for the room it serves**: the
  dining head's temperature and metered draw on the *Great room* card beside
  the Awair's own average, the bedroom head's on the *Bedroom* card beside its
  setpoint and target. The bedroom head's temperature is also the fourth line
  on the temp card, and both heads' humidity rides the humidity card. Every
  one of those traces is held for two report windows, by one bound.

  Five things to expect on a box today. **The draw and the temperatures are not
  comparable by height.** They share a card and not an axis: the temperatures
  are drawn to the card's degree ruler, and the draw to a zero-anchored watts
  axis on the right, the way the precipitation card already carries pressure on
  an axis of its own (#42) — though pressure's is a *fixed* 28.5-31.0 inHg
  rather than anchored at zero, because a barometer has no meaningful zero and
  a power draw does. `docs/reading-the-dashboard.md` says which axis the draw is read against,
  because nothing on the chart does. **A head's humidity may never arrive.**
  Whether these units carry SmartThings' `relativeHumidityMeasurement` has
  never been confirmed — the heads scope found one illustrative line in
  homelab's docs and no raw dump — so the column may stay NULL and the line
  may stay absent, which on that card is a reading of sorts rather than an
  outage. **A bedroom zone configured off logs `bedroom
  unkeyed` every poll**, which is the bridge publishing `bedroom: null`, not a
  fault. **The draw
  columns were NULL until parsons-pulse #182 deployed**; on the homelab they
  began filling 2026-10-30 11:54 UTC, and a box running an older bridge still
  stores rows carrying the head's temperature and nothing else, which is
  working rather than broken — such a row holds its temperature onto the grid
  and contributes no draw. With no draw to show, the right-hand axis is not
  drawn either: it is created only for a trace that has a reading in it, so a
  pre-#182 box gets the card it had before this feed rather than an empty
  ruler down its right edge. And **this feed has no health escalation**: unlike
  the outdoor poller it opens no alert event and sends no ntfy, so a bridge
  that stops answering shows up only as `error` lines in `journalctl --user -u
  awairelement-minisplit`. Deliberate for a first cut, and the reason the log
  line is worth watching after a deploy.
- **Basement** (`awair.basement`) — the basement ESPHome sensor's temperature
  and humidity every 60 s, from `AWAIR_BASEMENT_URL` (default
  `http://192.168.68.50/sensor/Temperature`) and the `Humidity` entity beside
  it. The payload has **no clock**, so
  rows are keyed on our poll clock truncated to the minute: one row per
  poll-minute. An unavailable sensor (null/NaN `value`) stores nothing, so a
  gap in the table is a gap on the chart. Drawn on the temp card as a
  "Basement" line on the same axis, averaged per chart bucket and held for two
  buckets — and since #156 on the humidity card the same way.
  Like the mini split feed it has no
  health escalation; watch `journalctl --user -u awairelement-basement`.

  **The humidity is a second GET and is subordinate to the first** (#156). The
  module is an AHT-family temperature/humidity part, so the reading is there,
  but the entity's *name* is a guess derived from the temperature URL and the
  path is keyed on the entity, so a wrong guess is a 404. Nothing about that
  fetch can cost the temperature: it happens only once the temperature has
  parsed, every failure it can have stores NULL and logs, and the row is
  written either way. If the entity turns out to be called something else,
  `AWAIR_BASEMENT_HUMIDITY_URL` is the fix and no release is needed. Rows
  written before this shipped have a NULL humidity and nothing backfills
  them — the sensor publishes a current value and no history.

The temp card also carries one derived verdict, the **basement watch**
(parsons-pulse #205): a yellow badge under the card's header when the basement
is above 62 °F during the hours Tom says the room is unoccupied — weekdays
23:00–17:00, weekends 00:00–07:00 — *and* the highest outdoor temperature in
the preceding 24 hours was below 62 °F too. A basement that is warm while the
weather cannot account for it is warm for some reason inside the house. The
watch reports and never instructs: there is no ntfy, no alert event and
nothing that acts on it. It holds until a reading comes back to 62 °F or
below, so the basement temperature it shows is *now* and the outdoor high is
*then* — up to 42 hours apart, which is why the badge says which is which.

A trailing 24 hours rather than the calendar day, decided on the record rather
than on taste — the two bases disagree about exactly one episode in the
basement feed's history and the calendar day is wrong about it
([ADR-003](docs/decisions/003-basement-watch-attributes-warmth-to-a-trailing-window.md)).
Two things to expect on a box today. **The watch has never fired on real
data** — 0 of 898 candidate readings through 2026-11-14 — so its first
appearance is worth going and looking at rather than trusting. And an
outdoor-poller outage shows as **no badge**, never as a yellow: a maximum over
a window with the afternoon missing is the night's high, so an uncovered
attribution window is no verdict at all — published as `unknown`, which is a
different key from `clear` even though both render as silence. The same goes
for a basement warm for longer than the 42 hours of history the latch reads:
its trip is outside the evidence, so the watch says `unknown` rather than
claiming it cleared.

Design and rationale live in [SCOPE.md](SCOPE.md); canonical vocabulary lives
in [GLOSSARY.md](GLOSSARY.md).

## Requirements

- An **Awair Element** with the **Local API enabled** — the "Test Mode" /
  Local API toggle in the Awair app. On current firmware this toggle is gone,
  so if it was never enabled on your device you may not be able to turn it on
  now; this project can't help you re-enable it. `curl http://<device-ip>/air-data/latest`
  should return JSON.
- **Python 3.13+** and [uv](https://github.com/astral-sh/uv). Four of the five
  pollers are stdlib-only; the dashboard uses Flask + gunicorn, and the
  humidifier poller uses [`pyvesync`](https://github.com/webdjoe/pyvesync)
  (MIT), which brings `aiohttp`, `mashumaro` and `orjson` with it. Nothing
  else imports it — see [The humidifier feed](#the-humidifier-feed).
- (Optional) a **VeSync account** with a Levoit humidifier on it, if you want
  that feed. Without one the poller logs a line and exits, and `/health`
  reports the feed as *not configured* rather than failing.
- (Optional, for alerts) an **[ntfy](https://ntfy.sh) topic** — either the
  public server or your own. You'll need the topic name and, if the topic is
  protected, an access token.

## Quick start

```bash
git clone https://github.com/tclancy/awairelement.git
cd awairelement
uv sync

# Point at your device and pick a DB path.
export AWAIR_URL="http://192.168.1.42/air-data/latest"   # your Element's LAN IP
export AWAIR_DB="$HOME/data/awairelement/awair.db"

# Run the poller. It creates the DB (and parent directory) on first run.
# Rows start accumulating; spike detection is disabled until ~6h of readings
# exist (see SCOPE.md → "Spike Detection → Cold start").
uv run python -m awair.poller
```

In a second shell:

```bash
export AWAIR_DB="$HOME/data/awairelement/awair.db"
uv run gunicorn -w 2 -b 127.0.0.1:8097 'awair.web:create_app()'
# Dashboard at http://127.0.0.1:8097/
```

If you don't want gunicorn, `uv run flask --app 'awair.web:create_app()' run`
works for local poking too.

## Configuration

All configuration is via environment variables. Only `AWAIR_URL` is required
in practice — every other var has a working default, and the ntfy vars can be
omitted entirely to run without alerts.

| Variable | Default | Notes |
|----------|---------|-------|
| `AWAIR_URL` | `http://192.168.68.51/air-data/latest` | The Local API URL for your device. Change this. |
| `AWAIR_DB` | `~/data/awairelement/awair.db` | SQLite path. The parent directory is created on first poll. |
| `AWAIR_POLL_SECONDS` | `30` | Seconds between polls. The device refreshes internally every ~10s, so shorter intervals just add duplicate-timestamp skips. |
| `AWAIR_NTFY_URL` | `https://notifications.tomclancy.info` | ntfy server root. Use `https://ntfy.sh` for the public server. |
| `AWAIR_NTFY_TOPIC` | `awair` | ntfy topic name. Pick your own. |
| `AWAIR_NTFY_TOKEN` | *(unset)* | Access token if your topic is protected. Empty string = no auth header sent. |
| `AWAIR_TZ` | `UTC` | IANA zone (e.g. `America/New_York`) for the dashboard's sunrise/sunset markers. Ignored if `AWAIR_LAT` / `AWAIR_LON` are unset. |
| `AWAIR_FAN_MITIGATION_ENABLED` | `false` | Drive the ceiling fans on high CO2. See [Fan mitigation](#fan-mitigation). |
| `AWAIR_FAN_HOST` | `192.168.68.68` | The NodeMCU that relays fan commands. |
| `AWAIR_CO2_FAN_ON` | `1000` | ppm at or above which the fans come on. |
| `AWAIR_CO2_FAN_OFF` | `900` | ppm below which they go off again. The gap between the two is deliberate — see [Fan mitigation](#fan-mitigation). |
| `AWAIR_FAN_MAX_RUN_MINUTES` | `90` | Longest single fan run, regardless of CO2. |
| `AWAIR_PM25_SUPPRESS` | `100` | pm25 (µg/m³) at or above which fans are blocked outright. |
| `AWAIR_PM25_NEAR_MISS` | `50` | pm25 at or above which a watchpoint line is logged. Behaviour-neutral. |
| `AWAIR_BASEMENT_URL` | `http://192.168.68.50/sensor/Temperature` | The basement ESPHome sensor's temperature entity. |
| `AWAIR_BASEMENT_HUMIDITY_URL` | *(derived)* | The humidity entity. Defaults to `AWAIR_BASEMENT_URL` with `Temperature` swapped for `Humidity`; set it when the entity is named something else. Unset and underivable means no humidity is polled, which is the pre-#156 behaviour. |
| `AWAIR_BASEMENT_POLL_SECONDS` | `60` | Seconds between basement polls. The module's own publish interval. |
| `AWAIR_MINISPLIT_URL` | `http://127.0.0.1:8100/status` | The parsons-pulse climate bridge's status endpoint. Loopback-only, so the mini split poller has to run on the bridge's box. |
| `AWAIR_MINISPLIT_POLL_SECONDS` | `300` | Seconds between mini split polls. The bridge's own control cycle — polling faster re-reads one report window. |
| `AWAIR_VESYNC_EMAIL` | *(unset)* | The VeSync account the humidifier is on. **Both this and the password, or the poller does not run.** Never logged. |
| `AWAIR_VESYNC_PASSWORD` | *(unset)* | That account's password. Never logged — see [The humidifier feed](#the-humidifier-feed). |
| `AWAIR_HUMIDIFIER_NAME` | *(unset)* | Which humidifier, when the account has more than one. With exactly one, leave it unset. |
| `AWAIR_HUMIDIFIER_POLL_SECONDS` | `300` | Seconds between humidifier polls, and **the floor**: a smaller value is logged and raised to 300. VeSync rate-limits per account. |
| `AWAIR_MAIN_ZONE_FEED` | `awair` | Which feed speaks for the main zone on the humidity card. Any name from `/health`. |
| `AWAIR_HUMIDITY_TARGET` | `40` | Main-zone relative humidity target, in percent. |
| `AWAIR_HUMIDITY_TARGET_COLD` | `35` | The target when it is very cold out. |
| `AWAIR_HUMIDITY_COLD_OUTDOOR_C` | `-12` | Outdoor temperature (**°C**, about 10 °F) at or below which the cold target applies. |
| `AWAIR_HEALTH_STALE_POLLS` | `6` | How many polls a feed may miss in a row before `GET /health` calls it stale. Multiplied by **each feed's own** `*_POLL_SECONDS`, so one number here is four different windows. See [Is it still recording?](#is-it-still-recording) |
| `AWAIR_WEB_SLOW_MS` | `1000` | Dashboard requests at or above this many milliseconds log at `WARNING` instead of `INFO`. See [Response times](#response-times). |

To disable ntfy entirely, leave `AWAIR_NTFY_TOKEN` unset and pick a topic
nobody's listening on — the poller will still POST but no one will see the
messages. (There's no explicit off-switch; alerting failures never block
ingestion, so a wrong URL or 401 just gets logged.)

## What you get

- **Every reading, every 30 seconds**, in a single SQLite `readings` table:
  score, temp, humidity, absolute humidity, dew point, CO2 (measured and
  estimated), TVOC (measured and baseline), the two raw VOC channels
  (`voc_h2_raw`, `voc_ethanol_raw`), and PM2.5. Timestamps are UTC.
- **Spike detection** — one ntfy notification when a metric opens an event,
  one when it clears. Tier 1 is a relative spike (>6× rolling MAD above the
  24h median for 4 consecutive polls); tier 2 is a hard ceiling (CO2 > 1200,
  TVOC > 1000, PM2.5 > 35 for 2 polls). Hysteresis prevents flapping. See
  [SCOPE.md → "Spike Detection"](SCOPE.md) for the full math and tunables.
- **The humidifier's own state** (#161) — one row per 5-minute poll of the
  Levoit Superior 6000S on VeSync: its humidity and temperature, the target
  it is set to, mist level, mode, on/off, water-empty, tank-lifted and filter
  life. Optional, and absent cleanly: see [The humidifier feed](#the-humidifier-feed).
- **A dew point per place, and a humidity target** (#161) — the great room's
  humidity against 40% (35% when it is very cold out), with the humidifier's
  own reading and the dining head's beside it, the stretches the humidifier
  spent off or dry shaded, and hours below target over the last week. Plus a
  dew point for every place that records a temperature and a humidity, which
  is what lets a warm room be compared with a cool one.
- **Device health alerts** — 10 consecutive fetch failures fire an
  "unreachable" event; 10 polls with an unchanged device timestamp fire a
  "stale" event (the wedged-but-HTTP-200 failure mode after some firmware
  updates). Recovery closes the event.
- **Dashboard** — small multiples for CO2, TVOC, PM2.5, temp, humidity, and
  score over today, 7d or 30d, opening on today (#108). "Today" is since local
  midnight, not the last 24 h. Detected events overlay as shaded spans.
  LAN-only by default (no auth). Five cards beside those six: the composite
  precipitation/pressure card (#42), the two **room** cards (#156), and the
  **Main zone humidity** and **Dew point** cards (#161).
- **Each card's range peak** (#116) — the header carries two numbers: the
  current reading, and `peak N`, the highest single reading over the range on
  screen. It exists because the card previously had no way to tell you that.
  The drawn line is the per-bucket **average**, so a spike inside one bucket is
  flattened into it; and uPlot's legend is *live*, so the `low`/`high` it shows
  are the extremes of the one bucket under the cursor — on `today` that is two
  samples of a 30-second poll. #116 reported the consequence: a TVOC line
  topping out near 35,000 beside a legend reading `high: 8,410`. Both numbers
  were right and neither was the range's extreme, and the 60-second bucket that
  set the visible peak was a few pixels wide, so hovering could not get it
  either. The peak is read off the bucket **maxima**, not the averages, because
  that is invariant under bucket size — pressing "7 days" re-buckets the same
  readings and must not move a number about the readings. It is therefore the
  top of the shaded band, which sits at or above the top of the averaged line.
  On five of the six cards that is also where the Y axis was scaled to; the
  temp card is the exception, because it shares its axis with the outdoor trace
  (#109) and in summer the axis top is the outdoor line. The composite
  precipitation card has no peak — its header already carries two numbers of
  two different quantities, and a third wrapped it onto a second line.
- **One temperature per place, and a card per room** (#156) — Tom's report was
  that the temp card had become "barely readable" once everything was on it:
  five lines, two quantities, eight legend entries. It is four lines now and
  one quantity, one per *place* — the Awair's own room, outdoors, the basement
  sensor, the bedroom head — because that is the comparison a single shared
  axis is for. What came off it is the dining head's temperature and its
  metered draw, and they did not go away: they are the **Great room** card,
  which draws the Awair's bucket average as its room line beside the head that
  serves that room and that head's draw on a right-hand watts axis. Its sibling
  is the existing **Bedroom** card. The pair is drawn by one factory from a
  `ROOM_CARDS` entry each, and the Great room card reads `/api/series` rather
  than an endpoint of its own: all three of its traces are already on that
  payload, on one grid, converted once. Screenshots in `docs/screenshots/`
  (`156-*`), taken against a seeded database at 1440 and 390, light and dark.
- **One humidity chart** (#156) — the same idea for the other quantity, and it
  needed two new feeds rather than a rearrangement: the basement sensor's
  humidity (a second GET against the AHT module's other entity) and each mini
  split head's own (out of the raw SmartThings status the bridge already
  publishes). The Awair's reading is the card's own line and the other three
  ride it, all in percent on one axis. Two caveats, both real: **the heads may
  report no humidity at all** — the capability is unverified on these units, so
  a missing line there means "no such reading" as often as it means an
  outage — and **nothing is backfilled**, so both new traces start at the
  deploy rather than at the left edge of a 30-day range.
- **One temperature chart, indoors and out** (#109) — the outdoor trace is
  drawn on the indoor temp card rather than on a card of its own, **sharing its
  Y axis**: both are a temperature in the same unit, and the distance between
  the lines is the thing the chart is for. A second auto-fitted axis would draw
  a 1° indoor drift and a 20° outdoor swing as the same stroke. The two sides
  are sampled an order of magnitude apart (30 s indoor, quarter-hourly
  outdoor), so `/api/series` **carry-forward**s the outdoor observations onto
  the indoor bucket grid — held, because the last published value is what an
  outdoor reading means between publishes, and nulled once it is more than two
  publish intervals old so a dead outdoor poller reads as a gap rather than as
  a flat line. One missed publish is always bridged; how many further misses
  it takes to open a gap depends on where the grid stamps fall (see
  **carry-forward** in `GLOSSARY.md`). *Assumption, since #109 did not say:* combining them replaces
  the standalone outdoor temp card. `/api/outdoor-series` still publishes
  `temp` at outdoor cadence and is unchanged; the page no longer draws it.
- **`GET /api/latest`** — the newest reading plus every open event, as
  read-only JSON, for the house hub to poll (#70). awairelement stays the
  system of record; the hub stores nothing and derives its own card colour
  from `score`. Two things about this endpoint differ from its siblings on
  purpose, because it feeds a machine rather than a browser: it is **always
  Celsius whatever `TEMPERATURE_UNIT` says** (and says so, via `temp_unit`),
  and it publishes **both** timestamps — `ts` is the Element's clock, for a
  human to read as "as of", while `received_at` is this machine's and is the
  one a consumer should measure staleness against. Measuring age off `ts`
  alone is a subtraction across two clocks: a device running fast yields a
  negative age, so a dead poller would render healthy forever. An empty
  `readings` table answers 200 with `"reading": null`, not an error, so a
  consumer can tell "up, but the poller is dead" from "unreachable".
  `open_events` carries **spike events only** — both health events (no peak/
  baseline/threshold) are filtered out, for two different reasons.
  `metric="device"`, opened by `poller.handle_device_health`, because a consumer
  learns the same thing earlier from `received_at` going stale, and publishing
  it would make three numeric fields nullable for everyone. `metric="outdoor"`,
  opened by `outdoor.handle_outdoor_health` (#94), because that argument does
  *not* transfer — this endpoint publishes the indoor `received_at`, and a
  partial outdoor poll writes a row anyway — but `/api/latest` is the indoor
  contract, so an outdoor transport fact on it is a category error. A broken
  air-quality endpoint is already visible on `/api/outdoor-latest` as a NULL
  `aq_ts`.

- **`GET /api/outdoor-latest`** — the newest outdoor reading, whole, as
  read-only JSON, for the house hub's weather card (#71). The outdoor sibling
  of `/api/latest` and it inherits both of that endpoint's machine-facing
  rules: **source units regardless of `TEMPERATURE_UNIT`**, and an empty table
  answers 200 with `"reading": null` rather than an error.

  It publishes **three** clocks. `ts` is Open-Meteo's publish time for the
  weather block (what a human reads as "as of"); `received_at` is this
  machine's poll time and is the one to measure staleness against; and `aq_ts`
  dates the *air-quality* half specifically. That third one is the
  non-obvious one: air quality is CAMS-backed and hourly while weather is
  quarter-hourly, so a row stamped 14:15 routinely carries a `us_aqi` measured
  at 13:00. Before #71 that timestamp was parsed and thrown away, so a card
  saying "as of 14:15" over an hour-old AQI had no way to know better. A NULL
  `aq_ts` means the AQI has no known observation time — the air-quality fetch
  failed for that poll, or the row predates #71 — and the hub is expected to
  read that as *"no current AQI"* (yellow) rather than folding an undateable
  number into a green.

  Five units are named in the payload — `temp_unit`, `pressure_unit`,
  `wind_speed_unit`, `precipitation_unit` and `snowfall_unit` — and they are
  the five somebody converts. #71's own motivating card reads `62°F, 8 mph, 0.00 in`, so the hub
  turns Celsius into F, km/h into mph and mm into inches; and this app
  separately turns hPa into inHg at the `/api/outdoor-series` boundary and
  Celsius into F at every browser-facing one. Each of those is a silent
  multiply on a number the card exists to display, and an unlabelled payload
  gives a consumer no way to notice it guessed wrong.

  The particulate and gas fields are deliberately **not** labelled. They have
  one spelling everywhere (Open-Meteo's native µg/m³), `us_aqi` is a
  dimensionless index and `humid` a percent, and nothing on either side of this
  contract converts them — so naming them would be documentation rather than
  disambiguation, and this paragraph is that documentation.

  `snowfall` is snow depth in **cm** (#79), and it is a separate field rather
  than something derived from `precipitation` because it cannot be: that one is
  the water equivalent in mm and the depth-to-water ratio moves with the snow's
  density. `snowfall_unit` is `cm` where `precipitation_unit` is `mm`, on
  purpose — one shared label would be a silent 10x on the one number whose
  threshold is *"more than 4 inches of snow"*. A NULL `snowfall` means
  **unknown**, not "it is not snowing": rows predating #79 never asked for the
  field, and the migration adds the column with no DEFAULT precisely so a
  fabricated zero cannot read as an all-clear.

  `weather_code` is the WMO interpretation code as the **integer** the source
  published. The code→word mapping is the hub's, for the same reason
  `/api/latest` ships open events rather than a card colour: awairelement is
  the system of record and publishes facts, the consumer renders. Rows written
  before #71 carry NULL for both new columns — the migration deliberately does
  not backfill either, since inventing an observation time is precisely what
  `aq_ts` exists to prevent. **Expect NULLs for the first quarter-hour after
  deploy**, until the outdoor poller writes its first post-migration row. That
  is correct behaviour and it will look like a bug to whoever is watching.

- **`GET /api/outdoor-today`** — today's outdoor totals and extremes, in
  source units, over the **local-midnight** window (#79). The machine-facing
  day aggregate: `precipitation_total`, `snowfall_total`, `temp_min`,
  `temp_max`, `wind_speed_max`, `us_aqi_min`, `us_aqi_max`, plus the window's
  `start`/`end`.

  `/api/outdoor-series?range=today` looks like the answer and is not, for two
  independent reasons. It converts to display units — inches, inHg, °F — which
  is the silent multiply `/api/outdoor-latest` exists to avoid; and
  `series.bucket` emits `avg`/`min`/`max` with no sum at all. There is a trap
  worth naming, because a consumer who tries it anyway **finds that it works**:
  at `range=today` the bucket is 900 s and so is the source cadence, so each
  bucket holds exactly one point and `avg` equals the raw value. Summing `avg`
  therefore produces the right rain total today, by coincidence, and would
  start under-reporting silently the moment either number changed.

  `row_count` and `contributing_rows` are both published and they are **not the
  same number**. `row_count` is rows in the window, and it separates "no rain
  today" from "the poller has been down since 03:00" — a day total over three
  rows is not a day total, and without it the hub renders a confident zero.
  `contributing_rows` is per source column, because a column can be NULL on a
  row that exists: every row written before the `snowfall` migration is exactly
  that, so for one day after deploy `snowfall_total` is a real sum over a
  strict subset of the day. A field with no contributing values at all is
  `null`, never `0`.

  Summing rows is sound only because Open-Meteo's `current.precipitation` and
  `current.snowfall` each accumulate over the block's own `interval` — 900 s,
  equal to the publish cadence, so consecutive rows cover disjoint contiguous
  windows — and because `ts` is the primary key, so a re-poll cannot
  double-count a window. That argument needs the window's **width**; it does
  not need to know which side of `ts` the window falls on, and this codebase
  does not claim to know. The only consequence is at the midnight boundary,
  where at most one 900 s bucket is attributed to one day or the other.
  `outdoor.SOURCE_INTERVAL_SECONDS` carries the width assumption and the poller
  logs a warning if the source ever stops matching it; at `interval: 3600`
  these totals would over-report by about 4x with every individual value still
  correct.

- **`GET /api/weather-alerts`** — active National Weather Service alerts for
  the parcel (#79). Tornado and hurricane warnings are two of the three
  conditions the hub paints its card red for and neither has an Open-Meteo
  equivalent, so the outdoor poller fetches
  `api.weather.gov/alerts/active?point=<lat>,<lon>` on its existing 15-minute
  timer. No API key and no account; NWS asks only that the client identify
  itself, which `AWAIR_NWS_USER_AGENT` does.

  It lives here rather than in the hub because this app already polls the WAN
  against `AWAIR_LAT`/`AWAIR_LON` and already owns "outdoor conditions"; the
  hub is a viewport with no producer and no WAN reach by design, so alerts
  landing here means it needs no new process, systemd unit or Ansible role.

  **`alerts` is `null` until a poll has succeeded.** An empty list means "we
  asked NWS and it said none"; `null` means "we have never had an answer".
  That distinction is the endpoint's whole reason for existing — a failed poll
  rendering as an all-clear is the one direction in which a missing number
  becomes a false statement about a tornado. Both clocks ship beside it:
  `last_attempt_at` says the poller is alive and trying, `last_success_at` is
  what an all-clear has to be measured against, and the two diverging is
  exactly a sustained outage. Nothing is ever deleted, so a transient NWS
  failure keeps publishing the last known set rather than emptying it, and one
  unreadable feature fails the **whole** poll rather than storing the readable
  remainder under a successful clock. Severity ranking is the consumer's, for
  the same reason card colour and the WMO code→word mapping are.

  The endpoint is `/api/weather-alerts` rather than `/api/alerts`: `awair.alerts`
  is this app's ntfy notifier and `alert_events` is its own spike bookkeeping,
  both a year older, and `/api/alerts` beside `/api/latest`'s `open_events`
  would read as those.

  **Nothing on this side acts on those two clocks diverging, and that is
  deliberate (#114).** A failing NWS fetch logs a WARNING every 15 minutes,
  stamps `last_attempt_at`, and stops there: its status never reaches
  `OutdoorHealth`, and `outdoor.main` discards the alert poll's return value on
  purpose. Feeding it in would escalate through `handle_outdoor_health`, which
  notifies at *high* priority for `unreachable` — waking someone for a fault on
  a third party's box — and would contend with the Open-Meteo tracker's own
  open row, since `db.get_open_events` returns at most one open event per
  metric (the collision #94 records for `device` against `outdoor`; it is a
  read-side property, not a constraint on the table).
  `tests/test_outdoor.py::test_an_nws_outage_does_not_escalate_the_outdoor_poller_to_unreachable`
  pins that: an NWS outage sends no notification and opens no `outdoor` event.

  **So detection of a *sustained* outage belongs to the consumer**, as the
  divergence between `last_attempt_at` and `last_success_at`. That is a
  contract, not a hope, and it has a failure mode worth naming: if the hub
  never implements the comparison, a permanently failing NWS fetch is silent
  everywhere except this app's log. The endpoint publishes everything the check
  needs and nothing here will notice it going unused. Read that as the price of
  keeping two upstreams' verdicts apart, and re-read it the day a card colour
  actually hangs off the alert feed — at which point the absent check becomes
  testable from the hub's side, which is the better place to decide it.

- **`GET /health`** — whether each feed is still recording (#158). The deploy
  check for this app used to be `GET /`, which proves the web process is up and
  the database opens and says nothing whatever about the four pollers that fill
  it: every feed could have stopped a week ago and that check would still pass.

  One entry per feed — the Awair device, the basement sensor, the outdoor
  weather, and each mini split head — with the time of its newest stored
  reading, that reading's age in seconds, and the window it is judged against.
  **200 when every feed is fresh, 503 when any is stale or has no rows at all**,
  and `stale` at the top level names the ones that failed. The body is the same
  either way: the code is for the deploy, the body is for whoever reads the
  failure.

  ```console
  $ curl -s localhost:8097/health | jq '{status, stale}'
  { "status": "ok", "stale": [] }
  ```

  **It reads the database and nothing else** — no device, no bridge, no
  Open-Meteo. Not an optimisation: a check that reaches for the network cannot
  tell "the feed stopped recording" from "the check could not ask", and goes
  red in exactly the outage it exists to describe. It needs no login, like
  every route here, and publishes nothing the dashboard does not — four names,
  four timestamps already drawn as points on the page, and the cadences
  documented above. Not `minisplit_readings.raw`, which carries device
  identifiers (ADR-019).

  Age is measured against `received_at`, **our** clock, for the reason
  `/api/latest` gives for publishing both: `ts` is the source's on three of
  the four tables, and a source clock stepping backwards would age a healthy
  feed by an hour it was recording through.

### Is it still recording?

Each feed is judged against **its own cadence**, because the pollers run 30x
apart at the extremes and one wall-clock threshold for all of them is either
blind indoors or permanently red outdoors. The rule is one missed-poll count
(`AWAIR_HEALTH_STALE_POLLS`, default 6) times each feed's own
`*_POLL_SECONDS`, which is read from the poller that sets it rather than
restated:

| feed | cadence | stale after |
|---|---|---|
| Awair Element | 30 s | 3 min |
| basement sensor | 60 s | 6 min |
| each mini split head | 300 s | 30 min |
| humidifier | 300 s | 30 min |
| outdoor weather | 900 s | 90 min |

Six, and this repo's own numbers bracket it. Below it, the carry-forward
windows are **two** intervals (`web._OUTDOOR_CARRY_INTERVALS`,
`monitor.HEALTH_RUN_MAX_AGE_INTERVALS`) — the floor for "this observation is
still evidence about now", and a verdict that can fail a deploy wants more
margin than a gap in a chart. Above it, the pollers' own ntfy alerting fires at
**ten** indoor polls and **four** outdoor ones. Six is also what the mini split
needs: `minisplit.MAX_REPORT_WINDOW_SECONDS` is 800, so the bridge can
legitimately leave 800 s between two rows, and two or three polls would call a
healthy bridge dead.

**One feed is allowed to be absent.** The humidifier needs a VeSync login
this repo does not hold, so a box without one would otherwise fail its own
deploy check for ever. `awair.feeds` marks that feed `optional`, and the rule
is drawn on the **database**: a table that has *never* held a row reads as
`not_configured` and is listed separately from `stale`, where a table that
has rows and has gone quiet is as red as any other feed. One row, ever, moves
it into the ordinary rules permanently.

The rule is deliberately not "is `AWAIR_VESYNC_EMAIL` set in the web worker's
environment". That variable says nothing about whether the humidifier unit is
installed, enabled or running — and on a box where somebody set it and never
installed the unit, it would fail the deploy for ever with no poller to fix.
Rows on disk are the only evidence either process has that a feed is real.
The blind spot that leaves, stated rather than hidden: a humidifier that *is*
configured and has never managed one successful poll reads as not set up.
Its own unit logs a clear line every poll, and
`systemctl --user status awairelement-humidifier` is where that case is
answerable.

Two consequences worth knowing before the first 503:

- **A mini split head configured off is stale, not excused.** `minisplit.zones`
  reads `bedroom: null` as "the zone is off" and writes no row, which this
  endpoint cannot tell from a dead bridge — and deliberately does not try to,
  since a feed quietly dropped off a health check is the failure mode the whole
  thing exists to remove. Turn a head off for good and either `AWAIR_HEALTH_STALE_POLLS`
  or `awair.feeds.FEEDS` has to say so on purpose.
- **The web process resolves the pollers' cadences from its own environment.**
  That is the pollers' cadence only because all five units load one
  `EnvironmentFile`; `tests/test_web_health.py::test_every_unit_reads_the_same_environment_file`
  pins the assumption where this repo can pin it, and the canonical units are
  templated in the homelab repo.


## The humidifier feed

The main floor's humidifier is a **Levoit Superior 6000S** on a VeSync
account, and this records what it is doing: its own humidity and temperature,
the target it is set to, mist level, mode, whether it is on, whether the tank
is empty or lifted out, and filter life. One row per poll in
`humidifier_readings`, deduplicated on the poll-minute the way the basement
feed is — the device's state carries no clock of its own.

Nothing here **changes** a humidifier setting. That is a later job (#160) and
probably a different repo.

```bash
export AWAIR_VESYNC_EMAIL="you@example.com"
export AWAIR_VESYNC_PASSWORD="…"          # never logged; see below
uv run python -m awair.humidifier
```

**Both variables or nothing.** With either one missing the poller writes one
line naming the two variables and exits 0, before it opens the database or
imports `pyvesync`. That is a normal state — four of the five feeds are local
and configuration-free — which is why its unit is the one that says
`Restart=on-failure` rather than `Restart=always`, and why `/health` reports
the feed as *not configured* rather than red.

**Neither credential reaches a log line**, and that is enforced rather than
intended. `pyvesync`'s own redaction stays on (`VeSync(redact=True)`, its
default, which blanks `"email"` and `"password"` out of a debug-level request
dump), and on top of it this poller installs a filter on the root logger's
handlers that replaces either value with `##_REDACTED_##` anywhere in any
record — including records the library writes, and including a traceback.
Two guards because the one that is only a default is the one an upgrade can
move. `tests/test_humidifier.py::test_no_log_line_anywhere_carries_either_credential`
drives a whole poll cycle — a successful read, a device error, and a login
failure whose *message* contains the password — through the real filter and
asserts neither value survives.

**Five minutes, and a back-off.** The cadence is `AWAIR_HUMIDIFIER_POLL_SECONDS`
(default 300) and 300 is also the floor: a smaller value is logged and raised,
because the limit here is somebody else's — VeSync rate-limits per account,
and this process shares that account with every phone in the house. When the
library reports a rate limit the poller doubles its wait (600 s, 1200 s,
2400 s, capped at an hour) and resets on the first good poll. Only a quota
refusal does that: a timeout or a server error costs one poll at the ordinary
cadence, like every other feed here.

**It runs in its own process, not a thread.** `pyvesync` 3.x is async-only
and the other four pollers are plain blocking loops. A fifth process keeps
the two shapes apart — a hung cloud read cannot stall a local feed, and vice
versa — and the only bridge needed is the stop signal, which crosses with
`asyncio.to_thread(stop.wait, delay)` so one clean-shutdown contract serves
both. `pyvesync` is imported **lazily**, inside the poll loop, through
`awair.vesync`: `awair.feeds` reads this poller's cadence and the Flask app
imports `awair.feeds`, so a module-scope import would load an async HTTP
stack into both gunicorn workers and four pollers that never make a VeSync
call.

### Moisture, not just humidity

Relative humidity cannot be compared between two rooms at two temperatures.
The wood stove lowers the great room's reading mostly by *heating the air* —
the same water vapour fills a smaller fraction of what warm air could hold —
so 35 % in a 23 °C great room and 45 % in a 16 °C basement are close to the
same amount of water. **Dew point** can be compared: it moves when the
moisture moves and stays put when only the heat does.

So the dashboard computes one, from one formula, for every place that records
both a temperature and a humidity — the Awair, the basement, each mini split
head, the humidifier, outdoors — and draws them on one card. The Awair
publishes its own `dew_point` and it is deliberately **not** what that card
draws: six places read against six formulas would not be a comparison. The
gap between the two is measured rather than assumed
(`tests/test_dewpoint.py::test_the_devices_own_dew_point_agrees_with_ours`).

### The main zone against a target

`AWAIR_MAIN_ZONE_FEED` (default `awair`) names the feed that speaks for the
great room. The Main zone card draws that sensor's humidity against the
target — `AWAIR_HUMIDITY_TARGET`, 40 %, dropping to
`AWAIR_HUMIDITY_TARGET_COLD`, 35 %, when it is at or below
`AWAIR_HUMIDITY_COLD_OUTDOOR_C` (−12 °C, about 10 °F) outside, because indoor
moisture condenses on the coldest window in the room and a cold pane cannot
carry 40 %. The dashed target line **steps** where the weather crosses that
threshold; with no outdoor reading it holds the warm target, so a dead
outdoor poller cannot quietly relax it.

Three sensors are drawn because three sensors are in that one room and they
disagree. The Awair sits high above the fireplace, so it reads warmer than
living level and therefore a lower relative humidity for the same moisture;
the humidifier reads at the floor; the dining mini split head reads at head
height. Their three dew points are printed side by side under the card's
header — in one open room those should roughly agree even when the
humidities do not, and a standing gap between them is worth seeing.

Stretches where the humidifier was **off** (grey) or **out of water or had
its tank lifted out** (red) are shaded, so a sag in the trace can be read as
a refill or as a setting. A humidifier reporting nothing at all is unshaded:
"we could not read it" is not "it was off".

The card's other number is **hours below target in the last seven days**,
always that window whatever range button is pressed. It counts only time
there are readings for — a gap wider than the feed's own staleness window
contributes nothing — so it is a floor rather than an estimate, which is the
right direction for a figure a human reads as "how bad was last week".

## Running as a systemd user service

The `systemd/` directory ships six unit files you can drop into
`~/.config/systemd/user/`. They assume the checkout lives at
`~/sources/awairelement` and read environment from
`~/.config/awairelement/environment`.

```bash
mkdir -p ~/.config/systemd/user ~/.config/awairelement
ln -sf "$PWD/systemd/awairelement.service"       ~/.config/systemd/user/
ln -sf "$PWD/systemd/awairelement-web.service"   ~/.config/systemd/user/
# Optional feeds. The outdoor one needs AWAIR_LAT/AWAIR_LON; the mini split one
# only makes sense on the box running the parsons-pulse climate bridge.
ln -sf "$PWD/systemd/awairelement-outdoor.service"   ~/.config/systemd/user/
ln -sf "$PWD/systemd/awairelement-minisplit.service" ~/.config/systemd/user/
ln -sf "$PWD/systemd/awairelement-basement.service"  ~/.config/systemd/user/
# The humidifier one needs AWAIR_VESYNC_EMAIL and AWAIR_VESYNC_PASSWORD, and
# is the one unit with Restart=on-failure rather than Restart=always: with no
# login it exits 0 on purpose, and `always` would make that a crash loop.
ln -sf "$PWD/systemd/awairelement-humidifier.service" ~/.config/systemd/user/

cat > ~/.config/awairelement/environment <<'EOF'
AWAIR_URL=http://192.168.1.42/air-data/latest
AWAIR_DB=/home/YOU/data/awairelement/awair.db
AWAIR_NTFY_URL=https://ntfy.sh
AWAIR_NTFY_TOPIC=your-topic-name
AWAIR_NTFY_TOKEN=
EOF

systemctl --user daemon-reload
systemctl --user enable --now awairelement awairelement-web
journalctl --user -u awairelement -f       # follow poller logs
```

Config (environment): `AWAIR_URL` (default `http://192.168.68.51/air-data/latest`),
`AWAIR_DB` (default `~/data/awairelement/awair.db`), `AWAIR_POLL_SECONDS` (default 30),
`TEMPERATURE_UNIT` (default `C`, also accepts `F` and `K`; display-only — storage
stays Celsius).

If your checkout isn't at `~/sources/awairelement`, edit `WorkingDirectory=`
and `ExecStart=` in each unit before symlinking.

### Fan mitigation

**Shipped state: `MITIGATION_RETIRED = True`.**

That line is the declared kill-switch state, and it is not prose:
`tests/test_fans.py::test_the_declared_shipped_state_matches_the_kill_switch`
reads it and fails if it disagrees with `awair/fans.py`. So flipping the kill
switch is two one-line edits — the constant and that line — and **no test
edit**: the suite still refuses a silent flip, but it refuses it where an
operator can fix it in the same breath, rather than demanding a test change in
the middle of turning the fans off.

While it reads `True`, automatic mitigation is **disabled in code**: the poller
releases the fans and `AWAIR_FAN_MITIGATION_ENABLED` has no power whatever it
says. While it reads `False`, the rules below are what runs.

With `AWAIR_FAN_MITIGATION_ENABLED=true` and the kill switch off, the poller
runs the ceiling fans while CO2 is high. **On at 1000 ppm, off below 900**, one
speed, and never for more than 90 minutes at a stretch.

Three rules, in precedence order:

1. **PM2.5 suppresses.** At or above 100 µg/m³ the fans are blocked, and a
   running fan is forced off immediately — bypassing the rate limit, because
   fans re-suspend settled dust and that is the exact failure this prevents.
2. **The duration cap.** A run is bounded at `AWAIR_FAN_MAX_RUN_MINUTES`, after
   which the fans stay off until CO2 recovers below the off-threshold — not
   just until the 90 minutes elapse, or the fans would come straight back on.
3. **The CO2 band.** On at or above `AWAIR_CO2_FAN_ON`, off below
   `AWAIR_CO2_FAN_OFF`, hold in between. The gap is what stops the fans
   chattering as CO2 wanders across a single line at 30-second polls.

A missing or stale reading (older than 5 minutes) means **off** for both CO2
and PM2.5 — absent data is never treated as good news.

Why these numbers: fan mitigation was retired in
[#61](https://github.com/tclancy/awairelement/issues/61) after running the fans
32% of the time, and un-retired on this trigger only after measuring it against
eight weeks of real readings — **0.62% duty cycle, 7 runs, longest 1.5 hours,
no overnight running**. The reasoning, the measurements and what would reverse
it again: [ADR-002](docs/decisions/002-co2-only-fan-mitigation.md).

`tests/fixtures/co2_summer_2026.txt` holds that recording, and
`test_replay_summer_2026_stays_under_two_percent_duty` replays it through the
shipped rules on every test run — so lowering a threshold too far fails the
suite rather than surprising you in the kitchen. It is summer data; a winter
re-measurement is owed.

**Turning it off.** Unset the env var, or set
`awair.fans.MITIGATION_RETIRED = True` for a code-level kill switch that beats
the environment (it logs a warning if a deploy still asks for fans, and the
startup banner reads `disabled in code` rather than `off` — the two have
different fixes). Either way a poller with mitigation off **releases** the
fans: any fan it still believes it left running gets one `off` command on the
next poll that stores a reading, then it stops touching them. (No reading, no
release — if the Awair is unreachable the fans stay put until it recovers.)
Turning a fan on at the wall afterwards is safe: the poller will not fight you.
If the NodeMCU can't be reached it retries a handful of times, then sends one
high-priority ntfy asking you to use the wall switch rather than retrying
forever.

Smoke-test the fan + ntfy plumbing — `--test` deliberately ignores every gate
above, including the kill switch:

```bash
python -m awair.poller --test   # fans to speed1, sends "Fan test", exits
```

A running poller with mitigation off releases them again on the next poll or
two that stores a reading (after the 60s rate limit).

## Deploy (homelab)
`restart.sh` in the repo root runs `uv sync --frozen` and restarts both units
— use it as a one-shot after a `git pull`.

To have the units start at boot rather than only after login, run
`loginctl enable-linger $USER` once.

## Response times

Every dashboard request logs one line and carries a `Server-Timing` header
(#141). Before this there was nothing to look at: gunicorn runs without
`--access-logfile`, so the journal held worker lifecycle lines and not one
request, and "is it slow?" was unanswerable on the record.

```
[2026-10-30 11:10:34,305] WARNING in timing: request GET /api/series?range=30d status=200 dur_ms=229.2 bytes=511567 endpoint=series
```

The path carries its **query string**, because that is what the duration
varies with — `/api/series` is 40x slower at `?range=30d` than at `?range=today`.

```bash
# Everything slow today. Grep the level, do NOT use `journalctl -p warning`:
# systemd stamps every line captured from a process's stderr with the same
# priority, so the Python level survives only as text in the message.
journalctl --user -u awairelement-web --since today -o cat | grep 'WARNING in timing'

# The three slowest samples per path today, grouped by path.
journalctl --user -u awairelement-web --since today -o cat |
  sed -n 's/.*request [A-Z]* \([^ ]*\) .*dur_ms=\([0-9.]*\) .*/\2 \1/p' |
  sort -k2,2 -k1,1gr |
  awk '$2 != p { p = $2; n = 0 } n++ < 3 { printf "%-34s %8.1f ms\n", $2, $1 }'
```

`AWAIR_WEB_SLOW_MS` moves the `WARNING` threshold. The tier exists because
`WARNING` clears Python's default effective level on its own, so the one line
nobody can afford to lose survives a future edit undoing the `INFO`
configuration in `awair/timing.py`
(`tests/test_web_timing.py::test_a_slow_line_survives_an_operator_resetting_the_level`).

### What the numbers mean, measured

`docs/verification/response_time_probe.py` times every request the page makes,
against a real database. Run on the homelab, 2026-10-30, 319,836 readings in a
56 MB file:

| request | range | p50 | body |
|---|---|---|---|
| document (`/`) | — | 0.2 ms | 11 KB |
| three static assets | — | 0.2 ms each | 80 KB total |
| `/api/series` | today | 7.9 ms | 115 KB |
| `/api/series` | 7d | 70 ms | 344 KB |
| `/api/series` | **30d** | **308 ms** | **500 KB** |
| `/api/outdoor-series` | 30d | 8.3 ms | 55 KB |
| `/api/events` | 30d | 0.7 ms | 11 KB |
| every unranged `/api/*` | — | 0.4 ms | < 1 KB |

`/health` is timed by the same probe and is the one route whose cost grows with
the table: it takes `MAX(received_at)` on four tables and `received_at` is
indexed on none. **29 ms p50** against a synthetic 78 MB file at homelab scale
(580,000 rows), which is an eighth of `/api/series?range=30d` on the same file
and a cost the deploy pays rather than a page load. `db.newest_received_at`
records why there is no index.

So on a quiet box the server is not the delay. **Two costs sit outside what
any in-app timer can see**, and both are named in `awair/timing.py`:

- **Waiting for a worker.** The unit runs `gunicorn -w 2` with *sync* workers,
  so two requests are served at a time, and `dashboard.js` fires three `/api/*`
  fetches at once after the browser has already asked for the document and
  three static assets. A queued request's timer does not start until it stops
  queueing, and nothing in the WSGI environ carries an arrival stamp. **The
  browser's TTFB minus `Server-Timing: app` is that wait** — which is the
  reason for the header.
- **Transfer.** 500 KB of JSON over wifi, plus the client-side parse and the
  uPlot draw, is a wait the server attributes to nobody.

### Caching the rendered HTML

Proposed on #141, and measured rather than argued: `/` touches no database
(`tests/test_web_readonly.py::test_dashboard_renders_without_touching_the_database`)
and every one of its six template arguments is a process constant, so caching
the render at startup is **safe**. It is also worth **0.05 ms** — the whole
Jinja render, measured by the probe above, on the box. Jinja already caches the
*compiled* template; what a cache would remove is only the interpolation. The
500 KB JSON body beside it is four orders of magnitude more expensive.

## Development

```bash
uv sync
uv run pytest              # runs the full test suite with coverage
uv run pre-commit install  # ruff + trailing-whitespace + radon complexity gate
```

`awair/spikes.py` is a pure-function module and carries most of the
interesting unit tests — the poller and web modules are thin glue tested with
an in-memory SQLite fixture. See `tests/`.

## Layout

```
awair/
├── db.py        # connection PRAGMAs + idempotent schema bootstrap
│                # (`connect`) and the query-only `connect_readonly` the web app uses
├── poller.py    # the 30s loop: fetch → store → detect → alert
├── spikes.py    # baseline math + hysteresis (pure functions)
├── monitor.py   # device health checks (unreachable, stale)
├── alerts.py    # ntfy client
├── series.py    # server-side bucketing for the dashboard
├── minisplit.py # the mini split feed: one row per head per bridge cycle
├── basement.py  # the basement ESPHome sensor: one row per poll-minute
├── humidifier.py # the humidifier feed: one row per poll, VeSync-free
├── vesync.py    # the only module that imports pyvesync: log in, read the device
├── dewpoint.py  # dew point from a temperature and a humidity (pure functions)
├── main_zone.py # the humidity target, the zone's sensor, hours below target
├── units.py     # temperature conversion, in both directions
├── timing.py    # one log line + a Server-Timing header per web request
├── feeds.py     # what `/health` checks: one entry per recorded feed
└── web.py       # Flask app + JSON endpoints
```

## License

MIT — see [LICENSE](LICENSE).
