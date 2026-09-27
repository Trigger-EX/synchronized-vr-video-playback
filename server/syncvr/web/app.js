"use strict";

// ---------------------------------------------------------------- state
let state = null;          // latest snapshot from the server
let receivedAt = 0;        // performance.now() when it arrived
let ws = null;
let nextId = 1;
const selected = new Set();
let editingId = null;
let lastLibraryKey = "";
let seekDragging = false;
let videoTouched = false;   // until the operator picks a video, follow what's loaded
let lastGridHtml = "";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(sec) {
  if (sec == null || !isFinite(sec)) return "–";
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
}

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + units[i];
}

function serverNow() {
  return state ? state.server.time + (performance.now() - receivedAt) / 1000 : 0;
}

function positionOf(desired, t) {
  if (!desired || (desired.mode !== "playing" && desired.mode !== "paused")) return null;
  let pos = desired.pos;
  if (desired.mode === "playing") {
    pos += Math.max(0, t - desired.at);
    if (desired.duration) pos = desired.loop ? pos % desired.duration : Math.min(pos, desired.duration);
  }
  return pos;
}

function toast(text, isError) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = "toast"), isError ? 5000 : 2000);
}

// ------------------------------------------------------------ transport
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.onopen = () => { $("#conn").textContent = "live"; $("#conn").className = "conn ok"; };
  ws.onclose = () => {
    $("#conn").textContent = "reconnecting…";
    $("#conn").className = "conn bad";
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "state") {
      state = msg.state;
      receivedAt = performance.now();
      render();
    } else if (msg.type === "result" && !msg.ok) {
      toast(msg.error, true);
    }
  };
}

function targets() {
  return selected.size ? Array.from(selected) : "all";
}

function command(action, params = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { toast("Not connected to the server", true); return; }
  ws.send(JSON.stringify(Object.assign({ action, targets: targets(), id: nextId++ }, params)));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// --------------------------------------------------------------- render
function devicesInView() {
  const group = $("#group-filter").value;
  const showOffline = $("#show-offline").checked;
  return state.devices
    .filter((d) => (!group || d.group === group) && (showOffline || d.online))
    .sort((a, b) => (b.online - a.online) || a.label.localeCompare(b.label, undefined, { numeric: true }));
}

function targetDevices() {
  if (!state) return [];
  return selected.size ? state.devices.filter((d) => selected.has(d.id)) : state.devices;
}

function render() {
  for (const id of Array.from(selected)) if (!state.devices.some((d) => d.id === id)) selected.delete(id);
  renderStats();
  renderGroups();
  renderGrid();
  renderLibrary();
  renderSettings();
  renderLog();
  renderPanel();
}

function renderStats() {
  const devs = state.devices;
  const online = devs.filter((d) => d.online).length;
  const playing = devs.filter((d) => d.online && d.status.state === "playing").length;
  const drifts = devs.filter((d) => d.online && d.status.state === "playing" && d.status.drift_ms != null)
    .map((d) => Math.abs(d.status.drift_ms));
  const worst = drifts.length ? Math.max(...drifts) : null;
  $("#server-name").textContent = state.server.name === "SyncVR" ? "Operator" : state.server.name;
  const addr = (state.server.addresses || [])[0];
  $("#stats").innerHTML =
    `<span><b>${online}</b> / ${devs.length} online</span>` +
    `<span><b>${playing}</b> playing</span>` +
    (worst != null ? `<span>worst drift <b class="${driftClass(worst)}">${worst.toFixed(0)} ms</b></span>` : "") +
    (addr ? `<span>dashboard: <b>http://${esc(addr)}:${state.server.http_port}</b></span>` : "");
  const dl = state.downloads;
  $("#dl-summary").textContent = dl.active.length || dl.queued.length
    ? `Downloading to ${dl.active.length}, ${dl.queued.length} waiting` : "";
}

function renderGroups() {
  const groups = Array.from(new Set(state.devices.map((d) => d.group).filter(Boolean))).sort();
  const sel = $("#group-filter");
  const cur = sel.value;
  const html = `<option value="">All groups</option>` + groups.map((g) => `<option>${esc(g)}</option>`).join("");
  if (sel.innerHTML !== html) { sel.innerHTML = html; sel.value = groups.includes(cur) ? cur : ""; }
  $("#group-list").innerHTML = groups.map((g) => `<option value="${esc(g)}">`).join("");
}

function driftClass(ms) {
  ms = Math.abs(ms);
  return ms < 25 ? "good" : ms < 80 ? "meh" : "poor";
}

function contentSummary(dev) {
  const lib = state.library;
  if (!lib.length) return "";
  const have = lib.filter((v) => dev.inventory[v.name] === v.size).length;
  return `${have}/${lib.length} videos`;
}

function cardHtml(dev) {
  const st = dev.status || {};
  const stateName = dev.online ? (st.state || "connecting") : "offline";
  const dotClass = !dev.online ? "" : st.state === "error" ? "err" : "on";
  const parts = [];
  parts.push(`<div class="card-head"><span class="dot ${dotClass}"></span>` +
    `<span class="name" title="${esc(dev.id)}">${esc(dev.label)}</span>` +
    (dev.group ? `<span class="chip">${esc(dev.group)}</span>` : "") +
    `<button class="edit" data-edit="${esc(dev.id)}" aria-label="Edit">Edit</button></div>`);
  parts.push(`<div class="line"><span class="badge ${esc(stateName)}">${esc(stateName)}</span>` +
    (st.video ? `<span class="video">${esc(st.video)}</span>` : "") + `</div>`);
  if (dev.online && st.position != null && st.duration) {
    const pct = Math.min(100, (st.position / st.duration) * 100);
    parts.push(`<div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>`);
    parts.push(`<div class="line"><span>${fmtTime(st.position)} / ${fmtTime(st.duration)}</span>` +
      (st.drift_ms != null && st.state === "playing"
        ? `<span class="${driftClass(st.drift_ms)}">drift ${st.drift_ms > 0 ? "+" : ""}${st.drift_ms.toFixed(0)} ms</span>` : "") +
      (st.rate && st.rate !== 1 ? `<span>${(st.rate * 100).toFixed(1)}%</span>` : "") + `</div>`);
  }
  if (dev.online) {
    const info = [];
    if (st.battery != null && st.battery >= 0) {
      const pct = Math.round(st.battery * 100);
      info.push(`<span class="${pct < 20 ? "poor" : pct < 40 ? "meh" : ""}">battery ${pct}%${st.charging ? " ⚡" : ""}</span>`);
    }
    if (st.temp_c != null && st.temp_c > 0) info.push(`<span class="${st.temp_c > 42 ? "poor" : ""}">${st.temp_c.toFixed(0)}°C</span>`);
    if (st.worn != null) info.push(`<span>${st.worn ? "on head" : "not worn"}</span>`);
    if (st.rtt_ms != null) info.push(`<span class="${st.rtt_ms > 50 ? "meh" : ""}">rtt ${st.rtt_ms.toFixed(0)} ms</span>`);
    if (st.wifi_rssi) info.push(`<span class="${st.wifi_rssi < -75 ? "poor" : st.wifi_rssi < -65 ? "meh" : ""}">wifi ${st.wifi_rssi} dBm</span>`);
    if (st.storage_free != null && st.storage_free >= 0 && st.storage_free < 2 * 1024 ** 3) {
      info.push(`<span class="meh">${fmtBytes(st.storage_free)} free</span>`);
    }
    const cs = contentSummary(dev);
    if (cs) info.push(`<span>${cs}</span>`);
    parts.push(`<div class="line">${info.join("")}</div>`);
    if (st.download && st.download.total) {
      const pct = (st.download.received / st.download.total) * 100;
      parts.push(`<div class="line"><span>↓ ${esc(st.download.name)} ${pct.toFixed(0)}%</span></div>` +
        `<div class="bar dl"><div style="width:${pct.toFixed(1)}%"></div></div>`);
    }
    if (st.error) parts.push(`<div class="line poor">${esc(st.error)}</div>`);
  } else {
    const seen = dev.last_seen ? new Date(dev.last_seen * 1000).toLocaleString() : "never";
    parts.push(`<div class="line"><span>last seen ${esc(seen)}</span></div>`);
  }
  const cls = ["card", selected.has(dev.id) ? "selected" : "", dev.online ? "" : "offline"].join(" ");
  return `<div class="${cls}" data-id="${esc(dev.id)}">${parts.join("")}</div>`;
}

function renderGrid() {
  const html = devicesInView().map(cardHtml).join("");
  if (html !== lastGridHtml) {
    $("#grid").innerHTML = html;
    lastGridHtml = html;
  }
  $("#grid-empty").hidden = state.devices.length > 0;
}

function renderLibrary() {
  const key = JSON.stringify(state.library) + JSON.stringify(state.devices.map((d) => d.inventory));
  if (key === lastLibraryKey || document.activeElement?.closest("#library-body")) return;
  lastLibraryKey = key;
  const total = state.devices.length;
  const opt = (values, cur) => values.map(([v, label]) => `<option value="${v}"${v === cur ? " selected" : ""}>${label}</option>`).join("");
  $("#library-body").innerHTML = state.library.map((v) => {
    const have = state.devices.filter((d) => d.inventory[v.name] === v.size).length;
    return `<tr data-name="${esc(v.name)}">
      <td><input class="title" data-field="title" value="${esc(v.title)}"></td>
      <td class="file" title="${esc(v.name)}">${esc(v.name)}${v.width ? `<br><span class="muted">${v.width}×${v.height}</span>` : ""}</td>
      <td>${fmtTime(v.duration)}</td>
      <td>${fmtBytes(v.size)}</td>
      <td><select data-field="projection">${opt([["360", "360°"], ["180", "180°"], ["flat", "Flat screen"]], v.projection)}</select></td>
      <td><select data-field="stereo">${opt([["mono", "Mono"], ["tb", "3D top/bottom"], ["sbs", "3D side-by-side"]], v.stereo)}</select></td>
      <td><input type="number" data-field="rotation" min="0" max="359" step="1" value="${v.rotation}"></td>
      <td><input type="checkbox" data-field="loop"${v.loop ? " checked" : ""}></td>
      <td>${have} / ${total}</td></tr>`;
  }).join("");
  $("#library-empty").hidden = state.library.length > 0;

  const sel = $("#video-select");
  const cur = sel.value;
  sel.innerHTML = state.library.map((v) => `<option value="${esc(v.name)}">${esc(v.title || v.name)}</option>`).join("");
  if (state.library.some((v) => v.name === cur)) sel.value = cur;
}

function followLoadedVideo() {
  const dev = focusDevice();
  if (!videoTouched && dev && state.library.some((v) => v.name === dev.desired.video)) {
    $("#video-select").value = dev.desired.video;
  }
}

const SETTING_HELP = {
  play_lead_ms: ["Start delay (ms)", "How far ahead synchronized starts are scheduled. Raise on busy networks."],
  seek_lead_ms: ["Seek delay (ms)", "How far ahead playback restarts after a seek."],
  pause_lead_ms: ["Pause delay (ms)", "How far ahead synchronized pauses are scheduled."],
  correction_mode: ["Correction mode", "rate: nudge speed, seek for big jumps. seek: only jump. external: player's own clock (experimental)."],
  deadband_ms: ["Deadband (ms)", "Drift smaller than this is left alone."],
  rate_gain: ["Rate gain", "How strongly speed reacts to drift (per second of drift)."],
  max_rate_adjust: ["Max speed change", "0.05 = play at most 5% faster or slower while catching up."],
  hard_seek_ms: ["Hard re-sync above (ms)", "Drift beyond this pauses briefly and restarts in sync."],
  seek_mode_threshold_ms: ["Seek-mode threshold (ms)", "In seek mode, drift beyond this triggers a re-sync."],
  seek_cooldown_ms: ["Re-sync cooldown (ms)", "Minimum time between two re-syncs on a headset."],
  settle_ms: ["Settle time (ms)", "Ignore drift for this long after starting or seeking."],
};

let settingsKey = "";
function renderSettings() {
  const key = JSON.stringify(state.settings) + state.downloads.max_concurrent;
  if (key === settingsKey || document.activeElement?.closest("#settings-form")) return;
  settingsKey = key;
  const fields = Object.entries(state.settings).map(([k, v]) => {
    const [label, help] = SETTING_HELP[k] || [k, ""];
    const input = k === "correction_mode"
      ? `<select name="${k}">${["rate", "seek", "external"].map((m) => `<option${m === v ? " selected" : ""}>${m}</option>`).join("")}</select>`
      : `<input name="${k}" type="number" step="any" value="${v}">`;
    return `<label class="field">${label}${input}<span class="help">${help}</span></label>`;
  });
  fields.push(`<label class="field">Simultaneous downloads<input name="max_downloads" type="number" min="0" step="1" value="${state.downloads.max_concurrent}"><span class="help">Headsets downloading at once (0 = no limit). Keep low on busy Wi-Fi.</span></label>`);
  fields.push(`<div class="actions"><button type="submit" class="primary">Save settings</button><button type="button" id="settings-defaults">Restore defaults</button></div>`);
  $("#settings-form").innerHTML = fields.join("");
}

function renderLog() {
  $("#log").innerHTML = state.events.slice().reverse().map((e) =>
    `<li><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span class="${esc(e.level)}">${esc(e.message)}</span></li>`).join("");
}

// The transport panel follows the first targeted headset that has something loaded.
function focusDevice() {
  const devs = targetDevices();
  return devs.find((d) => d.desired && (d.desired.mode === "playing" || d.desired.mode === "paused")) || null;
}

function renderPanel() {
  const n = selected.size;
  $("#target-label").textContent = n ? `Controlling ${n} selected headset${n > 1 ? "s" : ""}` : `Controlling all ${state.devices.length} headsets`;
  const devs = targetDevices();
  const vols = devs.map((d) => d.volume);
  if (vols.length && document.activeElement !== $("#volume")) {
    $("#volume").value = Math.round(vols[0] * 100);
    $("#volume-value").textContent = Math.round(vols[0] * 100) + "%";
  }
  followLoadedVideo();
  updateClock();
}

function updateClock() {
  if (!state) return;
  const dev = focusDevice();
  if (!dev) {
    $("#now-playing").textContent = "Nothing loaded";
    $("#t-pos").textContent = $("#t-dur").textContent = "0:00";
    if (!seekDragging) $("#seek").value = 0;
    return;
  }
  const d = dev.desired;
  const pos = positionOf(d, serverNow());
  const dur = d.duration;
  const vid = state.library.find((v) => v.name === d.video);
  $("#now-playing").textContent = `${d.mode === "playing" ? "Playing" : "Paused"}: ${vid ? vid.title || vid.name : d.video}`;
  if (!seekDragging) {
    $("#t-pos").textContent = fmtTime(pos);
    $("#seek").value = dur ? Math.round((pos / dur) * 1000) : 0;
  }
  $("#t-dur").textContent = fmtTime(dur);
}

// --------------------------------------------------------------- events
function wire() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  }));

  $("#grid").addEventListener("click", (ev) => {
    const edit = ev.target.closest("[data-edit]");
    if (edit) { openEdit(edit.dataset.edit); return; }
    const card = ev.target.closest(".card");
    if (!card) return;
    const id = card.dataset.id;
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    renderGrid();
    renderPanel();
  });
  $("#sel-all").onclick = () => { devicesInView().forEach((d) => selected.add(d.id)); render(); };
  $("#sel-none").onclick = () => { selected.clear(); render(); };
  $("#group-filter").onchange = () => render();
  $("#show-offline").onchange = () => render();

  const video = () => $("#video-select").value;
  $("#btn-load").onclick = () => video() ? command("load", { video: video() }) : toast("No video in the library", true);
  // The server resumes the video where the targets paused it, joins a show
  // already running it, or starts it from the beginning.
  $("#btn-play").onclick = () => video() ? command("play", { video: video() }) : toast("No video in the library", true);
  $("#video-select").onchange = () => (videoTouched = true);
  $("#btn-restart").onclick = () => command("play", { video: video(), pos: 0 });
  $("#btn-pause").onclick = () => command("pause");
  $("#btn-stop").onclick = () => command("stop");
  $("#btn-resync").onclick = () => command("resync");
  $$("[data-delta]").forEach((b) => (b.onclick = () => command("seek", { delta: Number(b.dataset.delta) })));

  const seek = $("#seek");
  seek.addEventListener("input", () => {
    seekDragging = true;
    const dev = focusDevice();
    if (dev) $("#t-pos").textContent = fmtTime((seek.value / 1000) * dev.desired.duration);
  });
  seek.addEventListener("change", () => {
    const dev = focusDevice();
    seekDragging = false;
    if (dev && dev.desired.duration) command("seek", { pos: (seek.value / 1000) * dev.desired.duration });
  });

  $("#volume").addEventListener("input", () => ($("#volume-value").textContent = $("#volume").value + "%"));
  $("#volume").addEventListener("change", () => command("volume", { value: $("#volume").value / 100 }));
  $("#btn-recenter").onclick = () => command("recenter");
  $("#btn-identify").onclick = () => command("identify");
  $("#btn-msg").onclick = () => {
    const text = $("#msg-text").value.trim();
    command("message", { text, seconds: text ? 10 : 0 });
    $("#msg-text").value = "";
  };
  $("#msg-text").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btn-msg").click(); });

  $("#btn-push").onclick = () => video() && command("sync_content", { videos: [video()] });
  $("#btn-push-all").onclick = () => command("sync_content", { videos: "all" });
  $("#btn-cancel-dl").onclick = () => command("cancel_downloads");
  $("#btn-delete").onclick = () => {
    const n = targetDevices().length;
    if (video() && confirm(`Remove "${video()}" from ${n} headset(s)?`)) command("delete_content", { videos: [video()] });
  };

  $("#rescan").onclick = () => api("POST", "/api/library/rescan").then((r) => toast(`${r.videos} video(s) in library`)).catch((e) => toast(e.message, true));
  $("#library-body").addEventListener("change", (ev) => {
    const el = ev.target.closest("[data-field]");
    if (!el) return;
    const name = el.closest("tr").dataset.name;
    const field = el.dataset.field;
    const value = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    api("POST", "/api/library/" + encodeURIComponent(name), { [field]: value })
      .then(() => toast("Saved")).catch((e) => toast(e.message, true));
  });

  $("#settings-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const body = {};
    for (const el of ev.target.elements) {
      if (!el.name) continue;
      body[el.name] = el.name === "correction_mode" ? el.value : Number(el.value);
    }
    settingsKey = "";
    api("POST", "/api/settings", body).then(() => toast("Settings saved and sent to headsets")).catch((e) => toast(e.message, true));
  });
  $("#settings-form").addEventListener("click", (ev) => {
    if (ev.target.id !== "settings-defaults") return;
    const defaults = { play_lead_ms: 1500, seek_lead_ms: 1500, pause_lead_ms: 300, correction_mode: "rate", deadband_ms: 20,
      rate_gain: 0.8, max_rate_adjust: 0.05, hard_seek_ms: 300, seek_mode_threshold_ms: 80, seek_cooldown_ms: 3000, settle_ms: 750 };
    settingsKey = "";
    api("POST", "/api/settings", defaults).then(() => toast("Defaults restored")).catch((e) => toast(e.message, true));
  });

  $("#edit-dialog").addEventListener("close", async () => {
    const action = $("#edit-dialog").returnValue;
    const id = editingId;
    editingId = null;
    try {
      if (action === "save") {
        await api("POST", "/api/devices/" + encodeURIComponent(id), { name: $("#edit-name").value, group: $("#edit-group").value });
      } else if (action === "forget") {
        await api("DELETE", "/api/devices/" + encodeURIComponent(id));
        selected.delete(id);
      }
    } catch (e) { toast(e.message, true); }
  });
}

function openEdit(id) {
  const dev = state.devices.find((d) => d.id === id);
  if (!dev) return;
  editingId = id;
  $("#edit-id").textContent = `${dev.id}${dev.model ? " · " + dev.model : ""}${dev.ip ? " · " + dev.ip : ""}${dev.app_version ? " · app " + dev.app_version : ""}`;
  $("#edit-name").value = dev.name;
  $("#edit-group").value = dev.group;
  $("#edit-forget").hidden = dev.online;
  $("#edit-dialog").showModal();
}

wire();
connect();
setInterval(updateClock, 250);
