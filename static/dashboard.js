/* Awair dashboard: six single-series small multiples with min/max bands,
   synced crosshairs, and alert-event washes. No build step. */
(function () {
  "use strict";

  // Temp unit symbol is stamped on <body data-temp-unit-symbol="..."> by
  // the server so a single TEMPERATURE_UNIT env var flips both API values
  // and the display label together.
  const TEMP_UNIT = document.body.dataset.tempUnitSymbol || "°C";
  const NON_METRIC_LABELS = { device: "Device", outdoor: "Outdoor" };
const METRICS = {
    co2:   { name: "CO₂",      unit: "ppm",     digits: 0 },
    voc:   { name: "TVOC",     unit: "ppb",     digits: 0 },
    pm25:  { name: "PM2.5",    unit: "µg/m³",   digits: 1 },
    temp:  { name: "Temp",     unit: TEMP_UNIT, digits: 1 },
    humid: { name: "Humidity", unit: "%",       digits: 1 },
    score: { name: "Score",    unit: "",        digits: 0 },
  };
  const OUTDOOR_METRICS = {
    temp: {
      name: "Outdoor Temp",
      unit: TEMP_UNIT,
      digits: 1,
      colorVar: "--series-outdoor-temp",
    },
    precipitation: {
      name: "Precipitation",
      unit: "in",
      digits: 2,
      colorVar: "--series-outdoor-precip",
    },
    pressure: {
      name: "Pressure",
      unit: "inHg",
      digits: 2,
      colorVar: "--series-outdoor-pressure",
    },
  };

  // Storm-warning threshold: a 3-hour pressure drop of ~2 hPa (~0.06 inHg) is
  // the classic "front moving in" signal (#42). Arrow shows current direction
  // of change over the last 3 hours of the visible range.
  const PRESSURE_TREND_THRESHOLD_INHG = 0.06;
  const PRESSURE_TREND_WINDOW_SECONDS = 3 * 3600;
  // uPlot right-side y-axis (side: 1 = right). Fixed range keeps the eye on
  // rate-of-change; auto-fit would visually flatten normal variance.
  const PRESSURE_SCALE_MIN_INHG = 28.5;
  const PRESSURE_SCALE_MAX_INHG = 31.0;

  // The opening range is NOT written down here (#108). `web.DEFAULT_RANGE`
  // renders it as `aria-pressed="true"` on one button, and this reads it back
  // out; a literal here would be a second copy of the default that nothing
  // keeps in step with the server's, which is how the button row and the data
  // it fetched could disagree.
  //
  // The `||` covers a rendered row with NOTHING pressed, which happens if
  // `DEFAULT_RANGE` is ever set to a key absent from `RANGE_LABELS`.
  // `test_range_labels_cover_exactly_the_ranges_both_endpoints_accept` forbids
  // that today, so this is defence against a future edit, not a live path. It
  // is deliberately NOT a hard-coded range name: falling back to the first
  // button keeps the highlight and the fetch agreeing, which is the whole
  // point above. With no `.ranges button` at all the read throws and the
  // dashboard does not start -- correct, since there would be no way to change
  // range and nothing to fall back to.
  const opening =
    document.querySelector('.ranges button[aria-pressed="true"]') ||
    document.querySelector(".ranges button");
  const state = { range: opening.dataset.range, plots: [], events: [], dailyEvents: [] };
  // Cursor-broadcast group for the synced crosshair. Charts join it by
  // passing `cursor: { sync: { key: sync.key } }` — uPlot's constructor ends
  // with `syncGroup.sub(self)`, so that config IS the subscription. Do not
  // also call `sync.sub(plot)`: it registers each chart twice, and `pub()`
  // walks the group's array, so one cursor move ran `updateCursor` twice on
  // every peer (#90). Nothing here unsubscribes — `destroy()` does it, which
  // is what makes `load()`'s five-minute rebuild leak-free.
  const sync = uPlot.sync("awair");

  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function hexToRgba(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  // Thousands-grouped, because uPlot groups the numbers this one sits beside:
  // the axis reads "40,000" and the legend "8,410", so a header reading
  // "38400 ppb" is the same quantity written two ways on one card (#116).
  function fmt(value, digits) {
    return value == null
      ? "–"
      : Number(value).toLocaleString(undefined, {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });
  }

  // The header's second number: the highest single reading in the visible
  // range, which before #116 the card had no way to tell you.
  //
  // The drawn line is `avg` and uPlot's legend is LIVE — its `low`/`high` are
  // the extremes of the one bucket under the cursor, not of the range — so
  // reading the peak off the chart meant hovering the exact bucket that set
  // it. On `today` that bucket is 60 s wide and a spike is one of them, a few
  // pixels across. Tom's report was a 35,000-topping TVOC line against a
  // legend saying "high: 8,410": both numbers correct, neither one the answer.
  //
  // Computed in `awair.series.peak` and shipped on the payload rather than
  // derived here, so it is unit-tested and converted exactly once, with the
  // series it summarises. Bare of its unit on purpose — the header states the
  // unit once already, and this segment has to fit beside the overlay
  // suffixes on the temp and precipitation cards at phone widths.
  function peakSuffix(peak, digits) {
    return peak == null ? "" : " · peak " + fmt(peak, digits);
  }

  // Wash alert-event spans onto a chart, clipped to the plotting area.
  function eventWashPlugin(metric) {
    return {
      hooks: {
        drawClear: (u) => {
          const ctx = u.ctx;
          ctx.save();
          ctx.beginPath();
          ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
          ctx.clip();
          for (const ev of state.events) {
            if (ev.metric !== metric) continue;
            const color = cssVar(`--event-${ev.tier}`) || cssVar("--event-relative");
            const x0 = u.valToPos(ev.opened_at, "x", true);
            const x1 = u.valToPos(ev.closed_at ?? Date.now() / 1000, "x", true);
            ctx.fillStyle = hexToRgba(color, 0.12);
            ctx.fillRect(x0, u.bbox.top, Math.max(x1 - x0, 2), u.bbox.height);
          }
          ctx.restore();
        },
      },
    };
  }

  // Sunrise/sunset glyphs painted along the top of the plot canvas (#32).
  // Server ships `daily_events: [{ts, kind: "sunrise"|"sunset"}]` computed from
  // AWAIR_LAT/AWAIR_LON in the AWAIR_TZ zone; the plugin just paints them.
  // Drawn in `draw` so glyphs land above series and event wash but below the
  // crosshair overlay, same slot as the ceiling line.
  //
  // Glyph size scales with the active date range (#47) — 30d packs ~60 glyphs
  // into the plot so they stay small; today shows just 1–2 and needs the
  // headroom. Y-offset tracks font size so the glyph never clips off the top.
  const SOLAR_GLYPH_PX = { today: 16, "7d": 13, "30d": 10 };
  function sunMoonMarkersPlugin() {
    return {
      hooks: {
        draw: (u) => {
          if (!state.dailyEvents.length) return;
          const ctx = u.ctx;
          const size = SOLAR_GLYPH_PX[state.range] || 10;
          const y = u.bbox.top + size;
          ctx.save();
          ctx.beginPath();
          ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
          ctx.clip();
          ctx.fillStyle = cssVar("--ink-muted") || "#898781";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.font = `${size}px system-ui, -apple-system, sans-serif`;
          for (const ev of state.dailyEvents) {
            const x = u.valToPos(ev.ts, "x", true);
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
            ctx.fillText(ev.kind === "sunrise" ? "☀" : "☾", x, y);
          }
          ctx.restore();
        },
      },
    };
  }

  // uPlot binds its cursor to mouse events only, and no browser synthesises
  // mousemove during a touch drag — so on a phone there is no gesture that
  // reads a value off a chart and the legend never populates (#87). Drive the
  // cursor from touch directly instead of hoping for compatibility events.
  //
  // `.plot { touch-action: pan-y }` already hands vertical gestures to the
  // page and horizontal ones to us, so these listeners stay passive and never
  // preventDefault. The axis lock is still needed on top of that: a vertical
  // page scroll that starts on a chart streams touchmove at us the whole way
  // down the page, and without the lock the crosshair rides along with it.
  const TOUCH_AXIS_LOCK_PX = 8;

  function touchCursorPlugin() {
    return {
      hooks: {
        ready: (u) => {
          let start = null;
          let axis = null;
          // Captured once per gesture rather than per touchmove: reading it in
          // the move handler forces a synchronous layout right after the
          // previous move retransformed eight plots. Safe to cache because an
          // x-locked gesture is one `touch-action: pan-y` will not scroll.
          let rect = null;

          // uPlot nulls the cursor index — and blanks the legend to "--" — for
          // a left below zero or a top past the plot height, and with _pub on
          // that blank publishes to every synced card. The plot area starts
          // ~65px into the card, so scrubbing left toward older data crosses
          // zero long before the finger leaves the screen. Clamp so the read
          // saturates at the edge instead of wiping the page.
          const clamp = (v, hi) => (v < 0 ? 0 : v > hi ? hi : v);

          // (_fire, _pub) — _pub is what republishes to the `awair` sync
          // group, so scrubbing one card moves the crosshair on all of them
          // the way a mouse does. Omit it and the other five cards stay "--".
          const readAt = (touch) => {
            if (rect === null) return;
            u.setCursor(
              {
                left: clamp(touch.clientX - rect.left, rect.width - 1),
                top: clamp(touch.clientY - rect.top, rect.height - 1),
              },
              true,
              true
            );
          };

          u.over.addEventListener(
            "touchstart",
            (e) => {
              // targetTouches, not touches: `touches` is every finger on the
              // screen, so a finger already resting elsewhere would be the one
              // we tracked and the gesture would silently never move.
              const touch = e.targetTouches[0];
              if (!touch) return;
              start = { x: touch.clientX, y: touch.clientY };
              axis = null;
              rect = u.over.getBoundingClientRect();
              // Deliberately no read here. A gesture that turns out to be a
              // page scroll would otherwise still yank the crosshair to
              // wherever the finger happened to land. Taps read on touchend.
            },
            { passive: true }
          );

          u.over.addEventListener(
            "touchmove",
            (e) => {
              const touch = e.targetTouches[0];
              if (!touch || start === null) return;
              if (axis === null) {
                const dx = Math.abs(touch.clientX - start.x);
                const dy = Math.abs(touch.clientY - start.y);
                if (Math.max(dx, dy) < TOUCH_AXIS_LOCK_PX) return;
                // Locked once and kept for the rest of the gesture: a scroll
                // that drifts sideways halfway down must not become a scrub.
                axis = dx > dy ? "x" : "y";
              }
              if (axis === "x") readAt(touch);
            },
            { passive: true }
          );

          u.over.addEventListener(
            "touchend",
            (e) => {
              const touch = e.changedTouches[0];
              // axis === null means the finger never travelled far enough to
              // be classified — that is a tap, and a tap is a read.
              if (touch && start !== null && axis === null) readAt(touch);
              start = null;
              axis = null;
              rect = null;
            },
            { passive: true }
          );

          // touchcancel is the same teardown: the OS took the gesture (a call
          // arrived, a system edge-swipe won) and no touchend is coming.
          u.over.addEventListener(
            "touchcancel",
            () => {
              start = null;
              axis = null;
              rect = null;
            },
            { passive: true }
          );

          // Nothing clears the values afterwards, on purpose. There is no
          // mouseleave on touch, and leaving the last-read values on screen
          // is the whole point — clearing them puts us back at #87.
        },
      },
    };
  }

  // Dashed horizontal reference line at the alert ceiling for this metric.
  // Anchors the eye when uPlot autoscales Y to a peak so 1500 ppb VOC doesn't
  // read as "cleared" when it's still 15× baseline and above the ceiling (#25).
  // Drawn in the `draw` hook so it lands over the series and event wash but
  // below the crosshair overlay.
  function ceilingLinePlugin(ceiling) {
    return {
      hooks: {
        draw: (u) => {
          if (u.scales.y.min == null || u.scales.y.max == null) return;
          if (ceiling < u.scales.y.min || ceiling > u.scales.y.max) return;
          const ctx = u.ctx;
          const y = Math.round(u.valToPos(ceiling, "y", true)) + 0.5;
          const color = cssVar("--event-ceiling") || "#d03b3b";
          ctx.save();
          ctx.beginPath();
          ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
          ctx.clip();
          ctx.strokeStyle = hexToRgba(color, 0.55);
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 3]);
          ctx.beginPath();
          ctx.moveTo(u.bbox.left, y);
          ctx.lineTo(u.bbox.left + u.bbox.width, y);
          ctx.stroke();
          ctx.restore();
        },
      },
    };
  }

  function makePlot(card, metric, series, outdoorTemp, peak) {
    const meta = METRICS[metric];
    const color = cssVar(`--series-${metric}`);
    const plotEl = card.querySelector(".plot");
    plotEl.innerHTML = "";

    // #109: the outdoor temperature trace rides on the indoor temp card.
    // Dispatched on `data-overlay`, not on `metric === "temp"`, because the
    // CSS reserves this card's extra legend row from the same attribute — one
    // marker, so the fifth entry and the space for it cannot disagree.
    //
    // The server ships `outdoor_temp` already on `series.t` (see
    // `web._outdoor_temp_on_grid`): uPlot takes one x array per chart, and the
    // two sides are sampled an order of magnitude apart. Nothing here
    // resamples, interpolates or holds anything — that is a decision about
    // what a stale reading means, and it belongs in Python where it is tested.
    const overlayOutdoor =
      card.dataset.overlay === "outdoor-temp" && Array.isArray(outdoorTemp);

    // Server stamps the alert ceiling on the card when the metric has one in
    // spikes.METRICS. Missing → no reference line for this chart.
    const ceilingRaw = card.dataset.ceiling;
    const ceiling = ceilingRaw ? Number(ceilingRaw) : null;
    const plugins = [
      eventWashPlugin(metric),
      sunMoonMarkersPlugin(),
      touchCursorPlugin(),
    ];
    if (ceiling != null && Number.isFinite(ceiling)) {
      plugins.push(ceilingLinePlugin(ceiling));
    }

    const data = overlayOutdoor
      ? [series.t, series.min, series.max, series.avg, outdoorTemp]
      : [series.t, series.min, series.max, series.avg];
    const axisStyle = {
      stroke: cssVar("--ink-muted"),
      grid: { stroke: cssVar("--grid"), width: 1 },
      ticks: { stroke: cssVar("--axis"), width: 1 },
      font: "11px system-ui, sans-serif",
    };
    const seriesConfig = [
      // Year-free hover timestamp: the full default ("2026-07-11 10:50am")
      // wraps the legend row in a card this narrow, and the row growing to
      // two lines shifts every chart below it.
      { value: "{M}/{D} {h}:{mm}{aa}" },
      { label: "low", stroke: null, points: { show: false } },
      { label: "high", stroke: null, points: { show: false } },
      {
        label: "avg",
        stroke: color,
        width: 2,
        points: { show: false },
        spanGaps: false,
      },
    ];
    if (overlayOutdoor) {
      seriesConfig.push({
        label: OUTDOOR_METRICS.temp.name,
        stroke: cssVar(OUTDOOR_METRICS.temp.colorVar),
        width: 1.5,
        // Deliberately NO `scale:` — this shares the card's y axis, unlike
        // #42's pressure overlay two functions down. Both lines here are a
        // temperature in the same unit, and the distance between them is the
        // answer #109 asked for. Two auto-fitted axes would draw a 1° indoor
        // drift and a 20° outdoor swing as the same stroke.
        value: (u, v) => (v == null ? "–" : v.toFixed(meta.digits) + " " + meta.unit),
        points: { show: false },
        // The server nulls the trace once the last outdoor observation goes
        // stale, so a dead outdoor poller has to render as a break. Spanned,
        // it would render as a flat line instead — a wrong answer, not a
        // missing one.
        spanGaps: false,
      });
    }

    const plot = new uPlot(
      {
        width: plotEl.clientWidth || 320,
        height: 150,
        cursor: { sync: { key: sync.key }, points: { size: 7 } },
        legend: { live: true },
        scales: { x: { time: true } },
        bands: [{ series: [2, 1], fill: hexToRgba(color, 0.14) }],
        series: seriesConfig,
        axes: [
          { ...axisStyle },
          { ...axisStyle, size: 52 },
        ],
        plugins,
      },
      data,
      plotEl
    );
    state.plots.push(plot);

    // Header: name, unit, colored dot, latest value.
    card.querySelector(".name").textContent = meta.name;
    card.querySelector(".unit").textContent = meta.unit;
    card.querySelector(".dot").style.background = color;
    const latest = [...series.avg].reverse().find((v) => v != null);
    let nowText = fmt(latest, meta.digits) + (meta.unit ? " " + meta.unit : "");
    // Before the overlay suffix, so this card's own two numbers stay adjacent.
    nowText += peakSuffix(peak, meta.digits);
    if (overlayOutdoor) {
      const latestOutdoor = [...outdoorTemp].reverse().find((v) => v != null);
      // Prefixed rather than bare: two numbers in the same unit side by side
      // is exactly the header that reads as a range.
      nowText +=
        " · out " + fmt(latestOutdoor, meta.digits) +
        (meta.unit ? " " + meta.unit : "");
    }
    card.querySelector(".now").textContent = nowText;
  }

  function renderEvents() {
    const box = document.getElementById("events-body");
    if (!state.events.length) {
      box.innerHTML = '<div class="empty">No alert events in this range. 🎉</div>';
      return;
    }
    const rows = state.events
      .slice()
      .reverse()
      .map((ev) => {
        const opened = new Date(ev.opened_at * 1000).toLocaleString();
        const closed = ev.closed_at
          ? new Date(ev.closed_at * 1000).toLocaleString()
          : "open";
        const meta = METRICS[ev.metric];
        // `/api/events` applies no metric filter, so both health events reach
        // this table. Falling through to "Device" labelled an outdoor poller
        // outage as the indoor Element (#94).
        const label = meta ? meta.name : NON_METRIC_LABELS[ev.metric] || "Device";
        const peak = ev.peak_value == null ? "–" : ev.peak_value;
        return `<tr>
          <td>${label}</td>
          <td><span class="badge ${ev.tier}">${ev.tier}</span></td>
          <td>${opened}</td>
          <td>${closed}</td>
          <td>${peak}</td>
        </tr>`;
      })
      .join("");
    box.innerHTML = `<table>
      <thead><tr><th>Metric</th><th>Tier</th><th>Opened</th><th>Closed</th><th>Peak</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  }

  // Latest non-null pressure (uses the min series) and the direction of change
  // over the last 3 hours. Returns {value, arrow} or null if no signal.
  function pressureSummary(series) {
    if (!series || !series.t || !series.min) return null;
    let latestIdx = -1;
    for (let i = series.min.length - 1; i >= 0; i--) {
      if (series.min[i] != null) {
        latestIdx = i;
        break;
      }
    }
    if (latestIdx < 0) return null;
    const latestT = series.t[latestIdx];
    const latestV = series.min[latestIdx];
    const windowStart = latestT - PRESSURE_TREND_WINDOW_SECONDS;
    let earlyV = null;
    for (let i = 0; i <= latestIdx; i++) {
      if (series.min[i] != null && series.t[i] >= windowStart) {
        earlyV = series.min[i];
        break;
      }
    }
    let arrow = "";
    if (earlyV != null) {
      const delta = latestV - earlyV;
      if (delta < -PRESSURE_TREND_THRESHOLD_INHG) arrow = "↓";
      else if (delta > PRESSURE_TREND_THRESHOLD_INHG) arrow = "↑";
      else arrow = "→";
    }
    return { value: latestV, arrow };
  }

  function makeOutdoorPlot(card, metric, series, allMetrics) {
    const meta = OUTDOOR_METRICS[metric];
    const color = cssVar(meta.colorVar);
    const plotEl = card.querySelector(".plot");
    plotEl.innerHTML = "";

    // Pressure overlays onto the precipitation chart (#42) — one card carries
    // the storm-signal glance: rain accumulation + pressure trace + trend
    // arrow. The pressure series is min-per-bucket (the trough matters more
    // than the average for a front-moving-in signal).
    // `data-overlay`, not the metric name: the CSS reserves this card's third
    // legend row from the same attribute (#109), so a card that loses the
    // marker loses the fifth entry and the space for it together rather than
    // drawing one into a box sized for four.
    const overlayPressure =
      card.dataset.overlay === "pressure" && allMetrics && allMetrics.pressure;
    const pressureSeries = overlayPressure ? allMetrics.pressure : null;
    const pressureColor = overlayPressure
      ? cssVar(OUTDOOR_METRICS.pressure.colorVar)
      : null;

    const data = overlayPressure
      ? [series.t, series.min, series.max, series.avg, pressureSeries.min]
      : [series.t, series.min, series.max, series.avg];
    const axisStyle = {
      stroke: cssVar("--ink-muted"),
      grid: { stroke: cssVar("--grid"), width: 1 },
      ticks: { stroke: cssVar("--axis"), width: 1 },
      font: "11px system-ui, sans-serif",
    };
    const seriesConfig = [
      { value: "{M}/{D} {h}:{mm}{aa}" },
      { label: "low", stroke: null, points: { show: false } },
      { label: "high", stroke: null, points: { show: false } },
      {
        label: "avg",
        stroke: color,
        width: 2,
        points: { show: false },
        spanGaps: false,
      },
    ];
    const scales = { x: { time: true } };
    const axes = [{ ...axisStyle }, { ...axisStyle, size: 52 }];
    if (overlayPressure) {
      seriesConfig.push({
        label: "Pressure",
        stroke: pressureColor,
        width: 1.5,
        scale: "pressure",
        value: (u, v) => (v == null ? "–" : v.toFixed(2) + " inHg"),
        points: { show: false },
        spanGaps: false,
      });
      scales.pressure = {
        range: [PRESSURE_SCALE_MIN_INHG, PRESSURE_SCALE_MAX_INHG],
      };
      axes.push({
        ...axisStyle,
        side: 1,
        scale: "pressure",
        size: 44,
        values: (u, splits) => splits.map((v) => v.toFixed(1)),
      });
    }
    const plot = new uPlot(
      {
        width: plotEl.clientWidth || 320,
        height: 150,
        cursor: { sync: { key: sync.key }, points: { size: 7 } },
        legend: { live: true },
        scales,
        bands: [{ series: [2, 1], fill: hexToRgba(color, 0.14) }],
        series: seriesConfig,
        axes,
        plugins: [sunMoonMarkersPlugin(), touchCursorPlugin()],
      },
      data,
      plotEl
    );
    state.plots.push(plot);
    card.querySelector(".name").textContent = meta.name;
    card.querySelector(".unit").textContent = meta.unit;
    card.querySelector(".dot").style.background = color;
    const latest = [...series.avg].reverse().find((v) => v != null);
    let nowText = fmt(latest, meta.digits) + (meta.unit ? " " + meta.unit : "");
    // Deliberately NO peak on this card, and it is a layout fact rather than a
    // preference (#116). This is the composite storm-signal card: its header
    // already carries two numbers of two different quantities (rain
    // accumulation and the pressure trace's latest + trend arrow, #42), under
    // the longest card name on the page. Measured at 1440px, a third segment
    // wrapped `.card h2` onto a second line, which grows this card taller than
    // its row-mates -- the same "a populated row is taller than an idle one"
    // shift `--legend-rows` exists to prevent, one element up. The peak is a
    // metric-card feature; `test_the_range_peak_is_rendered_on_the_metric_
    // cards_and_only_there` is what makes a third chart factory decide rather
    // than inherit.
    if (overlayPressure) {
      const summary = pressureSummary(pressureSeries);
      if (summary != null) {
        nowText +=
          " · " + fmt(summary.value, 2) + " inHg " + summary.arrow;
      }
    }
    card.querySelector(".now").textContent = nowText;
  }

  async function load() {
    const [seriesRes, eventsRes, outdoorRes] = await Promise.all([
      fetch(`/api/series?range=${state.range}`),
      fetch(`/api/events?range=${state.range}`),
      fetch(`/api/outdoor-series?range=${state.range}`),
    ]);
    const seriesPayload = await seriesRes.json();
    state.events = (await eventsRes.json()).events;
    const outdoorPayload = await outdoorRes.json();
    state.dailyEvents = outdoorPayload.daily_events || [];

    state.plots.forEach((p) => p.destroy());
    state.plots = [];
    for (const card of document.querySelectorAll(".card[data-metric]")) {
      const metric = card.dataset.metric;
      makePlot(
        card,
        metric,
        seriesPayload.metrics[metric],
        seriesPayload.outdoor_temp,
        seriesPayload.peaks[metric]
      );
    }
    for (const card of document.querySelectorAll(".card[data-outdoor]")) {
      const metric = card.dataset.outdoor;
      makeOutdoorPlot(
        card,
        metric,
        outdoorPayload.metrics[metric],
        outdoorPayload.metrics
      );
    }
    renderEvents();
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  }

  document.querySelectorAll(".ranges button").forEach((button) => {
    button.addEventListener("click", () => {
      state.range = button.dataset.range;
      document
        .querySelectorAll(".ranges button")
        .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      load();
    });
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(load, 200);
  });
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", load);

  load();
  setInterval(load, 5 * 60 * 1000); // refresh every 5 min
})();
