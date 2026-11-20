"use strict";

/* Frontend vanilla del dashboard de Exp-brazo.
 * Sin frameworks, sin CDN, sin fuentes externas. Todo dato del servidor se
 * pinta con textContent / createElement: nunca se interpreta como HTML.
 */

const tokenMeta = document.querySelector('meta[name="exp-brazo-token"]');
const TOKEN = tokenMeta ? tokenMeta.content : "";

/* El bootstrap remoto entrega el token por query; el token ya quedó en el
 * meta y se usa para la API/WS. Se limpia la URL para no dejarlo visible. */
try {
  if (window.location.search && new URLSearchParams(window.location.search).has("token")) {
    window.history.replaceState(null, "", window.location.pathname);
  }
} catch (error) {
  /* history puede no estar disponible; el meta sigue siendo la fuente. */
}

const GESTURES = ["HOLA", "SI", "NO", "SEIS_SIETE"];
const LAYERS = ["camera", "perception", "decision", "adapter", "supervisor"];
const JOINT_NAMES = ["J1", "J2", "J3", "J4", "J5", "J6"];
const JOINT_COLORS = ["#56b4e9", "#2fbf71", "#f0e442", "#e6a23c", "#cc79a7", "#e55353"];
const MODE_LABELS = {
  auto: "Auto",
  manual: "Manual",
  ambient: "Ambiental",
  simulation: "Simulación",
};

const state = {
  activeTab: "estado",
  status: null,
  behaviors: [],
  behaviorCards: new Map(),
  config: null,
  configDraft: null,
  cameras: [],
  gestureBars: null,
  ws: null,
  wsConnected: false,
  reconnectDelay: 1000,
  pollTimer: null,
  replayTimer: null,
  replayBusy: false,
  diagnosticsReport: "",
  events: [],
};

/* -- utilidades DOM ------------------------------------------------------ */
function $(id) {
  return document.getElementById(id);
}

function el(tag, options, children) {
  const opts = options || {};
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  if (opts.value !== undefined && opts.value !== null) node.value = String(opts.value);
  if (opts.attrs) {
    Object.keys(opts.attrs).forEach((key) => {
      const value = opts.attrs[key];
      if (value === false || value === null || value === undefined) return;
      node.setAttribute(key, value === true ? "" : String(value));
    });
  }
  if (opts.dataset) {
    Object.keys(opts.dataset).forEach((key) => {
      node.dataset[key] = String(opts.dataset[key]);
    });
  }
  if (opts.on) {
    Object.keys(opts.on).forEach((eventName) => {
      node.addEventListener(eventName, opts.on[eventName]);
    });
  }
  (children || []).forEach((child) => {
    if (child === null || child === undefined) return;
    node.append(child);
  });
  return node;
}

function clear(node) {
  if (node) node.replaceChildren();
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function labelFor(key) {
  return String(key)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

/* -- red ---------------------------------------------------------------- */
async function api(path, options) {
  const opts = Object.assign({}, options || {});
  const headers = new Headers(opts.headers || {});
  if (TOKEN) headers.set("X-ExpBrazo-Token", TOKEN);
  opts.headers = headers;
  const response = await fetch(path, opts);
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
  }
  if (!response.ok) {
    let message = "Error HTTP " + response.status;
    if (payload && payload.error) {
      message = String(payload.error);
    } else if (payload && payload.detail) {
      message = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    }
    const failure = new Error(message);
    failure.status = response.status;
    failure.payload = payload;
    throw failure;
  }
  return payload;
}

function jsonOptions(method, body) {
  return {
    method: method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/* -- toasts ------------------------------------------------------------- */
function toast(message, kind, timeout) {
  const container = $("toasts");
  if (!container) return;
  const node = el("div", {
    className: "toast toast-" + (kind || "info"),
    text: message,
    attrs: { role: "status" },
  });
  container.append(node);
  window.setTimeout(() => {
    node.classList.add("leaving");
    window.setTimeout(() => node.remove(), 250);
  }, timeout || 4800);
}

/* -- formato ------------------------------------------------------------ */
function formatUptime(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "—";
  const whole = Math.floor(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  if (hours > 0) return hours + " h " + minutes + " min";
  if (minutes > 0) return minutes + " min " + secs + " s";
  return secs + " s";
}

function formatEventTime(ts) {
  const value = Number(ts);
  if (!Number.isFinite(value)) return "—";
  if (value > 1000000000) {
    const date = new Date(value * 1000);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleTimeString("es-AR", { hour12: false });
    }
  }
  return "+" + value.toFixed(1) + "s";
}

function formatConfidence(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Math.round(Math.max(0, Math.min(1, number)) * 100) + " %";
}

function resultClass(result) {
  const safe = String(result || "desconocido").replace(/[^a-zA-Z0-9_-]/g, "-");
  return "result result-" + safe;
}

/* -- estado global ------------------------------------------------------ */
function setGlobalPill(label, kind) {
  const pill = $("global-pill");
  if (!pill) return;
  pill.textContent = label;
  pill.className = "pill pill-" + (kind || "neutral");
}

function setConnection(connected) {
  if (!connected && !state.status) {
    setGlobalPill("Sin conexión", "danger");
  }
}

function setSemaphore(name, ok, detail) {
  const dot = $("dot-" + name);
  const text = $("detail-" + name);
  if (dot) {
    let kind = "neutral";
    if (ok === true) kind = "ok";
    else if (ok === false) kind = "danger";
    dot.className = "dot dot-" + kind;
  }
  if (text) text.textContent = detail || "—";
}

function updatePauseButtons(paused) {
  const label = paused ? "Reanudar respuestas" : "Pausar respuestas";
  ["btn-pause", "btn-pause-control"].forEach((id) => {
    const button = $(id);
    if (!button) return;
    button.textContent = label;
    button.classList.toggle("btn-warn", !paused);
    button.classList.toggle("btn-ok", paused);
  });
}

function syncModeSelects(mode) {
  if (!mode) return;
  ["mode-select", "control-mode-select"].forEach((id) => {
    const select = $(id);
    if (select && select.value !== mode) select.value = mode;
  });
}

function updateStatusMessage(perception, robot) {
  const box = $("status-message");
  if (!box) return;
  let message = "";
  let kind = "notice-warn";
  if (perception && perception.camera_ok === false) {
    message =
      "Cámara no disponible — modo degradado. El dashboard sigue funcionando, " +
      "pero no se detectan gestos.";
  } else if (robot && robot.state === "fault") {
    message = "El robot reporta una falla. Revisá el diagnóstico y el paro de emergencia.";
    kind = "notice-error";
  } else if (state.status && state.status.last_error) {
    message = "Último error: " + state.status.last_error;
    kind = "notice-error";
  }
  if (message) {
    box.textContent = message;
    box.className = "notice " + kind;
  } else {
    box.textContent = "";
    box.className = "notice hidden";
  }
}

function renderStatus(data) {
  if (!data || typeof data !== "object") return;
  state.status = data;

  const perception = data.perception || {};
  const robot = data.robot || {};
  const layers = data.layers || {};
  const fallback = {
    camera: {
      ok: perception.camera_ok === true,
      detail: perception.camera_ok ? "cámara disponible" : "cámara no disponible",
    },
    perception: {
      ok: perception.camera_ok === true,
      detail: perception.last_error || (perception.camera_ok ? "activa" : "sin cámara"),
    },
    decision: { ok: true, detail: "modo " + (data.mode || "—") },
    adapter: {
      ok: robot.state === "idle" || robot.state === "busy",
      detail: "estado " + (robot.state || "desconocido"),
    },
    supervisor: { ok: true, detail: "—" },
  };

  LAYERS.forEach((name) => {
    const layer = layers[name] || fallback[name] || { ok: null, detail: "—" };
    setSemaphore(name, layer.ok, layer.detail);
  });

  const fps = Number(perception.fps);
  const latency = Number(perception.latency_ms);
  const values = {
    "robot-state": robot.state || "—",
    "mode-value": MODE_LABELS[data.mode] || data.mode || "—",
    "paused-value": data.paused ? "Pausadas" : "Activas",
    "fps-value": Number.isFinite(fps) ? fps.toFixed(1) : "—",
    "latency-value": Number.isFinite(latency) ? latency.toFixed(1) + " ms" : "—",
    "uptime-value": formatUptime(data.uptime_s),
    "events-value": String(Number(data.events_count) || 0),
    "config-version": String(data.config_version === undefined ? "—" : data.config_version),
    "last-error": data.last_error || "—",
  };
  Object.keys(values).forEach((id) => {
    const node = $(id);
    if (node) node.textContent = values[id];
  });

  const badge = $("config-version-badge");
  if (badge) badge.textContent = String(data.config_version === undefined ? "—" : data.config_version);

  updatePauseButtons(Boolean(data.paused));
  syncModeSelects(data.mode);
  updateStatusMessage(perception, robot);
  updateGestures(perception.gestures || {});
  updateLivePlaceholder(Boolean(perception.camera_ok));

  const anyFail = LAYERS.some((name) => {
    const layer = layers[name] || fallback[name];
    return layer && layer.ok === false;
  });
  if (robot.state === "fault") {
    setGlobalPill("Falla del robot", "danger");
  } else if (robot.state === "disconnected") {
    setGlobalPill("Robot desconectado", "danger");
  } else if (anyFail) {
    setGlobalPill("Modo degradado · " + (MODE_LABELS[data.mode] || data.mode || "—"), "warn");
  } else if (data.uptime_s !== undefined) {
    setGlobalPill("Operativo · " + (MODE_LABELS[data.mode] || data.mode || "—"), "ok");
  } else {
    setGlobalPill("Conectando…", "neutral");
  }
}

function liveVideoUrl() {
  return "/video.mjpeg?token=" + encodeURIComponent(TOKEN);
}

function updateLivePlaceholder(cameraOk) {
  const image = $("live-video");
  const placeholder = $("live-placeholder");
  if (!image || !placeholder) return;
  if (!cameraOk) {
    image.classList.add("hidden");
    placeholder.classList.remove("hidden");
    placeholder.textContent = "Cámara no disponible — modo degradado";
  } else {
    if (!image.getAttribute("src")) image.src = liveVideoUrl();
    image.classList.remove("hidden");
    placeholder.classList.add("hidden");
  }
}

/* -- barras de gestos --------------------------------------------------- */
function ensureGestureBars() {
  const container = $("gesture-bars");
  if (!container || state.gestureBars) return;
  clear(container);
  const bars = {};
  GESTURES.forEach((gesture) => {
    const fill = el("div", { className: "bar-fill" });
    const value = el("span", { className: "bar-value", text: "0 %" });
    const row = el("div", { className: "gesture-row" }, [
      el("span", { className: "gesture-name", text: gesture }),
      el("div", { className: "bar-track" }, [fill]),
      value,
    ]);
    container.append(row);
    bars[gesture] = { fill: fill, value: value };
  });
  state.gestureBars = bars;
}

function updateGestures(gestures) {
  ensureGestureBars();
  if (!state.gestureBars) return;
  GESTURES.forEach((gesture) => {
    const item = gestures[gesture] || {};
    const confidence = Number(item.confidence);
    const ratio = Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0;
    const nodes = state.gestureBars[gesture];
    if (!nodes) return;
    nodes.fill.style.width = (ratio * 100).toFixed(1) + "%";
    nodes.fill.classList.toggle("active", ratio >= 0.6);
    nodes.value.textContent = Math.round(ratio * 100) + " %";
  });
}

/* -- WebSocket y polling ------------------------------------------------ */
function connectWebSocket() {
  if (!window.WebSocket) {
    startPolling();
    return;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url =
    protocol + "//" + location.host + "/ws?token=" + encodeURIComponent(TOKEN);
  let socket;
  try {
    socket = new WebSocket(url);
  } catch (error) {
    startPolling();
    scheduleReconnect();
    return;
  }
  state.ws = socket;
  socket.addEventListener("open", () => {
    state.wsConnected = true;
    state.reconnectDelay = 1000;
    stopPolling();
    setConnection(true);
  });
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "status") {
        renderStatus(message.data);
      } else if (message.type === "events") {
        const events = Array.isArray(message.data) ? message.data : [];
        state.events = events;
        if (state.activeTab === "eventos") {
          const hasFilters = $("filter-gesture").value || $("filter-result").value;
          if (!hasFilters) renderEvents(events);
        }
      }
    } catch (error) {
      /* mensaje malformado: se ignora */
    }
  });
  socket.addEventListener("close", () => {
    state.wsConnected = false;
    state.ws = null;
    setConnection(false);
    startPolling();
    scheduleReconnect();
  });
  socket.addEventListener("error", () => {
    try {
      socket.close();
    } catch (error) {
      /* sin acción */
    }
  });
}

function scheduleReconnect() {
  const delay = state.reconnectDelay;
  state.reconnectDelay = Math.min(state.reconnectDelay * 2, 15000);
  window.setTimeout(connectWebSocket, delay);
}

function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = window.setInterval(async () => {
    try {
      const data = await api("/api/status");
      renderStatus(data);
    } catch (error) {
      setGlobalPill("Sin conexión con el servidor", "danger");
    }
  }, 2000);
}

function stopPolling() {
  if (!state.pollTimer) return;
  window.clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* -- tabs --------------------------------------------------------------- */
function setupTabs() {
  const buttons = document.querySelectorAll(".tab-button");
  buttons.forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });
}

function activateTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab-button").forEach((button) => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === name);
  });

  if (name === "eventos") loadEvents().catch((error) => toast(error.message, "error"));
  if (name === "control") {
    loadBehaviors().then(renderControl).catch((error) => toast(error.message, "error"));
  }
  if (name === "config") loadConfig().catch((error) => toast(error.message, "error"));
  if (name === "comportamientos") {
    loadBehaviors().then(renderBehaviors).catch((error) => toast(error.message, "error"));
  }
  if (name === "reproduccion") refreshReplay().catch((error) => toast(error.message, "error"));
}

/* -- eventos ------------------------------------------------------------ */
async function loadEvents() {
  const gesture = $("filter-gesture").value;
  const result = $("filter-result").value;
  const params = new URLSearchParams();
  params.set("limit", "200");
  if (gesture) params.set("gesture", gesture);
  if (result) params.set("result", result);
  const data = await api("/api/events?" + params.toString());
  const events = Array.isArray(data.events) ? data.events : [];
  state.events = events;
  renderEvents(events);
}

function renderEvents(events) {
  const body = $("events-body");
  if (!body) return;
  clear(body);
  const count = $("events-count");
  if (count) count.textContent = String(events.length);

  if (!events.length) {
    body.append(
      el("tr", {}, [el("td", { className: "muted", text: "Sin eventos para mostrar." })])
    );
    return;
  }

  events.forEach((record) => {
    const result = record.result || "—";
    body.append(
      el("tr", {}, [
        el("td", { className: "mono", text: formatEventTime(record.ts) }),
        el("td", { className: "mono", text: record.gesture || "—" }),
        el("td", { text: formatConfidence(record.confidence) }),
        el("td", { text: record.decision || "—" }),
        el("td", { className: "mono", text: record.behavior_id || "—" }),
        el("td", {}, [el("span", { className: resultClass(result), text: result })]),
        el("td", { className: "mono", text: record.error || "—" }),
      ])
    );
  });
}

/* -- control ------------------------------------------------------------ */
async function loadBehaviors(force) {
  if (state.behaviors.length && !force) return state.behaviors;
  const data = await api("/api/behaviors");
  state.behaviors = Array.isArray(data.behaviors) ? data.behaviors : [];
  return state.behaviors;
}

function renderControl() {
  const grid = $("control-grid");
  if (!grid) return;
  clear(grid);
  if (!state.behaviors.length) {
    grid.append(el("p", { className: "muted", text: "No hay comportamientos cargados." }));
    return;
  }
  state.behaviors.forEach((behavior) => {
    const enabled = behavior.enabled !== false;
    const button = el(
      "button",
      {
        className: "control-btn" + (enabled ? "" : " is-disabled"),
        attrs: { type: "button", disabled: !enabled },
      },
      [
        el("strong", { text: behavior.name || behavior.id }),
        el("span", { className: "meta", text: behavior.id + " · " + (behavior.trigger_gesture || "—") }),
        el("span", { className: "meta", text: enabled ? "activo" : "apagado" }),
      ]
    );
    button.addEventListener("click", () => triggerBehavior(behavior.id));
    grid.append(button);
  });
}

async function triggerBehavior(behaviorId) {
  try {
    const record = await api("/api/control/trigger/" + encodeURIComponent(behaviorId), {
      method: "POST",
    });
    showActionResult(behaviorId, record);
    const result = record && record.result ? record.result : "sin resultado";
    toast("Acción " + behaviorId + ": " + result, result === "executed" ? "ok" : "warn");
  } catch (error) {
    toast(error.message, "error");
  }
}

function showActionResult(behaviorId, record) {
  const box = $("control-result");
  if (!box) return;
  const parts = [behaviorId + ": " + ((record && record.result) || "sin resultado")];
  if (record && record.error) parts.push("error: " + record.error);
  if (record && record.duration_ms) parts.push(Math.round(record.duration_ms) + " ms");
  box.textContent = parts.join(" · ");
  box.className = "result-box small";
}

async function goHome() {
  const adapter = state.status && state.status.robot ? state.status.robot.adapter : null;
  if (adapter === "dobot") {
    const confirmed = window.confirm(
      "Vas a mover el brazo real a la posición de home. " +
        "Verificá que el área esté despejada y el paro de emergencia al alcance. ¿Continuar?"
    );
    if (!confirmed) return;
  }
  try {
    const record = await api("/api/control/home", { method: "POST" });
    showActionResult("home", record);
    toast("Home: " + ((record && record.result) || "sin resultado"), "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function togglePause() {
  const current = Boolean(state.status && state.status.paused);
  try {
    const data = await api("/api/control/pause", jsonOptions("POST", { paused: !current }));
    if (data.status) renderStatus(data.status);
    toast(data.paused ? "Respuestas pausadas" : "Respuestas reanudadas", data.paused ? "warn" : "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function changeMode(mode) {
  try {
    const data = await api("/api/control/mode", jsonOptions("POST", { mode: mode }));
    if (data.status) renderStatus(data.status);
    toast("Modo: " + (MODE_LABELS[mode] || mode), "ok");
  } catch (error) {
    toast(error.message, "error");
    if (state.status) syncModeSelects(state.status.mode);
  }
}

/* -- configuración ------------------------------------------------------ */
async function loadCameras(force) {
  if (state.cameras.length && !force) return state.cameras;
  try {
    const data = await api("/api/cameras");
    state.cameras = Array.isArray(data.devices) ? data.devices : [];
  } catch (error) {
    state.cameras = [];
  }
  const info = $("cameras-info");
  if (info) {
    if (state.cameras.length) {
      info.textContent = state.cameras.length + " dispositivo(s) detectado(s) en /dev/video*";
    } else {
      info.textContent = "No se detectaron dispositivos; ingresá el índice manualmente.";
    }
  }
  return state.cameras;
}

async function loadConfig(force) {
  if (state.config && !force) return state.config;
  const data = await api("/api/config");
  state.config = data.config || {};
  state.configDraft = clone(state.config);
  const version = $("config-version");
  if (version) version.textContent = String(data.version === undefined ? "—" : data.version);
  const badge = $("config-version-badge");
  if (badge) badge.textContent = String(data.version === undefined ? "—" : data.version);
  const versions = $("config-versions");
  if (versions) {
    versions.textContent = Array.isArray(data.versions) && data.versions.length
      ? data.versions.join(", ")
      : "sin versiones guardadas";
  }
  await loadCameras();
  renderConfigForm(state.configDraft);
  syncConfigJson();
  clearConfigError();
  return data;
}

function renderConfigForm(config) {
  const form = $("config-form");
  if (!form || !config) return;
  clear(form);
  Object.keys(config).forEach((key) => {
    if (key === "version") return;
    form.append(renderConfigField(key, config[key], [key]));
  });
}

function configSelectOptions(path) {
  const joined = path.join(".");
  if (joined === "robot.adapter") {
    return [
      { value: "mock", label: "mock (simulado)" },
      { value: "dobot", label: "dobot (robot real)" },
    ];
  }
  if (joined === "decision.busy_policy") {
    return [
      { value: "drop", label: "drop (descartar)" },
      { value: "keep_last", label: "keep_last (guardar el último)" },
    ];
  }
  if (joined === "camera.index") {
    const devices = Array.isArray(state.cameras) ? state.cameras : [];
    if (!devices.length) return null;
    return devices.map((device) => ({
      value: String(device.index),
      label:
        (device.path || "índice " + device.index) +
        " (índice " + device.index + ")" +
        (device.active ? " · activo" : ""),
    }));
  }
  return null;
}

function renderConfigField(key, value, path) {
  const joined = path.join(".");
  const options = configSelectOptions(path);
  if (options) return renderConfigSelect(key, value, path, options);

  if (path.length === 3 && path[0] === "robot" && path[1] === "joint_limits") {
    return renderJointLimit(key, value, path);
  }
  if (Array.isArray(value) && value.every((item) => typeof item === "number")) {
    return renderConfigArray(key, value, path);
  }
  if (Array.isArray(value)) {
    return renderConfigArray(key, value, path);
  }
  if (value && typeof value === "object") {
    const group = el("fieldset", { className: "config-group" }, [
      el("legend", { text: labelFor(key) }),
    ]);
    Object.keys(value).forEach((childKey) => {
      group.append(renderConfigField(childKey, value[childKey], path.concat([childKey])));
    });
    return group;
  }
  if (typeof value === "boolean") return renderConfigCheckbox(key, value, path);
  if (typeof value === "number") return renderConfigNumber(key, value, path);
  return renderConfigText(key, value, path);
}

function configInput(value, path, kind) {
  const input = el("input", { attrs: { type: "text" } });
  input.dataset.path = path.join(".");
  input.dataset.kind = kind;
  input.value = value === null || value === undefined ? "" : String(value);
  input.addEventListener("input", handleConfigInput);
  return input;
}

function renderConfigText(key, value, path) {
  return el("label", { className: "config-field" }, [
    el("span", { text: labelFor(key) }),
    configInput(value, path, "text"),
  ]);
}

function renderConfigNumber(key, value, path) {
  const input = configInput(value, path, "number");
  input.type = "number";
  input.setAttribute("step", Number.isInteger(value) ? "1" : "any");
  return el("label", { className: "config-field" }, [
    el("span", { text: labelFor(key) }),
    input,
  ]);
}

function renderConfigCheckbox(key, value, path) {
  const input = el("input", { attrs: { type: "checkbox" } });
  input.dataset.path = path.join(".");
  input.dataset.kind = "bool";
  input.checked = Boolean(value);
  input.addEventListener("change", handleConfigInput);
  return el("label", { className: "config-check" }, [
    input,
    el("span", { text: labelFor(key) }),
  ]);
}

function renderConfigSelect(key, value, path, options) {
  const select = el("select");
  select.dataset.path = path.join(".");
  select.dataset.kind = "text";
  options.forEach((option) => {
    const node = el("option", { text: option.label, attrs: { value: option.value } });
    if (String(value) === option.value) node.selected = true;
    select.append(node);
  });
  select.addEventListener("change", handleConfigInput);
  return el("label", { className: "config-field" }, [
    el("span", { text: labelFor(key) }),
    select,
  ]);
}

function renderConfigArray(key, value, path) {
  const text = Array.isArray(value) ? value.join(", ") : String(value);
  const input = configInput(text, path, "array");
  return el("label", { className: "config-field" }, [
    el("span", { text: labelFor(key) + " (separado por comas)" }),
    input,
  ]);
}

function renderJointLimit(joint, value, path) {
  const limits = Array.isArray(value) ? value : [0, 0];
  const minInput = el("input", {
    attrs: { type: "number", step: "any", placeholder: "mín" },
  });
  minInput.dataset.path = path.concat(["0"]).join(".");
  minInput.dataset.kind = "number";
  minInput.value = String(limits[0]);
  minInput.addEventListener("input", handleConfigInput);

  const maxInput = el("input", {
    attrs: { type: "number", step: "any", placeholder: "máx" },
  });
  maxInput.dataset.path = path.concat(["1"]).join(".");
  maxInput.dataset.kind = "number";
  maxInput.value = String(limits[1]);
  maxInput.addEventListener("input", handleConfigInput);

  return el("div", { className: "joint-row" }, [
    el("span", { className: "joint-name", text: joint }),
    minInput,
    maxInput,
  ]);
}

function setByPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index];
    if (cursor[part] === null || cursor[part] === undefined) {
      cursor[part] = /^\d+$/.test(parts[index + 1]) ? [] : {};
    }
    cursor = cursor[part];
  }
  cursor[parts[parts.length - 1]] = value;
}

function handleConfigInput(event) {
  const target = event.target;
  if (!target || !target.dataset || !target.dataset.path) return;
  const kind = target.dataset.kind;
  let value;
  if (kind === "bool") {
    value = target.checked;
  } else if (kind === "number") {
    if (target.value === "") {
      value = null;
    } else {
      value = Number(target.value);
      if (!Number.isFinite(value)) {
        showConfigError("Valor numérico inválido en " + target.dataset.path);
        return;
      }
    }
  } else if (kind === "array") {
    const raw = target.value.split(",").map((part) => part.trim()).filter(Boolean);
    const numbers = raw.map(Number);
    if (numbers.some((number) => !Number.isFinite(number))) {
      showConfigError("Lista numérica inválida en " + target.dataset.path);
      return;
    }
    value = numbers;
  } else {
    value = target.value;
  }
  try {
    setByPath(state.configDraft, target.dataset.path, value);
    syncConfigJson();
    clearConfigError();
  } catch (error) {
    showConfigError("No se pudo aplicar el valor: " + error.message);
  }
}

function syncConfigJson() {
  const textarea = $("config-json");
  if (!textarea || !state.configDraft) return;
  textarea.value = JSON.stringify(state.configDraft, null, 2);
}

function showConfigError(message) {
  const box = $("config-error");
  if (!box) return;
  box.textContent = message;
  box.classList.remove("hidden");
}

function clearConfigError() {
  const box = $("config-error");
  if (!box) return;
  box.textContent = "";
  box.classList.add("hidden");
}

function applyJsonToForm() {
  const textarea = $("config-json");
  if (!textarea) return;
  try {
    const parsed = JSON.parse(textarea.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("el JSON debe ser un objeto");
    }
    state.configDraft = parsed;
    renderConfigForm(state.configDraft);
    syncConfigJson();
    clearConfigError();
    toast("JSON aplicado al formulario. Revisá y guardá.", "ok");
  } catch (error) {
    showConfigError("JSON inválido: " + error.message);
  }
}

async function saveConfig() {
  clearConfigError();
  try {
    const data = await api("/api/config", jsonOptions("PUT", { config: state.configDraft }));
    const status = data && data.status ? data.status : {};
    const version = status.config_version;
    toast(
      "Configuración guardada" + (version === undefined ? "" : " · versión " + version),
      "ok"
    );
    await loadConfig(true);
    renderStatus(status);
  } catch (error) {
    showConfigError(error.message);
    toast(error.message, "error");
  }
}

async function rollbackConfig() {
  clearConfigError();
  try {
    const data = await api("/api/config/rollback", { method: "POST" });
    toast("Configuración restaurada a la versión anterior", "ok");
    await loadConfig(true);
    if (data && data.status) renderStatus(data.status);
  } catch (error) {
    showConfigError(error.message);
    toast(error.message, "error");
  }
}

/* -- comportamientos ---------------------------------------------------- */
function renderBehaviors() {
  const list = $("behaviors-list");
  if (!list) return;
  clear(list);
  state.behaviorCards = new Map();
  if (!state.behaviors.length) {
    list.append(el("p", { className: "muted", text: "No hay comportamientos cargados." }));
    return;
  }
  state.behaviors.forEach((behavior) => list.append(renderBehaviorCard(behavior)));
}

function renderBehaviorCard(behavior) {
  const card = el("article", { className: "behavior-card" });
  const title = el("div", { className: "behavior-title" }, [
    el("h3", { text: behavior.name || behavior.id }),
    el("span", { className: "badge badge-neutral mono", text: behavior.id }),
    el("span", {
      className: "badge " + (behavior.trigger_gesture ? "badge-neutral" : "badge-neutral"),
      text: behavior.trigger_gesture || "—",
    }),
    el("span", {
      className: "badge " + (behavior.enabled ? "badge-ok" : "badge-neutral"),
      text: behavior.enabled ? "Activo" : "Apagado",
    }),
    el("span", {
      className: "badge " + (behavior.validated ? "badge-ok" : "badge-warn"),
      text: behavior.validated ? "Validado" : "SIN VALIDAR",
    }),
  ]);

  const head = el("div", { className: "behavior-head" }, [title]);

  const nameInput = el("input", { attrs: { type: "text" }, value: behavior.name || "" });
  const descInput = el("textarea", { attrs: { rows: "2" } });
  descInput.value = behavior.description || "";
  const speedInput = el("input", { attrs: { type: "number", min: "1", max: "100", step: "1" } });
  speedInput.value = String(behavior.speed_pct);
  const accelInput = el("input", { attrs: { type: "number", min: "1", max: "100", step: "1" } });
  accelInput.value = String(behavior.acceleration_pct);
  const priorityInput = el("input", { attrs: { type: "number", step: "1" } });
  priorityInput.value = String(behavior.priority);
  const cooldownInput = el("input", { attrs: { type: "number", min: "0", step: "any" } });
  cooldownInput.value = String(behavior.cooldown_s);
  const enabledInput = el("input", { attrs: { type: "checkbox" } });
  enabledInput.checked = behavior.enabled !== false;

  const stepsInput = el("textarea", { className: "mono", attrs: { rows: "9", spellcheck: "false" } });
  stepsInput.value = JSON.stringify(behavior.steps || [], null, 2);

  const grid = el("div", { className: "form-grid" }, [
    el("label", {}, [el("span", { text: "Nombre" }), nameInput]),
    el("label", {}, [el("span", { text: "Velocidad (%)" }), speedInput]),
    el("label", {}, [el("span", { text: "Aceleración (%)" }), accelInput]),
    el("label", {}, [el("span", { text: "Prioridad" }), priorityInput]),
    el("label", {}, [el("span", { text: "Cooldown (s)" }), cooldownInput]),
    el("label", { className: "wide" }, [el("span", { text: "Descripción" }), descInput]),
    el("label", { className: "wide" }, [
      el("span", { text: "Pasos (JSON: array de {deltas, duration_ms, pause_ms})" }),
      stepsInput,
    ]),
    el("label", { className: "config-check" }, [enabledInput, el("span", { text: "Habilitado" })]),
  ]);

  const warnings = el("div", { className: "behavior-warnings" });
  const canvas = el("canvas", { className: "preview-canvas", attrs: { height: "260" } });

  const validateButton = el("button", {
    className: "btn btn-ghost",
    text: "Validar",
    attrs: { type: "button" },
  });
  const testButton = el("button", {
    className: "btn btn-ghost",
    text: "Probar en Mock",
    attrs: { type: "button" },
  });
  const previewButton = el("button", {
    className: "btn btn-ghost",
    text: "Previsualizar",
    attrs: { type: "button" },
  });
  const saveButton = el("button", {
    className: "btn btn-ok",
    text: "Guardar catálogo",
    attrs: { type: "button" },
  });
  const actions = el("div", { className: "behavior-actions" }, [
    validateButton,
    testButton,
    previewButton,
    saveButton,
  ]);

  card.append(head, grid, actions, warnings, canvas);
  card._fields = {
    name: nameInput,
    description: descInput,
    speed: speedInput,
    acceleration: accelInput,
    priority: priorityInput,
    cooldown: cooldownInput,
    enabled: enabledInput,
    steps: stepsInput,
  };
  state.behaviorCards.set(behavior.id, card);

  validateButton.addEventListener("click", () => validateBehaviors(card));
  testButton.addEventListener("click", () => testBehaviorMock(behavior.id, card));
  previewButton.addEventListener("click", () => previewBehavior(behavior.id, card, canvas));
  saveButton.addEventListener("click", saveBehaviors);

  return card;
}

function applyCardFields(behavior, card) {
  const fields = card._fields;
  const speed = Number(fields.speed.value);
  const acceleration = Number(fields.acceleration.value);
  const priority = Number(fields.priority.value);
  const cooldown = Number(fields.cooldown.value);
  if (![speed, acceleration, priority, cooldown].every((value) => Number.isFinite(value))) {
    throw new Error('Campos numéricos inválidos en "' + behavior.id + '".');
  }
  behavior.name = fields.name.value.trim() || behavior.id;
  behavior.description = fields.description.value;
  behavior.speed_pct = speed;
  behavior.acceleration_pct = acceleration;
  behavior.priority = priority;
  behavior.cooldown_s = cooldown;
  behavior.enabled = fields.enabled.checked;

  let steps;
  try {
    steps = JSON.parse(fields.steps.value);
  } catch (error) {
    throw new Error('JSON de pasos inválido en "' + behavior.id + '": ' + error.message);
  }
  if (!Array.isArray(steps) || steps.length === 0) {
    throw new Error('"' + behavior.id + '" debe tener al menos un paso.');
  }
  behavior.steps = steps;
  return behavior;
}

function collectBehaviors() {
  const result = [];
  state.behaviors.forEach((behavior) => {
    const copy = clone(behavior);
    const card = state.behaviorCards.get(behavior.id);
    if (card) applyCardFields(copy, card);
    result.push(copy);
  });
  return result;
}

function renderBehaviorWarnings(card, warnings) {
  const box = card.querySelector(".behavior-warnings");
  if (!box) return;
  clear(box);
  if (!warnings || !warnings.length) return;
  const list = el("ul");
  warnings.forEach((warning) => list.append(el("li", { text: warning })));
  box.append(el("span", { text: "Revisar:" }), list);
}

function showBehaviorsMessage(message, kind) {
  const box = $("behaviors-message");
  if (!box) return;
  if (!message) {
    box.textContent = "";
    box.className = "notice hidden";
    return;
  }
  box.textContent = message;
  let suffix = "notice-warn";
  if (kind === "error") suffix = "notice-error";
  else if (kind === "ok") suffix = "";
  box.className = ("notice " + suffix).trim();
}

async function validateBehaviors(card) {
  try {
    const payload = { behaviors: collectBehaviors() };
    const data = await api("/api/behaviors/validate", jsonOptions("POST", payload));
    const errors = Array.isArray(data.errors) ? data.errors : [];
    renderBehaviorWarnings(card, errors);
    showBehaviorsMessage(
      errors.length ? "La validación encontró " + errors.length + " problema(s)." : "Validación sin errores.",
      errors.length ? "error" : "ok"
    );
    toast(errors.length ? "Hay errores de validación" : "Comportamientos válidos", errors.length ? "warn" : "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function testBehaviorMock(behaviorId, card) {
  try {
    const data = await api(
      "/api/behaviors/" + encodeURIComponent(behaviorId) + "/test-mock",
      { method: "POST" }
    );
    const ok = data && data.ok === true;
    renderBehaviorWarnings(card, ok ? [] : [(data && data.result && data.result.error) || "La prueba falló."]);
    toast(ok ? "Prueba en mock completada" : "La prueba en mock falló", ok ? "ok" : "error");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function previewBehavior(behaviorId, card, canvas) {
  try {
    const preview = await api("/api/behaviors/" + encodeURIComponent(behaviorId) + "/preview");
    drawPreview(canvas, preview);
    renderBehaviorWarnings(card, Array.isArray(preview.warnings) ? preview.warnings : []);
    toast("Previsualización lista (usa la versión guardada)", "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function saveBehaviors() {
  try {
    const payload = { behaviors: collectBehaviors() };
    const data = await api("/api/behaviors", jsonOptions("PUT", payload));
    state.behaviors = Array.isArray(data.behaviors) ? data.behaviors : state.behaviors;
    showBehaviorsMessage("Comportamientos guardados y aplicados.", "ok");
    toast("Comportamientos guardados", "ok");
    renderBehaviors();
  } catch (error) {
    showBehaviorsMessage(error.message, "error");
    toast(error.message, "error");
  }
}

/* -- dibujo de previsualización ---------------------------------------- */
function drawPreview(canvas, preview) {
  if (!canvas) return;
  const context = canvas.getContext("2d");
  if (!context) return;
  const steps = Array.isArray(preview.steps) ? preview.steps : [];
  const home = Array.isArray(preview.final_joints) ? preview.final_joints : [];
  const total = Math.max(1, Number(preview.total_ms) || 1);

  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(360, canvas.clientWidth || 640);
  const height = 260;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  canvas.style.height = height + "px";
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);

  const pad = { left: 46, right: 14, top: 42, bottom: 28 };
  const plotWidth = Math.max(1, width - pad.left - pad.right);
  const plotHeight = Math.max(1, height - pad.top - pad.bottom);

  const series = {};
  let minValue = Infinity;
  let maxValue = -Infinity;
  JOINT_NAMES.forEach((joint, index) => {
    const base = Number(home[index]) || 0;
    const points = [{ t: 0, v: base }];
    let previous = base;
    steps.forEach((step) => {
      const targets = step.target_joints || {};
      const target = Number(targets[joint]);
      const value = Number.isFinite(target) ? target : previous;
      const start = Number(step.t_start_ms) || 0;
      const duration = Number(step.duration_ms) || 0;
      const pause = Number(step.pause_ms) || 0;
      points.push({ t: start, v: previous });
      points.push({ t: start + duration, v: value });
      points.push({ t: start + duration + pause, v: value });
      previous = value;
      minValue = Math.min(minValue, value);
      maxValue = Math.max(maxValue, value);
    });
    minValue = Math.min(minValue, base);
    maxValue = Math.max(maxValue, base);
    series[joint] = points;
  });
  if (!Number.isFinite(minValue) || !Number.isFinite(maxValue)) {
    minValue = -1;
    maxValue = 1;
  }
  if (maxValue - minValue < 1) {
    minValue -= 1;
    maxValue += 1;
  }
  const span = maxValue - minValue;

  const xOf = (t) => pad.left + (t / total) * plotWidth;
  const yOf = (value) => pad.top + (1 - (value - minValue) / span) * plotHeight;

  context.fillStyle = "#070c12";
  context.fillRect(0, 0, width, height);

  context.strokeStyle = "rgba(147, 162, 184, 0.18)";
  context.lineWidth = 1;
  context.font = "11px ui-monospace, monospace";
  context.fillStyle = "#93a2b8";
  for (let index = 0; index <= 4; index += 1) {
    const value = minValue + (span * index) / 4;
    const y = yOf(value);
    context.beginPath();
    context.moveTo(pad.left, y);
    context.lineTo(width - pad.right, y);
    context.stroke();
    context.fillText(Math.round(value) + "°", 6, y + 4);
  }

  steps.forEach((step) => {
    const x = xOf(Number(step.t_start_ms) || 0);
    context.strokeStyle = "rgba(86, 180, 233, 0.22)";
    context.beginPath();
    context.moveTo(x, pad.top);
    context.lineTo(x, height - pad.bottom);
    context.stroke();
  });

  JOINT_NAMES.forEach((joint, index) => {
    const points = series[joint];
    if (!points || points.length < 2) return;
    context.strokeStyle = JOINT_COLORS[index];
    context.lineWidth = 2;
    context.beginPath();
    points.forEach((point, pointIndex) => {
      const x = xOf(point.t);
      const y = yOf(point.v);
      if (pointIndex === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.stroke();
  });

  context.strokeStyle = "rgba(147, 162, 184, 0.5)";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(pad.left, pad.top);
  context.lineTo(pad.left, height - pad.bottom);
  context.lineTo(width - pad.right, height - pad.bottom);
  context.stroke();

  context.fillStyle = "#93a2b8";
  context.font = "11px ui-sans-serif, system-ui, sans-serif";
  context.fillText("tiempo (ms)", width - pad.right - 70, height - 8);
  context.fillText(String(Math.round(total)), pad.left, height - 8);

  let legendX = pad.left;
  JOINT_NAMES.forEach((joint, index) => {
    context.fillStyle = JOINT_COLORS[index];
    context.fillRect(legendX, 14, 14, 4);
    context.fillStyle = "#e7edf6";
    context.font = "11px ui-monospace, monospace";
    context.fillText(joint, legendX + 18, 19);
    legendX += 52;
  });
}

/* -- diagnóstico -------------------------------------------------------- */
async function runDiagnostics() {
  const button = $("btn-run-diagnostics");
  if (button) {
    button.disabled = true;
    button.textContent = "Ejecutando…";
  }
  try {
    const data = await api("/api/troubleshooting/run", { method: "POST" });
    state.diagnosticsReport = data && data.report ? data.report : "";
    renderDiagnostics(Array.isArray(data && data.results) ? data.results : []);
    toast("Chequeos completados", "ok");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Ejecutar chequeos";
    }
  }
}

function renderDiagnostics(results) {
  const list = $("diagnostics-list");
  if (!list) return;
  clear(list);
  if (!results.length) {
    list.append(el("p", { className: "muted", text: "Sin resultados todavía." }));
    return;
  }
  results.forEach((item) => {
    let kind = "diag-skip";
    let icon = "–";
    if (item.ok === true) {
      kind = "diag-ok";
      icon = "✓";
    } else if (item.ok === false) {
      kind = "diag-fail";
      icon = "✗";
    }
    const body = el("div", { className: "diag-body" }, [
      el("strong", { text: item.name || item.id || "Chequeo" }),
      el("p", { className: "muted small", text: item.detail || "—" }),
    ]);
    if (item.probable_cause) {
      body.append(el("p", { text: "Causa probable: " + item.probable_cause }));
    }
    if (item.suggested_action) {
      body.append(el("p", { text: "Acción sugerida: " + item.suggested_action }));
    }
    if (item.duration_ms !== undefined) {
      body.append(el("p", { className: "muted small", text: "Duración: " + item.duration_ms + " ms" }));
    }
    list.append(el("div", { className: "diag-item " + kind }, [el("div", { className: "diag-icon", text: icon }), body]));
  });
}

function fallbackCopy(text) {
  const textarea = el("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("no se pudo copiar");
}

async function copyReport() {
  const text = state.diagnosticsReport || "Ejecutá los chequeos para generar un reporte.";
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    toast("Reporte copiado", "ok");
  } catch (error) {
    try {
      fallbackCopy(text);
      toast("Reporte copiado", "ok");
    } catch (fallbackError) {
      toast("No se pudo copiar el reporte", "error");
    }
  }
}

/* -- reproducción y grabación ------------------------------------------ */
async function uploadReplay(file) {
  if (!file) return;
  try {
    toast("Subiendo " + file.name + "…", "info");
    const data = await api(
      "/api/replay/upload?filename=" + encodeURIComponent(file.name),
      {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      }
    );
    renderReplayStatus(data);
    if (data && data.error) {
      toast("Replay: " + data.error, "warn");
    } else {
      toast("Video enviado. Reproduciendo…", "ok");
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshReplay() {
  if (state.activeTab !== "reproduccion" || state.replayBusy) return;
  state.replayBusy = true;
  try {
    const status = await api("/api/replay/status");
    renderReplayStatus(status);
  } catch (error) {
    /* el polling no debe llenar la pantalla de toasts */
  } finally {
    state.replayBusy = false;
  }
}

function renderReplayStatus(status) {
  if (!status || typeof status !== "object") return;
  const running = $("replay-running");
  if (running) {
    if (status.error) {
      running.textContent = "Error";
      running.className = "badge badge-danger";
    } else if (status.running) {
      running.textContent = "Reproduciendo";
      running.className = "badge badge-ok";
    } else {
      running.textContent = "Detenido";
      running.className = "badge badge-neutral";
    }
  }
  const frames = $("replay-frames");
  if (frames) frames.textContent = String(status.frames || 0);
  const events = Array.isArray(status.events) ? status.events : [];
  const eventsNode = $("replay-events");
  if (eventsNode) eventsNode.textContent = String(events.length);
  const errorNode = $("replay-error");
  if (errorNode) errorNode.textContent = status.error || "—";
  renderReplayEvents(events);
}

function renderReplayEvents(events) {
  const body = $("replay-events-body");
  if (!body) return;
  clear(body);
  if (!events.length) {
    body.append(el("tr", {}, [el("td", { className: "muted", text: "Sin eventos de replay." })]));
    return;
  }
  events.slice(-200).forEach((event) => {
    body.append(
      el("tr", {}, [
        el("td", { className: "mono", text: formatEventTime(event.timestamp) }),
        el("td", { className: "mono", text: event.gesture || "—" }),
        el("td", { text: formatConfidence(event.confidence) }),
        el("td", { text: event.source || "—" }),
      ])
    );
  });
}

async function startRecording() {
  const input = $("record-seconds");
  const seconds = input ? Number(input.value) : 10;
  if (!Number.isFinite(seconds) || seconds <= 0) {
    toast("Indicá una cantidad de segundos válida", "error");
    return;
  }
  try {
    const data = await api("/api/record/start", jsonOptions("POST", { seconds: seconds }));
    const status = $("record-status");
    if (data && data.ok) {
      if (status) status.textContent = "Grabando en " + (data.path || "(sin ruta)") + " durante " + seconds + " s.";
      toast("Grabación iniciada", "ok");
    } else {
      const message = (data && data.error) || "no se pudo iniciar la grabación";
      if (status) status.textContent = "No se pudo grabar: " + message;
      toast("No se pudo grabar: " + message, "warn");
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

async function stopRecording() {
  try {
    const data = await api("/api/record/stop", { method: "POST" });
    const status = $("record-status");
    if (status) {
      status.textContent = data && data.path
        ? "Grabación detenida: " + data.path
        : "No hay grabación activa.";
    }
    toast("Grabación detenida", "ok");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* -- snapshot ----------------------------------------------------------- */
function refreshSnapshot() {
  const image = $("live-video");
  const placeholder = $("live-placeholder");
  if (!image || !placeholder) return;
  placeholder.textContent = "Cargando snapshot…";
  placeholder.classList.remove("hidden");
  image.classList.add("hidden");
  image.onload = () => {
    placeholder.classList.add("hidden");
    image.classList.remove("hidden");
  };
  image.onerror = () => {
    image.classList.add("hidden");
    placeholder.classList.remove("hidden");
    placeholder.textContent = "No hay frame disponible. Revisá la cámara.";
  };
  image.src =
    "/api/snapshot.jpg?token=" + encodeURIComponent(TOKEN) + "&t=" + Date.now();
}

/* -- inicialización ---------------------------------------------------- */
function bindControls() {
  const pause = $("btn-pause");
  if (pause) pause.addEventListener("click", togglePause);
  const pauseControl = $("btn-pause-control");
  if (pauseControl) pauseControl.addEventListener("click", togglePause);

  const mode = $("mode-select");
  if (mode) mode.addEventListener("change", (event) => changeMode(event.target.value));
  const controlMode = $("control-mode-select");
  if (controlMode) controlMode.addEventListener("change", (event) => changeMode(event.target.value));

  const home = $("btn-home");
  if (home) home.addEventListener("click", goHome);

  const applyEvents = $("btn-apply-events");
  if (applyEvents) applyEvents.addEventListener("click", () => loadEvents().catch((error) => toast(error.message, "error")));

  const refresh = $("btn-refresh-snapshot");
  if (refresh) refresh.addEventListener("click", refreshSnapshot);

  const saveConfigButton = $("btn-save-config");
  if (saveConfigButton) saveConfigButton.addEventListener("click", saveConfig);
  const rollbackButton = $("btn-rollback-config");
  if (rollbackButton) rollbackButton.addEventListener("click", rollbackConfig);
  const applyJson = $("btn-apply-json");
  if (applyJson) applyJson.addEventListener("click", applyJsonToForm);

  const refreshCameras = $("btn-refresh-cameras");
  if (refreshCameras) {
    refreshCameras.addEventListener("click", () => {
      loadCameras(true)
        .then(() => {
          if (state.configDraft) renderConfigForm(state.configDraft);
          toast("Cámaras detectadas: " + state.cameras.length, "info");
        })
        .catch((error) => toast(error.message, "error"));
    });
  }

  const reloadBehaviors = $("btn-reload-behaviors");
  if (reloadBehaviors) {
    reloadBehaviors.addEventListener("click", () => {
      loadBehaviors(true).then(renderBehaviors).catch((error) => toast(error.message, "error"));
    });
  }

  const runDiagnosticsButton = $("btn-run-diagnostics");
  if (runDiagnosticsButton) runDiagnosticsButton.addEventListener("click", runDiagnostics);
  const copyButton = $("btn-copy-report");
  if (copyButton) copyButton.addEventListener("click", copyReport);

  const replayFile = $("replay-file");
  if (replayFile) {
    replayFile.addEventListener("change", () => {
      const file = replayFile.files && replayFile.files[0];
      if (file) uploadReplay(file);
    });
  }
  const recordStart = $("btn-record-start");
  if (recordStart) recordStart.addEventListener("click", startRecording);
  const recordStop = $("btn-record-stop");
  if (recordStop) recordStop.addEventListener("click", stopRecording);
}

function setupTokenizedLinks() {
  const query = "token=" + encodeURIComponent(TOKEN);
  const json = $("export-json");
  if (json) json.href = "/api/events/export?format=json&" + query;
  const csv = $("export-csv");
  if (csv) csv.href = "/api/events/export?format=csv&" + query;
}

function init() {
  if (!TOKEN) {
    toast(
      "Falta el token de acceso del dashboard. Recargá la página desde la URL local.",
      "error"
    );
    return;
  }
  ensureGestureBars();
  setupTabs();
  bindControls();
  setupTokenizedLinks();
  const liveVideo = $("live-video");
  if (liveVideo) liveVideo.src = liveVideoUrl();
  connectWebSocket();
  api("/api/status")
    .then(renderStatus)
    .catch((error) => {
      setGlobalPill("Sin conexión con el servidor", "danger");
      toast("No se pudo leer el estado: " + error.message, "error");
    });
  state.replayTimer = window.setInterval(() => {
    refreshReplay();
  }, 1000);
}

document.addEventListener("DOMContentLoaded", init);
