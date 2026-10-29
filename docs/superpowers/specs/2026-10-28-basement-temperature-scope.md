# Basement temperature: scope

*Written 2026-10-28. Tom moved the new ESPHome sensor to the basement, "the one
place we really do not have any visibility into", and asked to track it "as an
addition to the temperature graph on the awair". This is a scope document and
a simple plan, not a design. Temperatures are stored in °C like every other
temperature in this database; timestamps are UTC.*

## Facts this rests on

| Fact | Source |
|---|---|
| The sensor is an Apollo ESPHome Starter Kit: an ESP32-C6 with an AHT-family temperature/humidity module at I²C `0x38`. It's at **`192.168.68.50`**, reserved in the Deco by MAC, so the address survives a rename. Hostname `bedroom-temperature` today; Tom will rename it. | set up 2026-10-27 |
| `GET http://192.168.68.50/sensor/Temperature` → `{"id":"sensor/Temperature","value":15.1268,"state":"15.13 °C"}`. **Celsius. No timestamp.** It's the last *published* value. `/sensor/Humidity` has the same shape. | measured 2026-10-28 |
| The module publishes every 60 s (ESPHome's default `update_interval`). Wi-Fi power-save makes a ping take 170–340 ms, which doesn't matter to an HTTP read. | measured 2026-10-27 |
| Against the Awair overnight (2026-10-28, 215 paired readings), it read 0.62 °F high on average (sd 0.31), with correlation 0.98. | parsons-pulse scope, 2026-10-28 |
| This repo already records an auxiliary temperature from another source: `awair/minisplit.py` polls the climate bridge, stores Celsius rows in `minisplit_readings`, and `/api/series` puts `minisplit_temp` on the temperature grid. **The dashboard doesn't draw it yet.** It draws one overlay, `outdoor_temp`, on the combined temperature card (#109), dispatched on `data-overlay` and carried onto the grid by `web._outdoor_temp_on_grid`. | `awair/minisplit.py`, `awair/web.py`, `static/dashboard.js` |
| Pollers run as their own systemd units (`systemd/awairelement-*.service`). `restart.sh` derives its unit list from `systemd/`. Installing a unit takes a homelab Ansible change too. | `restart.sh`, homelab `native-apps` |

## Goals

1. **Record the basement temperature**, and humidity since it's the same read,
   every 60 s, in its own table.
2. **Draw it on the combined temperature card** as a second overlay line,
   "Basement", beside outdoor. It uses the same server-side carry onto the
   grid and the same rule: the browser resamples nothing.

## Non-goals

- **Alerts.** A basement freeze or damp alert ("below 40 °F, tell me") is the
  obvious next thing, and it's why visibility matters, but it's a separate
  decision about thresholds. Recording comes first.
- **Drawing the dining head's `minisplit_temp`.** It would be nearly free once
  the second-overlay machinery exists, but Tom didn't ask for it.
- **A parsons-pulse hub card.**
- **Home Assistant, Matter, Thread.** Plain HTTP on the LAN, like everything
  else here.

## Decisions the design must make

| Question | Why it matters | Lean |
|---|---|---|
| **Which clock keys a row?** | The payload has no timestamp. `minisplit.py` refuses to key on its own poll clock because the bridge publishes a better one. Here there is no better one. | The poll clock, said out loud. It's a direct read of a sensor that republishes every 60 s, so "when we asked" is within a minute of "when it was measured". |
| **What counts as no reading?** | ESPHome serves the last published value. A failed read, `NaN` (`"state":"NA"`) or a non-finite number must store **nothing**, not a repeat. A stalled component that keeps serving a finite stale value can't be detected from this payload. | No row for failure or non-finite values. Name the stalled-value gap. The fix, if it ever matters, is an `uptime` sensor on the device. |
| **Units** | Every temperature column here is Celsius, converted once at ingest. | The sensor is already °C; store as is, and validate the unit from `state`'s suffix. |
| **Address** | Hostname resolution happens outside the socket timeout (the same reasoning as parsons-pulse's `AIR_URL`). | IP literal, `AWAIR_BASEMENT_URL=http://192.168.68.50/sensor/Temperature`. Renaming the device then changes nothing here. |
| **Poll rate** | Faster than 60 s re-reads the same value. | 60 s. |
| **Carry onto the chart grid** | The outdoor overlay holds a reading for 2 source intervals (`_OUTDOOR_CARRY_INTERVALS`). | The same rule, derived from the basement interval: hold for 2 × 60 s. A gap in the line means the sensor was gone, which is the honest picture. |
| **Separate unit or fold into an existing poller?** | A hung basement read must not stall the Awair or outdoor pollers. | Its own unit, `awairelement-basement.service`, the same shape as `-minisplit`. |
| **Legend and colour** | The temperature card reserves one extra legend row today (#109). | A second reserved row, keyed off the same `data-overlay` marker, now a list. |

## Plan

Each step is its own PR, tests first.

1. **This scope.** Docs only.
2. **Record:** `basement_readings` (ts, temp °C, humidity), `awair/basement.py`
   (`python -m awair.basement`), `systemd/awairelement-basement.service`, and
   tests for parsing, refusals and dedup. Paired **homelab PR** installing the
   unit (template, env, enable-and-start, the drift manifest and the OnFailure
   pairing). Deploy, then confirm rows are arriving.
3. **Draw:** `basement_temp` on `/api/series` (carried onto the grid in Python,
   with tests), and the second overlay on the temperature card. Checked in a
   browser at the desktop and phone widths in `docs/screenshots/`.

## Done means

- `basement_readings` gains a row about once a minute. A sensor unplugged for
  five minutes leaves a five-minute gap, not repeated values.
- The temperature card shows a "Basement" line beside outdoor over today, 7 and
  30 days, and the screenshots are committed.
