# Basement temperature: scope

*Written 2026-10-28. Tom moved the new ESPHome sensor to the basement, "the one
place we really do not have any visibility into", and asked to track it "as an
addition to the temperature graph on the awair". This is a scope document and
a simple plan, not a design. Temperatures are stored in °C like every other
temperature in this database; timestamps are UTC. An independent agent
reviewed it against the code and the box; its findings are folded in.*

## Facts this rests on

| Fact | Source |
|---|---|
| The sensor is an Apollo ESPHome Starter Kit: an ESP32-C6 with an AHT-family temperature/humidity module at I²C `0x38`. It's at **`192.168.68.50`**, reserved in the Deco by MAC. The device is being renamed `esphome-temperature`; the address doesn't change. | set up 2026-10-27 |
| `GET http://192.168.68.50/sensor/Temperature` → `{"id":"sensor/Temperature","value":15.1268,"state":"15.13 °C"}`. **Celsius. No timestamp.** It's the last *published* value. The path is keyed on the **entity** name (`Temperature`), not the device, so renaming the device is safe and renaming the entity is a 404. | measured 2026-10-28 |
| The module publishes every 60 s (ESPHome's default `update_interval`). | ESPHome default |
| **The pattern this would copy has never been deployed.** `awair/minisplit.py` (#140) is on `main`, but the box is at `6fc847d`, before #140. There's no `awairelement-minisplit` unit on the box, no `minisplit_readings` table there, and no homelab template for it. | box, 2026-10-28 |
| The dashboard draws one overlay, `outdoor_temp`, on the temperature card (#109). That's single-valued throughout: `METRIC_OVERLAYS = {"temp": "outdoor-temp"}`, `card.dataset.overlay === "outdoor-temp"` (`dashboard.js`), and `makePlot(…, outdoorTemp, …)`. The legend reservation `.card[data-overlay] .u-legend { --legend-rows: 3 }` matches *any* card with the attribute, precipitation's `pressure` overlay included. `/api/series` already serves `minisplit_temp`, but nothing draws it. | `web.py`, `dashboard.js`, `dashboard.html` |
| awair units in homelab deliberately have **no `OnFailure=`**: it would page on graceful stops (`awairelement.service.j2`). A new awair unit needs a `.j2` template, a task in `native-apps/tasks/main.yml`, a handler, its env key in `awair.env.j2`, `SuccessExitStatus=143`, and `uv run --frozen`. The repo's own `systemd/` copies differ from homelab's (`.venv/bin/python`). | homelab `native-apps` |

## Goals

1. **Record the basement temperature** in its own table, one row per published
   minute.
2. **Draw it on the temperature card** as a second overlay line, "Basement",
   beside outdoor. The server aligns it to the grid; the browser resamples
   nothing.

## Non-goals

- **Humidity.** It's a second GET with its own failures and clock, and nobody
  asked to see it.
- **Alerts.** A basement freeze/damp alert is the obvious next step, and it's
  why visibility matters, but it's a separate decision about thresholds.
- **Drawing the dining head's `minisplit_temp`.** It becomes cheap once
  overlays are a list. Not asked for.
- **Correcting the sensor.** The 0.62 °F gap against the Awair was measured in
  a different room, and no sensor in the house is "true". The line shows what
  the sensor reads.
- **A parsons-pulse hub card; Home Assistant / Matter.**

## Decisions the design must make

| Question | Lean |
|---|---|
| **Deploy minisplit first, alongside, or not at all?** It's merged code that isn't running, and it's the template this copies. | Its homelab unit PR goes first. It's a known-good shape to copy, and it ends the gap where `main` and the box disagree. Tom to confirm. |
| **Row key.** There's no timestamp in the payload, and a raw poll clock is unique every time, so dedup could never fire. Polling every 60 s plus fetch time also drifts against a 60 s publish. | Key on the poll clock **truncated to the minute** (`+00:00` isoformat, like the other tables) with `INSERT OR IGNORE`. One row per publish-minute; a second poll in the same minute writes nothing. |
| **What counts as no reading.** ESPHome's representation of an unavailable sensor is unconfirmed (probably `"state":"NA"` with `value` null or missing). | Null, missing or non-finite `value` → no row, checked **before** the `°C` unit check so `"NA"` isn't mistaken for a unit error. **Confirm by unplugging the module.** A stalled component serving a finite stale value can't be detected from this payload, so the scope promises gaps on fetch failure only. |
| **Address.** | IP literal: `AWAIR_BASEMENT_URL=http://192.168.68.50/sensor/Temperature`. |
| **Separate unit.** | `awairelement-basement.service`, so a hung read can't stall the Awair or outdoor pollers. |
| **Alignment to the chart grid.** Carry-forward samples one point per grid stamp, so on 7 d and 30 d the line would be single readings, not bucket averages like the indoor line. And the grid only spans the indoor data's range, so an Awair outage hides the basement line too. | **Bucket the basement rows on the same `bucket_seconds` (average) before aligning,** then carry for 2 intervals. State the grid-span behaviour in the code rather than fix it. Read one carry window before `since`, as `/api/series` does for its other feeds. |
| **Overlays become a list.** | `data-overlay` space-separated, matched with `split(" ").includes(...)`; overlays passed to `makePlot` as a map; `[data-overlay~="basement-temp"] { --legend-rows: 4 }`. Also: the header suffix (`· out X` gains `· bsmt Y`), a colour token in both light and dark blocks, the footer text, and GLOSSARY's "overlay" entry ("Two exist"). |

## Plan

Each step is its own PR, tests first.

0. **Deploy minisplit** (if confirmed): a homelab PR for the
   `awairelement-minisplit` unit, then deploy awairelement, then confirm
   `minisplit_readings` is filling up.
1. **This scope.** Docs only.
2. **Record:** the `basement_readings` table, `awair/basement.py`
   (`python -m awair.basement`), `systemd/awairelement-basement.service`, and
   tests for parsing, the no-reading refusals (null, missing, NaN, `"NA"`) and
   the per-minute key. Paired homelab PR with the unit pieces listed above.
   Deploy, confirm rows arrive, then unplug the module once to see the real
   unavailable payload.
3. **Draw:** `basement_temp` on `/api/series` (bucketed and carried in Python,
   tested), overlays as a list, and the second line on the temperature card.
   Checked in a browser at the desktop and phone widths in `docs/screenshots/`.

## Done means

- `basement_readings` gains about one row a minute, and unplugging the sensor
  for five minutes leaves a five-minute gap.
- The temperature card shows "Basement" beside outdoor on today, 7 and 30
  days, and outdoor still draws. Screenshots committed.
