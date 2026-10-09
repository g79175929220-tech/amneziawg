// Одностраничный интерфейс панели: список клиентов с живой статистикой.
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const HISTORY = 60;          // точек на графике (секунд)
const POLL_MS = 1000;

const $ = (sel, root = document) => root.querySelector(sel);
const listEl = $("#clients");
const rows = new Map();      // id -> {el, prev, hist}
let clients = [];
let cascade = null;          // адрес входного сервера каскада, если настроен
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
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
  } catch {
    throw new Error("Нет связи с панелью — проверьте интернет или перезапуск панели");
  }
  if (res.status === 401) { location.href = "/login"; throw new Error("auth"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (data && data.error) || `Ошибка ${res.status} — обновите страницу (F5)`;
    // Устаревшая сессия: обновляем страницу сами, чтобы получить свежий токен.
    if (data && data.reload) setTimeout(() => location.reload(), 2500);
    throw new Error(msg);
  }
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
  limits: "M3 17v2h6v-2zm0-12v2h10V5zm10 16v-2h8v-2h-8v-2h-2v6zM7 9v2H3v2h4v2h2V9zm14 4v-2H11v2zm-6-4h2V7h4V5h-4V3h-2z",
  link: "M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z",
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
  const link = document.createElement("span");
  link.className = "link";
  meta.append(ip, seen, exp, link);
  const quota = document.createElement("div");
  quota.className = "quota";
  const bar = document.createElement("span");
  bar.className = "bar";
  const barFill = document.createElement("i");
  bar.appendChild(barFill);
  const quotaText = document.createElement("span");
  quota.append(bar, quotaText);
  info.append(name, meta, quota);

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
    mk("link", "Одноразовая ссылка", () => openShare(c.id)),
    mk("limits", "Ограничения: срок и трафик", () => openLimits(c.id)),
    mk("trash", "Удалить", () => openDelete(c.id)),
  );

  li.append(spark, avatar, info, traffic, actions);
  return { el: li, avatar, avText, dot, name, ip, seen, exp, link, quota, barFill, quotaText,
           tDown, tUp, cb, pDown, pUp,
           prev: null, hist: { down: [], up: [] } };
}

function updateRow(row, c, now) {
  row.el.classList.toggle("disabled", !c.enabled);
  row.el.classList.toggle("online", c.online);
  row.avatar.style.setProperty("--h", hue(c.name));
  row.avText.textContent = initials(c.name);
  if (!row.renaming) row.name.textContent = c.name;
  row.ip.textContent = c.ip;
  const REASONS = { limit: "лимит исчерпан", expired: "срок истёк" };
  row.seen.textContent = c.enabled ? (c.online ? "в сети" : fmtAgo(c.handshake, now))
                                   : (REASONS[c.disabled_reason] || "отключён");
  row.seen.className = c.online ? "on" : (REASONS[c.disabled_reason] ? "reason" : "");
  row.link.textContent = (c.via === "cascade" && cascade ? "⇄ каскад  " : "") +
                         (c.share_expires ? "🔗 ссылка активна" : "");
  row.link.title = c.share_expires ? `Одноразовая ссылка действует до ${new Date(c.share_expires * 1000).toLocaleString("ru-RU")}` : "";

  row.quota.hidden = !c.limit_bytes;
  if (c.limit_bytes) {
    const used = c.rx + c.tx, pct = Math.min(100, (used / c.limit_bytes) * 100);
    row.barFill.style.width = pct.toFixed(1) + "%";
    row.quota.classList.toggle("warn", pct >= 80 && pct < 100);
    row.quota.classList.toggle("over", pct >= 100);
    row.quotaText.textContent = `${fmtBytes(used)} из ${fmtBytes(c.limit_bytes)}` +
                                (c.limit_period === "month" ? " в месяц" : "");
  }

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
    if (c.online) online++;
    rx += c.rx; tx += c.tx;
  }
  for (const [id, row] of rows) {
    if (!seen.has(id)) { row.el.remove(); rows.delete(id); }
  }
  // Порядок как на сервере (по дате создания). Узлы двигаем, только если порядок
  // изменился: лишняя перестановка сбивает фокус (переименование) и hover.
  data.clients.forEach((c, i) => {
    const el = rows.get(c.id).el;
    if (listEl.children[i] !== el) listEl.insertBefore(el, listEl.children[i] || null);
  });

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
    cascade = data.cascade;
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
  } catch (e) { cb.checked = !cb.checked; toast(e.message, true); refresh(); }
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
    const { id } = await api("POST", "/api/clients", {
      name: f.name.value, expires: f.expires.value, via: f.via ? f.via.value : "direct",
      limit_gb: f.limit_gb.value, limit_period: f.limit_period.value,
    });
    $("#dlg-create").close();
    toast("Клиент создан");
    await refresh();
    openQR(id);
  } catch (err) { toast(err.message, true); }
});

let qrId = null;
function showRoute(via) {
  $("#qr-route").hidden = !cascade;
  $("#qr-route").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.via === via));
}
$("#qr-route").addEventListener("click", async (e) => {
  const via = e.target.dataset && e.target.dataset.via;
  if (!via) return;
  try {
    await api("PATCH", `/api/clients/${qrId}`, { via });
    showRoute(via);
    $("#qr-img").src = `/api/clients/${qrId}/qr.svg?t=${Date.now()}`;
    toast(via === "cascade" ? "Маршрут: через каскад. Обновите конфиг на устройстве"
                            : "Маршрут: напрямую. Обновите конфиг на устройстве");
    refresh();
  } catch (err) { toast(err.message, true); }
});
function openQR(id) {
  const c = clients.find((x) => x.id === id);
  qrId = id;
  $("#qr-title").textContent = c ? c.name : "";
  showRoute(c && c.via === "cascade" ? "cascade" : "direct");
  $("#qr-img").src = `/api/clients/${id}/qr.svg?t=${Date.now()}`;
  $("#qr-download").href = `/clients/${id}/download`;
  $("#dlg-qr").showModal();
}
$("#qr-copy").addEventListener("click", async () => {
  try { copy(await api("GET", `/api/clients/${qrId}/config`), "Конфигурация скопирована"); }
  catch (e) { toast(e.message, true); }
});

let limId = null;
function openLimits(id) {
  const c = clients.find((x) => x.id === id);
  const f = $("#form-limits");
  limId = id;
  f.reset();
  $("#limits-name").textContent = c.name;
  f.expires.value = isoDate(c.expires);
  f.limit_gb.value = c.limit_bytes ? +(c.limit_bytes / 1024 ** 3).toFixed(2) : "";
  f.limit_period.value = c.limit_bytes ? c.limit_period : "month";
  $("#limits-usage").textContent = `Израсходовано: ${fmtBytes(c.rx + c.tx)} ` +
    `(↓ ${fmtBytes(c.tx)}, ↑ ${fmtBytes(c.rx)})`;
  $("#dlg-limits").showModal();
}
$("#form-limits").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("PATCH", `/api/clients/${limId}`, {
      expires: f.expires.value, limit_gb: f.limit_gb.value,
      limit_period: f.limit_period.value, reset_usage: f.reset_usage.checked,
    });
    $("#dlg-limits").close();
    toast("Ограничения сохранены");
    refresh();
  } catch (err) { toast(err.message, true); }
});

let shareId = null;
function openShare(id) {
  const c = clients.find((x) => x.id === id);
  shareId = id;
  $("#share-name").textContent = c.name;
  $("#share-form").hidden = false;
  $("#share-result").hidden = true;
  $("#share-create").hidden = false;
  $("#dlg-share").showModal();
}
$("#share-create").addEventListener("click", async () => {
  try {
    const r = await api("POST", `/api/clients/${shareId}/share`, { ttl: $("#share-ttl").value });
    $("#share-url").value = r.url;
    $("#share-exp").textContent = `Действует до ${new Date(r.expires * 1000).toLocaleString("ru-RU")} ` +
      "или до первого открытия. Сертификат панели самоподписанный — браузер получателя покажет предупреждение.";
    $("#share-form").hidden = true;
    $("#share-create").hidden = true;
    $("#share-result").hidden = false;
    $("#share-url").select();
    copy(r.url, "Ссылка создана и скопирована");
    refresh();
  } catch (e) { toast(e.message, true); }
});
$("#share-copy").addEventListener("click", () => copy($("#share-url").value, "Ссылка скопирована"));

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
  cascade = data.cascade;
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
