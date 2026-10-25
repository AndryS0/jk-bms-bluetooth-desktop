"use strict";

const RANGES = [["1 h", 3600], ["6 h", 21600], ["24 h", 86400], ["3 d", 259200], ["7 d", 604800], ["30 d", 2592000]];
const STATUS_MS = 5000;
const HISTORY_MS = 30000;
const TABLE_ROWS = 300;
const LANE_BLOCK = 0.62; // height of an "on" block, as a share of its lane

const app = {
  range: 86400, from: 0, to: 0, zoom: null,
  status: null, history: null,
  charts: [], signature: "", loading: false,
};

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- formatting

function fmt(v, digits) {
  return v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(digits);
}

function fmtTime(ts, seconds = false) {
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  });
}

function fmtClock(ts, seconds = false) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}),
  });
}

function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${String(m % 60).padStart(2, "0")} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

const tempLabel = (name) => (name === "MOS" ? "MOSFETs" : name);

const GLYPH = { good: "✓", info: "i", warning: "!", serious: "!", critical: "✕" };

function statusIcon(level) {
  const icon = el("span", `status-icon ${level}`, GLYPH[level] || "•");
  icon.setAttribute("aria-hidden", "true");
  return icon;
}

function lineKey(color) {
  const key = el("span", "line-key");
  key.style.setProperty("--key", color);
  return key;
}

// ---------------------------------------------------------------- theme

function theme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    font: v("--font"),
    surface: v("--surface-1"),
    secondary: v("--text-secondary"),
    muted: v("--text-muted"),
    grid: v("--grid"),
    baseline: v("--baseline"),
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--series-${i}`)),
  };
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// ---------------------------------------------------------------- chart specs

// One measure per chart, on one axis. Colors follow the entity: cell 1 is
// always slot 1, charging is always slot 1 and discharging slot 2, and so on.
const CHARTS = [
  {
    key: "voltage", title: "Pack voltage", unit: "V", digits: 2, minSpan: 0.1,
    series: (h) => [{ label: "Pack voltage", values: h.voltage, slot: 1 }],
  },
  {
    key: "current", title: "Current", unit: "A", digits: 1, minSpan: 2, withZero: true, polarity: true,
    note: "Above zero the UPS is charging the battery, below zero the battery is powering the UPS",
    series: (h) => [{ label: "Current", values: h.current, slot: 1 }],
  },
  {
    key: "cells", title: "Cell voltages", unit: "V", digits: 3, minSpan: 0.02,
    series: (h) => h.cells.map((values, i) => ({ label: `Cell ${i + 1}`, values, slot: i + 1 })),
    refs: (t) => [
      [t.cell_ovp_v, `Charge cutoff ${fmt(t.cell_ovp_v, 2)} V`],
      [t.balance_start_v, `Balancer works above ${fmt(t.balance_start_v, 2)} V`],
      [t.cell_uvp_v, `Low-voltage cutoff ${fmt(t.cell_uvp_v, 2)} V`],
    ],
  },
  {
    key: "delta", title: "Cell difference", unit: "mV", digits: 0, minSpan: 5, fromZero: true,
    note: "Highest cell minus lowest cell",
    series: (h) => [{ label: "Difference", values: h.delta_mv, slot: 1 }],
    refs: (t) => [[t.balance_trigger_mv, `Balancer starts at ${fmt(t.balance_trigger_mv, 0)} mV`]],
    refsInRange: true,
  },
  {
    key: "soc", title: "State of charge", unit: "%", digits: 0, fixed: [0, 100],
    series: (h) => [{ label: "State of charge", values: h.soc, slot: 1 }],
  },
  {
    key: "temps", title: "Temperatures", unit: "°C", digits: 1, minSpan: 4,
    // MOS2 repeats the MOSFET sensor on this BMS; it stays in the CSV export
    series: (h) => Object.keys(h.temps)
      .filter((name) => name === "MOS" || !name.startsWith("MOS"))
      .map((name, i) => ({ label: tempLabel(name), values: h.temps[name], slot: i + 1 })),
  },
  {
    key: "switches", title: "Switches", lanes: true, note: "A filled band means the switch was on",
    series: (h) => [
      { label: "Charging", values: h.chg, slot: 1 },
      { label: "Discharging", values: h.dsg, slot: 2 },
      { label: "Balancer", values: h.balancing, slot: 3 },
    ],
  },
];

const laneBase = (i, n) => n - 1 - i + (1 - LANE_BLOCK) / 2;

function chartData(spec, defs, ts) {
  if (!spec.lanes) return [ts, ...defs.map((d) => d.values)];
  return [ts, ...defs.map((d, i) => d.values.map((v) => (v == null ? null : laneBase(i, defs.length) + (v ? LANE_BLOCK : 0))))];
}

function valueText(spec, v) {
  if (v == null) return "–";
  if (spec.lanes) return v ? "On" : "Off";
  return `${fmt(v, spec.digits)} ${spec.unit}`;
}

function currentLabel(v) {
  const act = app.status?.active_current_a ?? 0.25;
  return v >= act ? "Charging" : v <= -act ? "Discharging" : "Idle";
}

// Charging is slot 1, discharging slot 2, idle the muted ink.
function currentColor(c, v) {
  const label = v == null ? "Idle" : currentLabel(v);
  return label === "Charging" ? c.series[0] : label === "Discharging" ? c.series[1] : c.muted;
}

function yRange(spec, defs) {
  if (spec.lanes) return { range: [0, defs.length] };
  if (spec.fixed) return { range: spec.fixed };
  return {
    range: (u, min, max) => {
      if (min == null || max == null) return [0, 1];
      if (spec.withZero) { min = Math.min(min, 0); max = Math.max(max, 0); }
      if (spec.fromZero) min = 0;
      if (spec.refsInRange) {
        for (const [value] of spec.refs(app.status?.thresholds || {})) {
          if (value != null) max = Math.max(max, value * 1.2);
        }
      }
      if (max - min < spec.minSpan) {
        const mid = (min + max) / 2;
        [min, max] = spec.fromZero ? [0, spec.minSpan] : [mid - spec.minSpan / 2, mid + spec.minSpan / 2];
      }
      const pad = (max - min) * 0.08;
      return [spec.fromZero ? 0 : min - pad, max + pad];
    },
  };
}

function axisDigits(u) {
  const span = u.scales.y.max - u.scales.y.min;
  return span < 0.05 ? 3 : span < 0.5 ? 2 : span < 5 ? 1 : 0;
}

// One series, two poles: blue above the zero line, orange below it, and a
// neutral band on the line itself so an idle 0 A doesn't read as either.
function polarityPaint(u, pos, neutral, neg) {
  const { top, height } = u.bbox;
  if (!height) return neutral;
  const clamp = (f) => Math.min(1, Math.max(0, f));
  const zero = (u.valToPos(0, "y", true) - top) / height;
  const band = (1.5 * (window.devicePixelRatio || 1)) / height;
  const g = u.ctx.createLinearGradient(0, top, 0, top + height);
  g.addColorStop(0, pos);
  g.addColorStop(clamp(zero - band), pos);
  g.addColorStop(clamp(zero - band), neutral);
  g.addColorStop(clamp(zero + band), neutral);
  g.addColorStop(clamp(zero + band), neg);
  g.addColorStop(1, neg);
  return g;
}

// Tick labels in the browser's own date/time format; the date is added under
// the first tick of each day.
function timeTicks(u, splits, axisIdx, space, incr) {
  const day = (d) => d.toLocaleDateString([], { month: "short", day: "numeric" });
  return splits.map((ts, i) => {
    const d = new Date(ts * 1000);
    if (incr >= 86400) return day(d);
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const prev = i > 0 ? new Date(splits[i - 1] * 1000) : null;
    return !prev || prev.toDateString() !== d.toDateString() ? `${time}\n${day(d)}` : time;
  });
}

// Threshold lines from config.toml, drawn only when inside the visible range.
function drawRefs(u, spec, c) {
  if (!spec.refs) return;
  const { left, top, width } = u.bbox;
  const dpr = window.devicePixelRatio || 1;
  const ctx = u.ctx;
  ctx.save();
  ctx.strokeStyle = c.baseline;
  ctx.lineWidth = dpr;
  ctx.setLineDash([4 * dpr, 4 * dpr]);
  ctx.fillStyle = c.muted;
  ctx.font = `${Math.round(11 * dpr)}px ${c.font}`;
  ctx.textAlign = "right";
  for (const [value, label] of spec.refs(app.status?.thresholds || {})) {
    if (value == null || value < u.scales.y.min || value > u.scales.y.max) continue;
    const y = Math.round(u.valToPos(value, "y", true)) + 0.5;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + width, y);
    ctx.stroke();
    const above = y - 16 * dpr > top;
    ctx.textBaseline = above ? "bottom" : "top";
    ctx.fillText(label, left + width - 6 * dpr, above ? y - 3 * dpr : y + 3 * dpr);
  }
  ctx.restore();
}

// ---------------------------------------------------------------- tooltip & zoom

function tooltipPlugin(spec, chart) {
  let tip = null;
  let hovering = false;
  return {
    hooks: {
      init: (u) => {
        tip = el("div", "tip");
        tip.hidden = true;
        u.over.appendChild(tip);
        u.over.addEventListener("mouseenter", () => { hovering = true; });
        u.over.addEventListener("mouseleave", () => { hovering = false; tip.hidden = true; });
      },
      setCursor: (u) => {
        const i = u.cursor.idx;
        if (!hovering || i == null) { tip.hidden = true; return; }
        tip.replaceChildren(el("div", "tip-time", fmtTime(u.data[0][i], true)));
        for (const d of chart.defs) {
          const v = d.values[i];
          const row = el("div", "tip-row");
          row.append(
            lineKey(spec.polarity ? currentColor(chart.colors, v) : chart.colors.series[d.slot - 1]),
            el("strong", null, valueText(spec, v)),
            el("span", null, spec.polarity && v != null ? currentLabel(v) : d.label),
          );
          tip.append(row);
        }
        tip.hidden = false;
        const W = u.over.clientWidth;
        const H = u.over.clientHeight;
        let x = u.cursor.left + 16;
        if (x + tip.offsetWidth > W) x = u.cursor.left - tip.offsetWidth - 16;
        const y = Math.max(0, Math.min((u.cursor.top ?? 0) - tip.offsetHeight / 2, H - tip.offsetHeight));
        tip.style.transform = `translate(${Math.max(0, x)}px, ${y}px)`;
      },
    },
  };
}

function onSelect(u) {
  const { left, width } = u.select;
  if (width > 4) setZoom([u.posToVal(left, "x"), u.posToVal(left + width, "x")]);
  u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
}

function setZoom(win) {
  app.zoom = win;
  $("reset-zoom").hidden = !win;
  $("range-hint").textContent = win ? "Double-click a graph or press Reset zoom to see the whole range" : "Drag across a graph to zoom in";
  applyWindow();
  updateCsvLink();
  for (const chart of app.charts) if (chart.tableOpen) renderChartTable(chart);
}

function applyWindow() {
  const [min, max] = app.zoom || [app.from, app.to];
  for (const chart of app.charts) chart.u.setScale("x", { min, max });
}

// ---------------------------------------------------------------- charts

function buildOpts(spec, chart, width) {
  const c = chart.colors;
  const defs = chart.defs;
  const font = `12px ${c.font}`;
  const series = [{}];
  defs.forEach((d, i) => {
    const color = c.series[d.slot - 1];
    const s = { label: d.label, stroke: color, width: 2, spanGaps: false, points: { show: false } };
    if (spec.polarity) {
      const neg = c.series[1];
      s.stroke = (u) => polarityPaint(u, color, c.muted, neg);
      s.fill = (u) => polarityPaint(u, withAlpha(color, 0.1), "rgba(0, 0, 0, 0)", withAlpha(neg, 0.1));
      s.fillTo = 0;
    }
    if (spec.lanes) {
      s.paths = uPlot.paths.stepped({ align: 1 });
      s.fill = withAlpha(color, 0.3);
      s.fillTo = laneBase(i, defs.length);
    }
    series.push(s);
  });

  const pointColor = (u, si) => {
    const d = chart.defs[si - 1];
    return spec.polarity ? currentColor(c, d.values[u.cursor.idx]) : c.series[d.slot - 1];
  };

  const yAxis = spec.lanes
    ? {
      stroke: c.secondary, font, size: 96, gap: 8, grid: { show: false }, ticks: { show: false },
      splits: () => defs.map((_, i) => laneBase(i, defs.length) + LANE_BLOCK / 2),
      values: () => defs.map((d) => d.label),
    }
    : {
      stroke: c.muted, font, size: 64, gap: 6, grid: { stroke: c.grid, width: 1 }, ticks: { show: false },
      values: (u, splits) => splits.map((v) => fmt(v, axisDigits(u))),
    };

  return {
    width,
    height: spec.lanes ? 52 + 30 * defs.length : 220,
    padding: [12, 16, 0, 0],
    legend: { show: false },
    series,
    scales: { x: { time: true }, y: yRange(spec, defs) },
    axes: [
      {
        stroke: c.muted, font, space: 80, values: timeTicks,
        grid: { stroke: c.grid, width: 1 }, ticks: { stroke: c.grid, width: 1, size: 4 },
      },
      yAxis,
    ],
    cursor: {
      y: false,
      sync: { key: "bms" },
      drag: { x: true, y: false, setScale: false },
      bind: { dblclick: () => (e) => { if (e.button === 0) setZoom(null); return null; } },
      points: spec.lanes ? { show: false } : { size: 10, width: 2, stroke: () => c.surface, fill: pointColor },
    },
    hooks: {
      setSelect: [onSelect],
      draw: [(u) => drawRefs(u, spec, c)],
    },
    plugins: [tooltipPlugin(spec, chart)],
  };
}

function chartCard(spec, chart) {
  const c = chart.colors;
  const card = el("article", "card chart-card");
  const head = el("header", "chart-head");
  const titles = el("div");
  titles.append(el("h2", null, spec.lanes ? spec.title : `${spec.title} (${spec.unit})`));
  if (spec.note) titles.append(el("p", "muted small", spec.note));

  const tools = el("div", "chart-tools");
  // lanes carry their names on the axis, so they need no legend
  const keys = spec.polarity
    ? [["Charging", c.series[0]], ["Idle", c.muted], ["Discharging", c.series[1]]]
    : chart.defs.length > 1 && !spec.lanes ? chart.defs.map((d) => [d.label, c.series[d.slot - 1]]) : [];
  if (keys.length) {
    const legend = el("ul", "legend");
    for (const [label, color] of keys) {
      const li = el("li");
      li.append(lineKey(color), document.createTextNode(label));
      legend.append(li);
    }
    tools.append(legend);
  }

  const wrap = el("div", "table-wrap");
  wrap.hidden = true;
  const toggle = el("button", "btn ghost small", "Table");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  toggle.addEventListener("click", () => {
    chart.tableOpen = !chart.tableOpen;
    toggle.setAttribute("aria-expanded", String(chart.tableOpen));
    wrap.hidden = !chart.tableOpen;
    if (chart.tableOpen) renderChartTable(chart);
  });
  tools.append(toggle);
  head.append(titles, tools);

  const plot = el("div", "plot");
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", `${spec.title} over time. The Table button lists the values.`);
  card.append(head, plot, wrap);
  return card;
}

function renderChartTable(chart) {
  const { spec, defs } = chart;
  const h = app.history;
  const ts = h.series.ts;
  const [lo, hi] = app.zoom || [h.from, h.to];
  const head = el("tr");
  head.append(el("th", null, "Time"));
  for (const d of defs) head.append(el("th", "num", spec.lanes ? d.label : `${d.label} (${spec.unit})`));
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  let shown = 0;
  for (let i = ts.length - 1; i >= 0 && shown < TABLE_ROWS; i--) {
    if (ts[i] < lo || ts[i] > hi || defs.every((d) => d.values[i] == null)) continue;
    const tr = el("tr");
    tr.append(el("td", "nowrap", fmtTime(ts[i], true)));
    for (const d of defs) tr.append(el("td", "num", spec.lanes ? valueText(spec, d.values[i]) : fmt(d.values[i], spec.digits)));
    tbody.append(tr);
    shown++;
  }
  const table = el("table");
  table.append(thead, tbody);
  const note = h.bucket_seconds
    ? `Averages over about ${fmtDuration(h.bucket_seconds)} each. Newest first, up to ${TABLE_ROWS} rows.`
    : `Newest first, up to ${TABLE_ROWS} rows.`;
  chart.card.querySelector(".table-wrap").replaceChildren(el("p", "muted small", note), table);
}

function buildCharts(h) {
  for (const chart of app.charts) chart.u.destroy();
  app.charts = [];
  const box = $("charts");
  box.replaceChildren();
  const colors = theme();
  for (const spec of CHARTS) {
    const defs = spec.series(h.series);
    if (!defs.length) continue;
    const chart = { spec, defs, colors, tableOpen: false };
    chart.card = chartCard(spec, chart);
    box.append(chart.card);
    chart.plot = chart.card.querySelector(".plot");
    chart.u = new uPlot(buildOpts(spec, chart, chart.plot.clientWidth || 600), chartData(spec, defs, h.series.ts), chart.plot);
    app.charts.push(chart);
  }
  applyWindow();
}

function renderHistory(h) {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const signature = `${h.series.cells.length}|${Object.keys(h.series.temps).join(",")}|${dark}`;
  if (signature !== app.signature || !app.charts.length) {
    app.signature = signature;
    buildCharts(h);
  } else {
    for (const chart of app.charts) {
      chart.defs = chart.spec.series(h.series);
      chart.u.setData(chartData(chart.spec, chart.defs, h.series.ts), false);
      if (chart.tableOpen) renderChartTable(chart);
    }
    applyWindow();
  }
  renderSessions(h.sessions);
  renderEvents(h.events);
  const notice = $("notice");
  notice.hidden = h.count > 0;
  notice.textContent = "No readings stored in this range yet. The logger records whenever this PC is on.";
}

async function loadHistory() {
  if (app.loading) return;
  app.loading = true;
  $("charts").classList.add("is-loading"); // keep the old frame, dimmed, while fetching
  try {
    const to = Math.ceil(Date.now() / 1000);
    const from = to - app.range;
    const points = Math.round(Math.min(3000, Math.max(400, ($("charts").clientWidth || 1000) * 1.5)));
    const res = await fetch(`api/history?from=${from}&to=${to}&points=${points}`);
    if (!res.ok) throw new Error(res.statusText);
    const h = await res.json();
    app.history = h;
    app.from = h.from;
    app.to = h.to;
    renderHistory(h);
    updateCsvLink();
  } catch {
    // keep the previous render; the status pill already says the logger is down
  } finally {
    app.loading = false;
    $("charts").classList.remove("is-loading");
  }
}

// ---------------------------------------------------------------- tiles & tables

function tile(label, value, sub, status) {
  const t = el("div", "tile card");
  t.append(el("div", "tile-label", label));
  const v = el("div", "tile-value");
  if (status) v.append(statusIcon(status));
  v.append(document.createTextNode(value));
  t.append(v);
  if (sub) t.append(el("div", "tile-sub", sub));
  return t;
}

function cellsTile(s, colors) {
  const t = el("div", "tile card cells-tile");
  t.append(el("div", "tile-label", "Cells"));
  const grid = el("div", "cells-grid");
  const hi = Math.max(...s.cells);
  const lo = Math.min(...s.cells);
  s.cells.forEach((v, i) => {
    const item = el("div", "cell-item");
    const tag = hi === lo ? "" : v === hi ? "highest" : v === lo ? "lowest" : "";
    item.append(lineKey(colors.series[i % 8]), `Cell ${i + 1}`, el("strong", null, `${fmt(v, 3)} V`), el("span", "tag", tag));
    grid.append(item);
  });
  t.append(grid);
  return t;
}

function renderTiles(st) {
  const box = $("tiles");
  const s = st.sample;
  if (!s) {
    box.replaceChildren(tile("Latest reading", "Waiting", "Readings appear a few seconds after the BMS connects"));
    return;
  }
  const t = st.thresholds || {};
  const amps = s.current ?? 0;
  const temps = Object.entries(s.temps || {}).filter(([name]) => name === "MOS" || !name.startsWith("MOS"));
  const hottest = temps.length ? Math.max(...temps.map(([, v]) => v)) : null;
  const expected = new Set(t.expected_alarms || []);
  const names = s.alarm_names || [];
  const unexpected = names.filter((n) => !expected.has(n));
  const normalStop = !s.chg && names.length > 0 && unexpected.length === 0;

  box.replaceChildren(
    tile("Pack voltage", `${fmt(s.voltage, 2)} V`, `${fmt(s.voltage / (s.cells.length || 1), 3)} V per cell on average`),
    tile("Current", `${fmt(amps, 1)} A`, `${currentLabel(amps)} · ${fmt(Math.abs(s.power ?? 0), 0)} W`),
    tile("State of charge", `${fmt(s.soc, 0)} %`, `${fmt(s.remaining_ah, 1)} Ah left`),
    tile("Cell difference", `${fmt(s.delta_mv, 0)} mV`, `${fmt(s.cell_min, 3)} – ${fmt(s.cell_max, 3)} V`),
    tile("Hottest sensor", `${fmt(hottest, 1)} °C`, temps.map(([n, v]) => `${tempLabel(n)} ${fmt(v, 1)}`).join(" · ")),
    tile(
      "Charging switch", s.chg ? "On" : "Off",
      s.chg ? "The UPS can charge the battery"
        : normalStop ? `Stopped at the ${fmt(t.cell_ovp_v, 2)} V cutoff, which is normal` : "The BMS is blocking charging",
      s.chg ? "good" : normalStop ? "info" : "warning",
    ),
    tile(
      "Discharging switch", s.dsg ? "On" : "Off",
      s.dsg ? "The battery can power the UPS" : "The battery can't power the UPS",
      s.dsg ? "good" : "critical",
    ),
    tile(
      "Alarms", unexpected.length ? unexpected.join(", ") : "None",
      names.filter((n) => expected.has(n)).map((n) => `${n} is on (expected)`).join(", ") || "Nothing reported",
      unexpected.length ? "critical" : "good",
    ),
    cellsTile(s, theme()),
  );
}

function headerRow(columns) {
  const tr = el("tr");
  for (const [label, numeric] of columns) tr.append(el("th", numeric ? "num" : null, label));
  const thead = el("thead");
  thead.append(tr);
  return thead;
}

function emptyRow(text, span) {
  const tr = el("tr");
  const td = el("td", "muted", text);
  td.colSpan = span;
  tr.append(td);
  return tr;
}

function renderSessions(sessions) {
  const columns = [["Type"], ["Started"], ["Duration", 1], ["Amount", 1], ["Peak current", 1],
    ["State of charge", 1], ["Pack voltage", 1], ["Extreme cell", 1]];
  const tbody = el("tbody");
  if (!sessions.length) tbody.append(emptyRow("No charging or discharging in this range.", columns.length));
  const c = theme();
  for (const s of sessions.slice().reverse()) {
    const charge = s.kind === "charge";
    const type = el("td", "nowrap");
    type.append(lineKey(c.series[charge ? 0 : 1]), document.createTextNode((charge ? "Charging" : "Discharging") + (s.open ? " (ongoing)" : "")));
    const tr = el("tr");
    tr.append(
      type,
      el("td", "nowrap", fmtTime(s.start)),
      el("td", "num", fmtDuration(s.end - s.start)),
      el("td", "num", `${fmt(s.ah, 1)} Ah`),
      el("td", "num", `${fmt(s.peak_a, 1)} A`),
      el("td", "num", `${fmt(s.soc_start, 0)} → ${fmt(s.soc_end, 0)} %`),
      el("td", "num", `${fmt(s.v_start, 2)} → ${fmt(s.v_end, 2)} V`),
      el("td", "num", charge ? `highest ${fmt(s.cell_max, 3)} V` : `lowest ${fmt(s.cell_min, 3)} V`),
    );
    tbody.append(tr);
  }
  $("sessions").replaceChildren(headerRow(columns), tbody);
}

function renderEvents(events) {
  const tbody = el("tbody");
  const list = events.slice(-200).reverse();
  if (!list.length) tbody.append(emptyRow("Nothing happened in this range.", 2));
  for (const e of list) {
    const tr = el("tr", e.level === "muted" ? "event-muted" : null);
    const text = el("td");
    if (e.level !== "muted") text.append(statusIcon(e.level));
    text.append(document.createTextNode(e.text));
    tr.append(el("td", "nowrap", fmtTime(e.ts, true)), text);
    tbody.append(tr);
  }
  $("events").replaceChildren(headerRow([["Time"], ["Event"]]), tbody);
}

// ---------------------------------------------------------------- status

const STATUS = {
  starting: ["warning", "Starting"],
  searching: ["warning", "Looking for the BMS"],
  connecting: ["warning", "Connecting"],
  reconnecting: ["warning", "Reconnecting"],
  logging: ["good", "Logging"],
  paused: ["info", "Paused"],
  "not found": ["serious", "BMS not found"],
  error: ["critical", "Bluetooth error"],
};

function setPill(level, text) {
  const icon = $("conn-pill").querySelector(".status-icon");
  icon.className = `status-icon ${level}`;
  icon.textContent = GLYPH[level];
  $("conn-text").textContent = text;
}

function renderStatus(st) {
  const paused = st.pause_until > st.now;
  let [level, text] = STATUS[st.status] || ["warning", st.status];
  let detail = st.detail || "";
  if (paused) {
    [level, text] = ["info", `Paused until ${fmtClock(st.pause_until)}`];
    detail = "The JK phone app can connect now";
  } else if (st.status === "logging" && st.sample) {
    const age = st.now - st.sample.ts;
    if (age > 3 * st.poll_seconds + 30) {
      [level, text] = ["warning", "No fresh readings"];
      detail = `last one ${fmtDuration(age)} ago`;
    } else {
      detail = `updated ${fmtClock(st.sample.ts, true)}`;
    }
  }
  setPill(level, text);
  $("conn-detail").textContent = detail;
  $("pause-btn").hidden = paused;
  $("resume-btn").hidden = !paused;

  const d = st.device || {};
  if (d.model) {
    $("device").textContent = [d.model, d.hw_version && `HW ${d.hw_version}`, d.sw_version && `SW ${d.sw_version}`, d.name]
      .filter(Boolean).join(" · ");
  }
  $("active-a").textContent = fmt(st.active_current_a, 2);
  const db = st.db || {};
  $("footer").textContent = [
    db.first_ts ? `Recording since ${fmtTime(db.first_ts)}` : "Nothing recorded yet",
    `database ${fmt(db.size_mb, 1)} MB`,
    `reading every ${st.poll_seconds} s`,
  ].join(" · ");
}

async function loadStatus() {
  let st;
  try {
    const res = await fetch("api/status");
    if (!res.ok) throw new Error(res.statusText);
    st = await res.json();
  } catch {
    setPill("critical", "Logger not running");
    $("conn-detail").textContent = "Start it with the JK BMS logger shortcut, then reload this page";
    return;
  }
  app.status = st;
  renderStatus(st);
  renderTiles(st);
  document.dispatchEvent(new CustomEvent("bms-status"));
}

async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-JKBMS": "1" },
      body: JSON.stringify(body || {}),
    });
    if (res.ok) {
      app.status = await res.json();
      renderStatus(app.status);
      document.dispatchEvent(new CustomEvent("bms-status"));
    }
  } catch {
    setPill("critical", "Logger not running");
  }
}

function updateCsvLink() {
  const [lo, hi] = app.zoom || [app.from, app.to];
  $("csv-link").href = `api/export.csv?from=${Math.floor(lo)}&to=${Math.ceil(hi)}`;
}

// ---------------------------------------------------------------- start

function renderRanges() {
  const box = $("ranges");
  for (const [label, secs] of RANGES) {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(secs === app.range));
    b.addEventListener("click", () => {
      app.range = secs;
      for (const other of box.children) other.setAttribute("aria-pressed", String(other === b));
      try { localStorage.setItem("jkbms.range", String(secs)); } catch { /* private mode */ }
      app.zoom = null;
      $("reset-zoom").hidden = true;
      loadHistory();
    });
    box.append(b);
  }
}

async function init() {
  try {
    const saved = Number(localStorage.getItem("jkbms.range"));
    if (RANGES.some(([, s]) => s === saved)) app.range = saved;
  } catch { /* storage blocked: keep the default */ }
  renderRanges();
  $("reset-zoom").addEventListener("click", () => setZoom(null));
  $("pause-btn").addEventListener("click", () => post("api/pause", { minutes: 10 }));
  $("resume-btn").addEventListener("click", () => post("api/resume"));

  let resizeTimer = 0;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      for (const chart of app.charts) {
        const w = chart.plot.clientWidth;
        if (w && w !== chart.u.width) chart.u.setSize({ width: w, height: chart.u.height });
      }
    }, 120);
  }).observe($("charts"));

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (app.history) renderHistory(app.history);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadStatus(); loadHistory(); }
  });

  await loadStatus();
  await loadHistory();
  setInterval(() => { if (!document.hidden) loadStatus(); }, STATUS_MS);
  setInterval(() => { if (!document.hidden) loadHistory(); }, HISTORY_MS);
}

init();
