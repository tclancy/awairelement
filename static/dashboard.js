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
  };

  const state = { range: "7d", plots: [], events: [] };
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

  function makePlot(card, metric, series) {
    const meta = METRICS[metric];
    const color = cssVar(`--series-${metric}`);
    const plotEl = card.querySelector(".plot");
    plotEl.innerHTML = "";

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
        plugins: [eventWashPlugin(metric)],
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

  function makeOutdoorPlot(card, metric, series) {
    const meta = OUTDOOR_METRICS[metric];
    const color = cssVar(meta.colorVar);
    const plotEl = card.querySelector(".plot");
    plotEl.innerHTML = "";
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
        axes: [{ ...axisStyle }, { ...axisStyle, size: 52 }],
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
    card.querySelector(".now").textContent =
      fmt(latest, meta.digits) + (meta.unit ? " " + meta.unit : "");
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

    state.plots.forEach((p) => p.destroy());
    state.plots = [];
    for (const card of document.querySelectorAll(".card[data-metric]")) {
      const metric = card.dataset.metric;
      makePlot(card, metric, seriesPayload.metrics[metric]);
    }
    for (const card of document.querySelectorAll(".card[data-outdoor]")) {
      const metric = card.dataset.outdoor;
      makeOutdoorPlot(card, metric, outdoorPayload.metrics[metric]);
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
