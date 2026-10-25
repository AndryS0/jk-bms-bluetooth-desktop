"use strict";

// "BMS settings" tab. Values come from the settings frame the logger captured;
// edits are staged here, checked against the same limits and rules the logger
// enforces, and written by the logger, which reads every setting back after
// each write. Uses the helpers and `app` state from app.js.

const settingsUi = { data: null, meta: {}, staged: {}, writing: false };

function settingText(m, v) {
  if (v == null) return "–";
  if (m.switch) return v ? "On" : "Off";
  return `${fmt(v, m.digits)}${m.unit ? ` ${m.unit}` : ""}`;
}

function liveState() {
  const st = app.status;
  if (!st) return { ok: false, why: "Waiting for the logger." };
  if (st.pause_until > st.now) return { ok: false, why: "Logging is paused for the phone app. Resume it to read or change settings here." };
  if (st.status !== "logging") return { ok: false, why: "Not connected to the BMS right now. Settings can be changed once it reconnects." };
  return { ok: true, why: "" };
}

function showSettingsNotice(text) {
  $("settings-notice").hidden = !text;
  $("settings-notice").textContent = text;
}

async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-JKBMS": "1" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  return { ok: res.ok, data };
}

function useSettings(data) {
  settingsUi.data = data;
  settingsUi.meta = Object.fromEntries(data.settings.map((m) => [m.key, m]));
  settingsUi.staged = {};
  renderSettings();
}

async function loadSettings(fromBms = false) {
  try {
    if (fromBms) {
      $("settings-reload").disabled = true;
      const r = await postJson("api/settings/read");
      if (!r.ok) {
        showSettingsNotice(r.data?.error || "Couldn't read the settings from the BMS.");
        return;
      }
      useSettings(r.data);
    } else {
      useSettings(await (await fetch("api/settings")).json());
    }
  } catch {
    showSettingsNotice("Couldn't reach the logger.");
  } finally {
    updateAvailability();
  }
}

// ---------------------------------------------------------------- rendering

function settingRow(m, value) {
  const row = el("div", "setting-row");
  row.dataset.key = m.key;
  const id = `set-${m.key}`;

  const name = el("div", "setting-name");
  const label = el(m.editable ? "label" : "span", "name", m.label);
  if (m.editable) label.htmlFor = id;
  const range = m.editable && !m.switch ? ` · allowed ${fmt(m.min, m.digits)}–${fmt(m.max, m.digits)} ${m.unit}` : "";
  name.append(label, el("p", "muted small", `JK app: ${m.jk_name}${range}`));
  if (m.help) name.append(el("p", "muted small", m.help));

  const current = el("div", "setting-current", settingText(m, value));
  current.title = "Value in the BMS now";

  const input = el("div", "setting-input");
  if (!m.editable) {
    input.append(el("span", "muted small", m.locked === "config" ? "read-only: no range in config.toml" : "read-only"));
  } else if (m.switch) {
    const box = el("input", "toggle");
    box.type = "checkbox";
    box.id = id;
    box.checked = Boolean(value);
    box.setAttribute("role", "switch");
    const state = el("span", "toggle-label", value ? "On" : "Off");
    box.addEventListener("change", () => {
      state.textContent = box.checked ? "On" : "Off";
      stage(m, box.checked);
    });
    input.append(box, state);
  } else {
    const num = el("input");
    Object.assign(num, { type: "number", id, step: String(m.step), min: String(m.min), max: String(m.max) });
    num.inputMode = "decimal";
    num.value = fmt(value, m.digits);
    num.addEventListener("input", () => stage(m, num.value.trim() === "" ? NaN : Number(num.value)));
    input.append(num, el("span", "unit", m.unit));
  }
  row.append(name, current, input);
  return row;
}

function flagsBlock(flags) {
  const details = el("details");
  details.append(el("summary", null, "Other options in the BMS (read-only)"));
  const list = el("ul", "flag-list");
  for (const [name, on] of Object.entries(flags)) list.append(el("li", null, `${name}: ${on ? "on" : "off"}`));
  details.append(list);
  return details;
}

function renderSettings() {
  const d = settingsUi.data;
  $("settings-read").textContent = d.read_at ? `Read from the BMS at ${fmtTime(d.read_at, true)}` : "Not read yet";
  const problems = d.config_problems || [];
  $("settings-config").hidden = !problems.length;
  $("settings-config").replaceChildren(
    ...(problems.length ? [el("strong", null, "Problems in config.toml"), ...problems.map((p) => el("div", null, p))] : []),
  );
  const box = $("settings-groups");
  box.replaceChildren();
  if (d.values) {
    for (const group of d.groups) {
      const metas = d.settings.filter((m) => m.group === group.key);
      if (!metas.length) continue;
      const card = el("section", "card block settings-group");
      card.append(el("h2", null, group.label));
      for (const m of metas) card.append(settingRow(m, d.values[m.key]));
      if (group.key === "battery" && d.flags) card.append(flagsBlock(d.flags));
      box.append(card);
    }
  } else {
    box.append(el("p", "notice", "The settings appear once the logger has connected to the BMS."));
  }
  renderSettingsHistory(d.history || []);
  updateChangesBar();
}

function renderSettingsHistory(history) {
  const tbody = el("tbody");
  if (!history.length) tbody.append(emptyRow("No changes made from this page yet.", 4));
  for (const h of history) {
    const m = settingsUi.meta[h.key] || { label: h.key, digits: 3, unit: "" };
    const result = el("td");
    result.append(
      statusIcon(h.ok ? "good" : "critical"),
      h.ok ? "Confirmed" : `Not applied, the BMS kept ${settingText(m, h.read_back)}${h.note ? ` (${h.note})` : ""}`,
    );
    const tr = el("tr");
    tr.append(
      el("td", "nowrap", fmtTime(h.ts, true)),
      el("td", null, m.label),
      el("td", "nowrap", `${settingText(m, h.old_value)} → ${settingText(m, h.new_value)}`),
      result,
    );
    tbody.append(tr);
  }
  $("settings-history").replaceChildren(headerRow([["Time"], ["Setting"], ["Change"], ["Result"]]), tbody);
}

// ---------------------------------------------------------------- staging & checks

function stage(m, v) {
  const cur = settingsUi.data.values[m.key];
  const step = 10 ** -m.digits;
  const same = m.switch ? v === Boolean(cur) : Number.isFinite(v) && Math.abs(v - cur) < step / 2;
  if (same) delete settingsUi.staged[m.key];
  else settingsUi.staged[m.key] = m.switch || !Number.isFinite(v) ? v : Math.round(v / step) * step;
  updateChangesBar();
}

// Same limits and cross-checks as the logger (which repeats them before writing).
function checkStaged() {
  const { data, staged, meta } = settingsUi;
  const fields = {};
  const general = [];
  for (const [key, v] of Object.entries(staged)) {
    const m = meta[key];
    if (m.switch) continue;
    if (!Number.isFinite(v)) fields[key] = "Enter a number";
    else if (v < m.min - 1e-9 || v > m.max + 1e-9) {
      fields[key] = `Allowed ${fmt(m.min, m.digits)}–${fmt(m.max, m.digits)} ${m.unit}`;
    }
  }
  if (!Object.keys(fields).length) {
    const merged = { ...data.values, ...staged };
    for (const r of data.rules) {
      if ((r.a in staged || r.b in staged) && merged[r.a] > merged[r.b] + r.offset + 1e-9) general.push(r.message);
    }
  }
  return { fields, general };
}

function updateChangesBar() {
  if (!settingsUi.data) return;
  const keys = Object.keys(settingsUi.staged);
  const { fields, general } = checkStaged();
  for (const row of document.querySelectorAll("#settings-groups .setting-row")) {
    const key = row.dataset.key;
    row.classList.toggle("changed", key in settingsUi.staged);
    row.classList.toggle("invalid", key in fields);
    row.querySelector(".field-error")?.remove();
    if (fields[key]) row.append(el("div", "field-error", fields[key]));
  }
  $("changes-bar").hidden = keys.length === 0;
  $("changes-count").textContent = keys.length === 1 ? "1 unsaved change" : `${keys.length} unsaved changes`;
  $("changes-errors").textContent = general.join(" · ");
  $("changes-review").disabled = !keys.length || Object.keys(fields).length > 0 || general.length > 0 || !liveState().ok;
}

function updateAvailability() {
  const live = liveState();
  showSettingsNotice(live.ok ? "" : live.why);
  $("settings-reload").disabled = !live.ok;
  for (const input of document.querySelectorAll("#settings-groups input")) input.disabled = !live.ok;
  updateChangesBar();
}

// ---------------------------------------------------------------- review & write

function resultRow(level, text) {
  const row = el("div", "result-row");
  row.append(statusIcon(level), el("span", null, text));
  return row;
}

function openReview() {
  const { staged, meta, data } = settingsUi;
  const rows = [];
  const cautions = [];
  for (const [key, v] of Object.entries(staged)) {
    const m = meta[key];
    const tr = el("tr");
    tr.append(el("td", null, m.label), el("td", "num", settingText(m, data.values[key])), el("td", null, "→"),
      el("td", "num", settingText(m, v)));
    rows.push(tr);
    if (m.switch && v === false && m.caution) cautions.push(resultRow("critical", m.caution));
  }
  $("review-list").replaceChildren(...rows);
  $("review-cautions").replaceChildren(...cautions);
  $("review-results").hidden = true;
  const write = $("review-write");
  write.hidden = false;
  write.disabled = false;
  write.textContent = "Write to BMS";
  $("review-cancel").textContent = "Cancel";
  $("review").showModal();
}

function resultText(m, r) {
  if (r.status === "ok") return `${m.label}: now ${settingText(m, r.read_back)}, confirmed by reading it back`;
  if (r.status === "unchanged") return `${m.label}: already ${settingText(m, r.read_back)}`;
  if (r.status === "skipped") return `${m.label}: not written, because the change before it went wrong`;
  return `${m.label}: not applied, the BMS reports ${settingText(m, r.read_back)}${r.note ? ` (${r.note})` : ""}`;
}

async function writeStaged() {
  const write = $("review-write");
  const cancel = $("review-cancel");
  write.disabled = true;
  write.textContent = "Writing…";
  cancel.disabled = true;
  settingsUi.writing = true;
  let r;
  try {
    r = await postJson("api/settings", { changes: { ...settingsUi.staged } });
  } catch {
    r = { ok: false, data: { error: "Couldn't reach the logger." } };
  }
  settingsUi.writing = false;

  const d = r.data || {};
  const lines = [];
  if (d.error) lines.push(resultRow("critical", d.error));
  for (const e of d.errors || []) lines.push(resultRow("critical", e));
  for (const res of d.results || []) {
    const level = res.status === "ok" || res.status === "unchanged" ? "good" : res.status === "skipped" ? "info" : "critical";
    lines.push(resultRow(level, resultText(settingsUi.meta[res.key], res)));
  }
  $("review-results").replaceChildren(el("h3", null, d.ok ? "Done" : "Not everything was written"), ...lines);
  $("review-results").hidden = false;
  write.hidden = true;
  cancel.disabled = false;
  cancel.textContent = "Close";
  if (d.settings) useSettings(d.settings);
}

// ---------------------------------------------------------------- tabs & wiring

function showTab(name) {
  const settings = name === "settings";
  $("view-monitor").hidden = settings;
  $("view-settings").hidden = !settings;
  $("tab-monitor").setAttribute("aria-selected", String(!settings));
  $("tab-settings").setAttribute("aria-selected", String(settings));
  $("tab-monitor").tabIndex = settings ? -1 : 0;
  $("tab-settings").tabIndex = settings ? 0 : -1;
  history.replaceState(null, "", settings ? "#settings" : location.pathname);
  if (settings && !Object.keys(settingsUi.staged).length) loadSettings();
}

$("tab-monitor").addEventListener("click", () => showTab("monitor"));
$("tab-settings").addEventListener("click", () => showTab("settings"));
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const next = $("tab-monitor").getAttribute("aria-selected") === "true" ? "settings" : "monitor";
  showTab(next);
  $(`tab-${next}`).focus();
});
$("settings-reload").addEventListener("click", () => loadSettings(true));
$("changes-discard").addEventListener("click", () => useSettings(settingsUi.data));
$("changes-review").addEventListener("click", openReview);
$("review-write").addEventListener("click", writeStaged);
$("review-cancel").addEventListener("click", () => $("review").close());
$("review").addEventListener("cancel", (e) => { if (settingsUi.writing) e.preventDefault(); });
document.addEventListener("bms-status", () => { if (settingsUi.data) updateAvailability(); });

showTab(location.hash === "#settings" ? "settings" : "monitor");
