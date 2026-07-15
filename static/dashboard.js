/* Awair dashboard: six single-series small multiples with min/max bands,
   synced crosshairs, and alert-event washes. No build step. */
(function () {
  "use strict";

  // Temp unit symbol is stamped on <body data-temp-unit-symbol="..."> by
  // the server so a single TEMPERATURE_UNIT env var flips both API values
  // and the display label together.
  const TEMP_UNIT = document.body.dataset.tempUnitSymbol || "°C";
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

  const state = { range: "7d", plots: [], events: [], dailyEvents: [] };
  const sync = uPlot.sync("awair");

  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function hexToRgba(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function fmt(value, digits) {
    return value == null ? "–" : Number(value).toFixed(digits);
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
  function sunMoonMarkersPlugin() {
    return {
      hooks: {
        draw: (u) => {
          if (!state.dailyEvents.length) return;
          const ctx = u.ctx;
          const y = u.bbox.top + 10;
          ctx.save();
          ctx.beginPath();
          ctx.rect(u.bbox.left, u.bbox.top, u.bbox.width, u.bbox.height);
          ctx.clip();
          ctx.fillStyle = cssVar("--ink-muted") || "#898781";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.font = "10px system-ui, -apple-system, sans-serif";
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

  function makePlot(card, metric, series) {
    const meta = METRICS[metric];
    const color = cssVar(`--series-${metric}`);
    const plotEl = card.querySelector(".plot");
    plotEl.innerHTML = "";

    // Server stamps the alert ceiling on the card when the metric has one in
    // spikes.METRICS. Missing → no reference line for this chart.
    const ceilingRaw = card.dataset.ceiling;
    const ceiling = ceilingRaw ? Number(ceilingRaw) : null;
    const plugins = [eventWashPlugin(metric), sunMoonMarkersPlugin()];
    if (ceiling != null && Number.isFinite(ceiling)) {
      plugins.push(ceilingLinePlugin(ceiling));
    }

    const data = [series.t, series.min, series.max, series.avg];
    const axisStyle = {
      stroke: cssVar("--ink-muted"),
      grid: { stroke: cssVar("--grid"), width: 1 },
      ticks: { stroke: cssVar("--axis"), width: 1 },
      font: "11px system-ui, sans-serif",
    };

    const plot = new uPlot(
      {
        width: plotEl.clientWidth || 320,
        height: 150,
        cursor: { sync: { key: sync.key }, points: { size: 7 } },
        legend: { live: true },
        scales: { x: { time: true } },
        bands: [{ series: [2, 1], fill: hexToRgba(color, 0.14) }],
        series: [
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
        ],
        axes: [
          { ...axisStyle },
          { ...axisStyle, size: 52 },
        ],
        plugins,
      },
      data,
      plotEl
    );
    sync.sub(plot);
    state.plots.push(plot);

    // Header: name, unit, colored dot, latest value.
    card.querySelector(".name").textContent = meta.name;
    card.querySelector(".unit").textContent = meta.unit;
    card.querySelector(".dot").style.background = color;
    const latest = [...series.avg].reverse().find((v) => v != null);
    card.querySelector(".now").textContent =
      fmt(latest, meta.digits) + (meta.unit ? " " + meta.unit : "");
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
        const label = meta ? meta.name : "Device";
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
    const overlayPressure =
      metric === "precipitation" && allMetrics && allMetrics.pressure;
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
        plugins: [sunMoonMarkersPlugin()],
      },
      data,
      plotEl
    );
    sync.sub(plot);
    state.plots.push(plot);
    card.querySelector(".name").textContent = meta.name;
    card.querySelector(".unit").textContent = meta.unit;
    card.querySelector(".dot").style.background = color;
    const latest = [...series.avg].reverse().find((v) => v != null);
    let nowText = fmt(latest, meta.digits) + (meta.unit ? " " + meta.unit : "");
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
      makePlot(card, metric, seriesPayload.metrics[metric]);
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
