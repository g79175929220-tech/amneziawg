// Одностраничный интерфейс панели: список клиентов с живой статистикой.
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const HISTORY = 60;          // точек на графике (секунд)
const POLL_MS = 1000;

const $ = (sel, root = document) => root.querySelector(sel);
const listEl = $("#clients");
const rows = new Map();      // id -> {el, prev, hist}
let clients = [];
let filter = "";

// ---------------------------------------------------------------- утилиты
function fmtBytes(n) {
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(n < 10 ? 2 : 1)) + " " + u[i];
}
const fmtRate = (n) => fmtBytes(n) + "/s";

function fmtAgo(ts, now) {
  if (!ts) return "ещё не подключался";
  const d = Math.max(0, now - ts);
  if (d < 60) return `${d} с назад`;
  if (d < 3600) return `${Math.floor(d / 60)} мин назад`;
  if (d < 86400) return `${Math.floor(d / 3600)} ч назад`;
  return `${Math.floor(d / 86400)} д назад`;
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" });
}
function isoDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function initials(name) {
  const parts = name.replace(/[^\p{L}\p{N}]+/gu, " ").trim().split(/\s+/);
  const s = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || "?").slice(0, 2);
  return s.toUpperCase();
}
function hue(str) {
  let h = 0;
  for (const ch of str) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.className = "toast"), 2500);
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("auth"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `Ошибка ${res.status}`);
  return data;
}

function svg(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function icon(path) {
  const s = svg("svg", { viewBox: "0 0 24 24" });
  s.appendChild(svg("path", { d: path, fill: "currentColor" }));
  return s;
}
const ICONS = {
  qr: "M3 3h8v8H3zm2 2v4h4V5zm8-2h8v8h-8zm2 2v4h4V5zM3 13h8v8H3zm2 2v4h4v-4zm8-2h2v2h-2zm2 2h2v2h-2zm-2 2h2v2h-2zm4 0h2v2h-2zm2-4h2v2h-2zm0 4h2v4h-4v-2h2z",
  download: "M5 20h14v-2H5zm7-3 6-6-1.4-1.4L13 13.2V4h-2v9.2L7.4 9.6 6 11z",
  clock: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16zm.5-13H11v6l5.2 3.2.8-1.3-4.5-2.7z",
  trash: "M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6zM19 4h-3.5l-1-1h-5l-1 1H5v2h14z",
};

// ---------------------------------------------------------------- график
function sparkPath(values, max, w, h) {
  if (!values.length) return "";
  const step = w / (HISTORY - 1);
  const off = (HISTORY - values.length) * step;
  let d = `M${off},${h}`;
  values.forEach((v, i) => { d += ` L${(off + i * step).toFixed(1)},${(h - (v / max) * (h - 4)).toFixed(1)}`; });
  return d + ` L${w},${h} Z`;
}

function drawSpark(row) {
  const { down, up } = row.hist;
  const max = Math.max(1024, ...down, ...up);
  row.pDown.setAttribute("d", sparkPath(down, max, 300, 60));
  row.pUp.setAttribute("d", sparkPath(up, max, 300, 60));
}

// ---------------------------------------------------------------- строка клиента
function buildRow(c) {
  const li = document.createElement("li");
  li.className = "client";

  const spark = svg("svg", { class: "spark", viewBox: "0 0 300 60", preserveAspectRatio: "none" });
  const pDown = svg("path", { class: "down" });
  const pUp = svg("path", { class: "up" });
  spark.append(pDown, pUp);

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  const avText = document.createElement("span");
  const dot = document.createElement("i");
  dot.className = "dot";
  avatar.append(avText, dot);

  const info = document.createElement("div");
  info.className = "info";
  const name = document.createElement("div");
  name.className = "name";
  name.title = "Нажмите, чтобы переименовать";
  name.addEventListener("click", () => startRename(c.id, name));
  const meta = document.createElement("div");
  meta.className = "meta";
  const ip = document.createElement("code");
  ip.className = "ip";
  ip.title = "Скопировать IP";
  ip.addEventListener("click", () => copy(ip.textContent, "IP скопирован"));
  const seen = document.createElement("span");
  const exp = document.createElement("span");
  exp.className = "exp";
  meta.append(ip, seen, exp);
  info.append(name, meta);

  const traffic = document.createElement("div");
  traffic.className = "traffic";
  const tDown = document.createElement("div");
  const tUp = document.createElement("div");
  tDown.className = "t-down";
  tUp.className = "t-up";
  traffic.append(tDown, tUp);

  const actions = document.createElement("div");
  actions.className = "actions";
  const sw = document.createElement("label");
  sw.className = "switch";
  sw.title = "Включить / отключить";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.addEventListener("change", () => toggle(c.id, cb));
  sw.append(cb, document.createElement("span"));

  const mk = (ic, title, fn) => {
    const b = document.createElement("button");
    b.className = "icon-btn";
    b.title = title;
    b.setAttribute("aria-label", title);
    b.appendChild(icon(ICONS[ic]));
    b.addEventListener("click", fn);
    return b;
  };
  actions.append(
    sw,
    mk("qr", "QR-код", () => openQR(c.id)),
    mk("download", "Скачать .conf", () => (location.href = `/clients/${c.id}/download`)),
    mk("clock", "Срок действия", () => openExpire(c.id)),
    mk("trash", "Удалить", () => openDelete(c.id)),
  );

  li.append(spark, avatar, info, traffic, actions);
  return { el: li, avatar, avText, dot, name, ip, seen, exp, tDown, tUp, cb, pDown, pUp,
           prev: null, hist: { down: [], up: [] } };
}

function updateRow(row, c, now) {
  row.el.classList.toggle("disabled", !c.enabled);
  row.el.classList.toggle("online", c.online);
  row.avatar.style.setProperty("--h", hue(c.name));
  row.avText.textContent = initials(c.name);
  if (!row.renaming) row.name.textContent = c.name;
  row.ip.textContent = c.ip;
  row.seen.textContent = c.enabled ? (c.online ? "в сети" : fmtAgo(c.handshake, now)) : "отключён";
  row.seen.className = c.online ? "on" : "";

  row.exp.textContent = "";
  row.exp.classList.remove("warn", "over");
  if (c.expires) {
    const left = c.expires - now;
    row.exp.textContent = left > 0 ? `до ${fmtDate(c.expires)}` : "срок истёк";
    if (left <= 0) row.exp.classList.add("over");
    else if (left < 3 * 86400) row.exp.classList.add("warn");
  }
  if (document.activeElement !== row.cb) row.cb.checked = c.enabled;

  // Скорость: rx сервера = отдача клиента, tx сервера = загрузка клиента.
  let down = 0, up = 0;
  const t = performance.now() / 1000;
  if (row.prev) {
    const dt = t - row.prev.t;
    if (dt > 0) {
      down = Math.max(0, (c.tx - row.prev.tx) / dt);
      up = Math.max(0, (c.rx - row.prev.rx) / dt);
    }
  }
  row.prev = { rx: c.rx, tx: c.tx, t };
  row.hist.down.push(down);
  row.hist.up.push(up);
  if (row.hist.down.length > HISTORY) { row.hist.down.shift(); row.hist.up.shift(); }
  drawSpark(row);

  row.tDown.textContent = `↓ ${fmtRate(down)}`;
  row.tDown.title = `Всего скачано: ${fmtBytes(c.tx)}`;
  row.tDown.dataset.total = fmtBytes(c.tx);
  row.tUp.textContent = `↑ ${fmtRate(up)}`;
  row.tUp.title = `Всего отправлено: ${fmtBytes(c.rx)}`;
  row.tUp.dataset.total = fmtBytes(c.rx);
}

function matches(c) {
  if (!filter) return true;
  return c.name.toLowerCase().includes(filter) || c.ip.includes(filter);
}

function render(data) {
  const now = data.now;
  const seen = new Set();
  let online = 0, rx = 0, tx = 0;
  for (const c of data.clients) {
    seen.add(c.id);
    let row = rows.get(c.id);
    if (!row) { row = buildRow(c); rows.set(c.id, row); }
    updateRow(row, c, now);
    row.el.hidden = !matches(c);
    if (row.el.parentNode !== listEl) listEl.appendChild(row.el);
    if (c.online) online++;
    rx += c.rx; tx += c.tx;
  }
  for (const [id, row] of rows) {
    if (!seen.has(id)) { row.el.remove(); rows.delete(id); }
  }
  // Порядок как на сервере (по дате создания).
  data.clients.forEach((c) => listEl.appendChild(rows.get(c.id).el));

  $("#empty").hidden = data.clients.length > 0;
  $("#st-total").textContent = data.clients.length;
  $("#st-online").textContent = online;
  $("#st-tx").textContent = fmtBytes(tx);
  $("#st-rx").textContent = fmtBytes(rx);
  const st = $("#srv-status");
  st.textContent = data.up ? "работает" : "остановлен";
  st.className = "chip " + (data.up ? "ok" : "bad");
}

async function poll() {
  try {
    const data = await api("GET", "/api/clients");
    clients = data.clients;
    render(data);
  } catch (e) {
    if (e.message !== "auth") $("#srv-status").textContent = "нет связи";
  }
  setTimeout(poll, document.hidden ? POLL_MS * 5 : POLL_MS);
}

// ---------------------------------------------------------------- действия
async function copy(text, msg) {
  try { await navigator.clipboard.writeText(text); toast(msg); }
  catch { toast("Не удалось скопировать (нужен HTTPS)", true); }
}

async function toggle(id, cb) {
  try {
    await api("PATCH", `/api/clients/${id}`, { enabled: cb.checked });
    toast(cb.checked ? "Клиент включён" : "Клиент отключён");
  } catch (e) { cb.checked = !cb.checked; toast(e.message, true); }
}

function startRename(id, el) {
  const row = rows.get(id);
  if (row.renaming) return;
  row.renaming = true;
  const old = el.textContent;
  const input = document.createElement("input");
  input.className = "rename";
  input.value = old;
  input.maxLength = 64;
  el.textContent = "";
  el.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    row.renaming = false;
    el.textContent = save && val ? val : old;
    if (save && val && val !== old) {
      try { await api("PATCH", `/api/clients/${id}`, { name: val }); toast("Переименовано"); }
      catch (e) { el.textContent = old; toast(e.message, true); }
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function openCreate() {
  const f = $("#form-create");
  f.reset();
  $("#dlg-create").showModal();
  f.name.focus();
}

$("#form-create").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const { id } = await api("POST", "/api/clients", { name: f.name.value, expires: f.expires.value });
    $("#dlg-create").close();
    toast("Клиент создан");
    await refresh();
    openQR(id);
  } catch (err) { toast(err.message, true); }
});

let qrId = null;
function openQR(id) {
  const c = clients.find((x) => x.id === id);
  qrId = id;
  $("#qr-title").textContent = c ? c.name : "";
  $("#qr-img").src = `/api/clients/${id}/qr.svg?t=${Date.now()}`;
  $("#qr-download").href = `/clients/${id}/download`;
  $("#dlg-qr").showModal();
}
$("#qr-copy").addEventListener("click", async () => {
  try { copy(await api("GET", `/api/clients/${qrId}/config`), "Конфигурация скопирована"); }
  catch (e) { toast(e.message, true); }
});

let expId = null;
function openExpire(id) {
  const c = clients.find((x) => x.id === id);
  expId = id;
  $("#expire-name").textContent = c.name;
  $("#form-expire").expires.value = isoDate(c.expires);
  $("#dlg-expire").showModal();
}
$("#form-expire").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("PATCH", `/api/clients/${expId}`, { expires: e.target.expires.value });
    $("#dlg-expire").close();
    toast("Срок действия сохранён");
    refresh();
  } catch (err) { toast(err.message, true); }
});

let delId = null;
function openDelete(id) {
  const c = clients.find((x) => x.id === id);
  delId = id;
  $("#del-name").textContent = c.name;
  $("#dlg-delete").showModal();
}
$("#del-confirm").addEventListener("click", async () => {
  try {
    await api("DELETE", `/api/clients/${delId}`);
    $("#dlg-delete").close();
    toast("Клиент удалён");
    refresh();
  } catch (e) { toast(e.message, true); }
});

async function refresh() {
  const data = await api("GET", "/api/clients");
  clients = data.clients;
  render(data);
}

$("#btn-new").addEventListener("click", openCreate);
$("#search").addEventListener("input", (e) => {
  filter = e.target.value.trim().toLowerCase();
  clients.forEach((c) => { const r = rows.get(c.id); if (r) r.el.hidden = !matches(c); });
});
// Закрытие модалок кликом по фону.
document.querySelectorAll("dialog").forEach((d) =>
  d.addEventListener("click", (e) => { if (e.target === d) d.close(); }));

poll();
