"""Веб-панель AmneziaWG."""
import argparse
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import sys
import time

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import awg
import jobs
import nodes
import totp

PANEL_FILE = os.path.join(awg.STATE_DIR, "panel.json")


def load_panel():
    with open(PANEL_FILE) as f:
        return json.load(f)


def save_panel(cfg):
    os.makedirs(awg.STATE_DIR, exist_ok=True)
    with open(PANEL_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(PANEL_FILE, 0o600)


app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_SECURE=os.environ.get("AWG_PANEL_INSECURE") != "1",
                  PERMANENT_SESSION_LIFETIME=12 * 3600)
if os.path.exists(PANEL_FILE):
    app.secret_key = load_panel()["secret_key"]

_failed = {}


def human_bytes(n):
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def ago(ts):
    if not ts:
        return "никогда"
    d = int(time.time() - ts)
    if d < 60:
        return f"{d} с назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} д назад"


def dec_bytes(n):
    """Десятичные единицы, как у хостингов: 1 ТБ = 10^12 байт."""
    n = n or 0
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} ПБ"


app.jinja_env.filters.update(bytes=human_bytes, ago=ago, dbytes=dec_bytes)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def guard():
    if request.endpoint in ("login", "login_2fa", "static", "share_page", "share_redeem"):
        return
    is_api = request.path.startswith("/api/")
    if not session.get("auth"):
        return ({"error": "Требуется вход"}, 401) if is_api else redirect(url_for("login"))
    if request.method not in ("GET", "HEAD"):
        tok = request.headers.get("X-CSRF-Token") if is_api else request.form.get("csrf")
        if not hmac.compare_digest((tok or "").encode(), session.get("csrf", "").encode()):
            # Обычно значит, что в панель заново вошли в другой вкладке и токен этой устарел.
            msg = "Сессия устарела — обновите страницу (F5) и повторите"
            return ({"error": msg, "reload": True}, 400) if is_api else abort(400, msg)


@app.errorhandler(HTTPException)
def api_http_error(e):
    """Ошибки API — всегда JSON с понятным текстом, а не HTML-страница."""
    if not request.path.startswith("/api/"):
        return e
    texts = {400: "Неверный запрос", 404: "Клиент не найден — обновите страницу",
             405: "Панель на сервере устарела — обновите её (git pull && bash install.sh)",
             413: "Слишком большой запрос"}
    desc = e.description if e.description and e.description != type(e).description else None
    msg = desc or texts.get(e.code, f"Ошибка {e.code}: {e.name}")
    app.logger.warning("API %s %s -> %s: %s", request.method, request.path, e.code, msg)
    return {"error": msg}, e.code


def _too_many(ip):
    fails = [t for t in _failed.get(ip, []) if time.time() - t < 900]
    _failed[ip] = fails
    return len(fails) >= 5


def _fail(ip, msg):
    _failed.setdefault(ip, []).append(time.time())
    time.sleep(1)
    flash(msg, "err")


def _login_ok(message=None):
    session.clear()                       # сначала очистка, потом сообщение — иначе оно пропадёт
    session.permanent = True
    session["auth"] = True
    _failed.pop(request.remote_addr, None)
    if message:
        flash(message, "ok")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        if _too_many(ip):
            flash("Слишком много попыток. Подождите 15 минут.", "err")
            return render_template("login.html"), 429
        cfg = load_panel()
        if (hmac.compare_digest(request.form.get("username", "").encode(), cfg["username"].encode())
                and check_password_hash(cfg["password_hash"], request.form.get("password", ""))):
            if cfg.get("totp_enabled"):
                # Пароль верен — второй шаг: код из приложения. 5 минут на ввод.
                session.clear()
                session["pending_2fa"] = int(time.time())
                return redirect(url_for("login_2fa"))
            return _login_ok()
        _fail(ip, "Неверный логин или пароль")
    return render_template("login.html")


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    started = session.get("pending_2fa")
    if not started or time.time() - started > 300:
        session.clear()
        return redirect(url_for("login"))
    if request.method == "POST":
        ip = request.remote_addr
        if _too_many(ip):
            flash("Слишком много попыток. Подождите 15 минут.", "err")
            return render_template("login.html", step="2fa"), 429
        code = request.form.get("code", "")
        with awg._lock:
            cfg = load_panel()
            step = totp.verify(cfg["totp_secret"], code, last_counter=cfg.get("totp_last"))
            if step is not None:
                cfg["totp_last"] = step               # тот же код повторно не пройдёт
                save_panel(cfg)
                return _login_ok()
            h = totp.hash_recovery(code)
            if code.strip() and h in cfg.get("totp_recovery", []):
                cfg["totp_recovery"].remove(h)        # запасной код — одноразовый
                save_panel(cfg)
                return _login_ok(f"Вход по запасному коду. Осталось кодов: {len(cfg['totp_recovery'])}")
        _fail(ip, "Неверный код")
    return render_template("login.html", step="2fa")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    s = awg.load()["server"]
    return render_template("index.html", s=s)


def _get_client(cid):
    state = awg.load()
    for c in state["clients"]:
        if c["id"] == cid:
            return state, c
    abort(404)


def _parse_expires(val):
    """'' -> None, 'YYYY-MM-DD' -> конец этого дня (локальное время сервера)."""
    if not val:
        return None
    try:
        return int(time.mktime(time.strptime(val, "%Y-%m-%d"))) + 86399
    except ValueError:
        abort(400, "Неверная дата")


def _parse_limit(data):
    """Лимит из запроса: limit_gb (0/пусто — без лимита), limit_period total|month."""
    raw = data.get("limit_gb")
    if raw in (None, "", 0, "0"):
        limit = None
    else:
        try:
            gb = float(str(raw).replace(",", "."))
        except ValueError:
            abort(400, "Неверный лимит")
        if gb <= 0 or gb > 1_000_000:
            abort(400, "Неверный лимит")
        limit = int(gb * 1024**3)
    period = data.get("limit_period", "total")
    if period not in ("total", "month"):
        abort(400, "Неверный период лимита")
    return limit, period


@app.get("/api/clients")
def api_clients():
    state = awg.load()
    live = awg.status(state)
    shares = awg.active_shares(state)
    now = time.time()
    out = []
    for c in state["clients"]:
        st = (live or {}).get(c["public_key"], {})
        hs = st.get("handshake", 0)
        rx, tx = awg.live_usage(c, live)
        out.append({
            "id": c["id"], "name": c["name"], "ip": c["ip"], "enabled": c["enabled"],
            "disabled_reason": c.get("disabled_reason"),
            "created": c.get("created"), "expires": c.get("expires"),
            # Дата окончания по времени сервера: браузер в другом поясе не сдвинет её на день.
            "expires_date": time.strftime("%Y-%m-%d", time.localtime(c["expires"])) if c.get("expires") else None,
            "limit_bytes": c.get("limit_bytes"), "limit_period": c.get("limit_period", "total"),
            "rx": rx, "tx": tx, "handshake": hs,
            "endpoint": st.get("endpoint", ""),
            "online": c["enabled"] and bool(hs) and now - hs < 180,
            "share_expires": shares.get(c["id"]),
            "via": c.get("via", "direct"),
            "node": awg.node_id(c),
            "trial": bool(c.get("trial")), "note": c.get("note", ""),
            "config_name": c.get("config_name") or "", "file_name": file_name(config_name(c)),
            # конфиг на устройстве со старым адресом (сменили Endpoint, порт или маршрут)
            "stale": bool(c.get("issued")) and c["issued"] != awg.client_endpoint(state, c),
        })
    return {"up": awg.LOCAL not in live.down, "now": int(now), "clients": out,
            "cascade": state["server"].get("cascade") or None,
            "servers": servers_info(state, live)}


def servers_info(state, live=None):
    """Серверы панели для интерфейса: этот и дополнительные."""
    out = []
    for nid, srv in [(awg.LOCAL, state["server"])] + [(n["id"], n) for n in state.get("nodes", [])]:
        out.append({
            "id": nid, "name": awg.server_name(state, nid),
            "endpoint": f"{srv['endpoint']}:{srv['port']}", "cascade": srv.get("cascade") or None,
            "up": None if live is None else nid not in live.down,
            "error": None if nid == awg.LOCAL else nodes.error(nid),
            "clients": sum(1 for c in state["clients"] if awg.node_id(c) == nid),
        })
    return out


def _parse_node(state, val):
    nid = str(val or awg.LOCAL)
    try:
        awg.server_by_id(state, nid)
    except KeyError:
        abort(400, "Такого сервера нет")
    return nid


@app.post("/api/clients")
def api_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:64]
    if not name:
        return {"error": "Укажите имя клиента"}, 400
    limit, period = _parse_limit(data)
    expires = _parse_expires(data.get("expires"))       # до try: 400, а не 500
    via = "cascade" if data.get("via") == "cascade" else "direct"
    note = str(data.get("note", "")).strip()[:200]
    node = _parse_node(awg.load(), data.get("node"))
    try:
        c = awg.add_client(name, expires=expires, limit_bytes=limit, limit_period=period, via=via,
                           trial=bool(data.get("trial")), note=note, node=node)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Не удалось создать клиента %r", name)
        return {"error": str(e)}, 500
    return {"id": c["id"]}


@app.patch("/api/clients/<cid>")
def api_update(cid):
    _get_client(cid)
    data = request.get_json(silent=True) or {}
    fields = {}
    if "name" in data:
        name = str(data["name"]).strip()[:64]
        if not name:
            return {"error": "Имя не может быть пустым"}, 400
        fields["name"] = name
    if "enabled" in data:
        fields["enabled"] = bool(data["enabled"])
    if "expires" in data:
        fields["expires"] = _parse_expires(data["expires"])
    if "limit_gb" in data:
        fields["limit_bytes"], fields["limit_period"] = _parse_limit(data)
    if data.get("reset_usage"):
        fields["reset_usage"] = True
    if "via" in data:
        fields["via"] = "cascade" if data["via"] == "cascade" else "direct"
    if "trial" in data:
        fields["trial"] = bool(data["trial"])
    if "note" in data:
        fields["note"] = str(data["note"]).strip()[:200]
    if "config_name" in data:
        fields["config_name"] = str(data["config_name"]).strip()[:40] or None
    if "node" in data:
        fields["node"] = _parse_node(awg.load(), data["node"])
    try:
        awg.update_client(cid, **fields)
    except ValueError as e:
        return {"error": str(e)}, 409
    return {"ok": True}


SHARE_TTLS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}


@app.post("/api/clients/<cid>/share")
def api_share(cid):
    _get_client(cid)
    ttl = SHARE_TTLS.get((request.get_json(silent=True) or {}).get("ttl"), 86400)
    token, expires = awg.create_share(cid, ttl)
    url = url_for("share_page", token=token, _external=True)
    # QR-код самой ссылки: его картинку можно переслать клиенту, он наведёт камеру.
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return {"url": url, "expires": expires, "qr_svg": buf.getvalue().decode()}


def _share_headers(resp):
    resp.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                         "X-Robots-Tag": "noindex, nofollow"})
    return resp


@app.get("/s/<token>")
def share_page(token):
    # GET ссылку не гасит: превью в мессенджерах тоже делают GET.
    c = awg.share_info(token)
    resp = app.make_response((render_template("share.html", c=c, token=token, conf=None),
                              200 if c else 404))
    return _share_headers(resp)


@app.post("/s/<token>")
def share_redeem(token):
    res = awg.redeem_share(token)
    if not res:
        resp = app.make_response((render_template("share.html", c=None, token=token, conf=None), 404))
        return _share_headers(resp)
    state, c = res
    awg.mark_issued(c["id"])
    conf = awg.client_conf(state, c)
    img = qrcode.make(conf, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    fname = file_name(config_name(c))
    return _share_headers(app.make_response(render_template(
        "share.html", c=c, token=token, conf=conf, qr=buf.getvalue().decode(), fname=fname)))


@app.delete("/api/clients/<cid>")
def api_delete(cid):
    _get_client(cid)
    awg.delete_client(cid)
    return {"ok": True}


@app.get("/api/clients/<cid>/qr.svg")
def api_qr(cid):
    state, c = _get_client(cid)
    awg.mark_issued(cid)
    img = qrcode.make(awg.client_conf(state, c), image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/clients/<cid>/config")
def api_config(cid):
    state, c = _get_client(cid)
    awg.mark_issued(cid)
    return Response(awg.client_conf(state, c), mimetype="text/plain",
                    headers={"Cache-Control": "no-store"})


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def file_name(name, short=False):
    """Имя файла латиницей: браузеры и мессенджеры теряют кириллицу в именах загрузок.
    short — для .conf: приложения WireGuard/AmneziaWG берут имя туннеля из имени файла
    и принимают не длиннее 15 символов."""
    out = []
    for ch in name:
        t = _TR.get(ch.lower())
        out.append(ch if t is None else (t.capitalize() if ch.isupper() else t))
    res = re.sub(r"[^A-Za-z0-9_-]+", "_", "".join(out)).strip("_") or "client"
    return res[:15].rstrip("_-") or "client" if short else res


def config_name(c):
    """Название конфига: своё, если задано, иначе имя клиента."""
    return c.get("config_name") or c["name"]


@app.get("/clients/<cid>/download")
def download(cid):
    """Конфиг клиента. По умолчанию .vpn — его открывает AmneziaVPN (формат определяется по
    содержимому, а на Android .vpn привязан к приложению); ?format=conf — для AmneziaWG.
    octet-stream, а не text/plain: иначе телефоны сохраняют файл как *.conf.txt."""
    state, c = _get_client(cid)
    ext = "conf" if request.args.get("format") == "conf" else "vpn"
    awg.mark_issued(cid)
    return Response(awg.client_conf(state, c), mimetype="application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="{file_name(config_name(c), short=ext == "conf")}.{ext}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]
QUOTA_MODES = {"total": "входящий + исходящий", "out": "только исходящий", "in": "только входящий"}


def month_title(period):
    y, m = period.split("-")
    return f"{MONTHS[int(m)]} {y}"


def quota_used(cell, mode):
    if mode == "out":
        return cell.get("if_tx", 0)
    if mode == "in":
        return cell.get("if_rx", 0)
    return cell.get("if_rx", 0) + cell.get("if_tx", 0)


def traffic_report(state):
    """Сводка трафика: по серверам за текущий месяц (с прогнозом), по месяцам, по клиентам."""
    period = awg._period()
    tr = state.get("traffic", {})
    srvs = [(awg.LOCAL, state["server"])] + [(n["id"], n) for n in state.get("nodes", [])]
    lt = time.localtime()
    days_in = (time.mktime((lt.tm_year + lt.tm_mon // 12, lt.tm_mon % 12 + 1, 1, 0, 0, 0, 0, 0, -1))
               - time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    elapsed = time.time() - time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    since = state.get("traffic_since", {})
    cur = []
    for nid, srv in srvs:
        cell = tr.get(period, {}).get(nid, {})
        mode = srv.get("quota_mode", "total")
        quota = int(float(srv.get("quota_tb", 1)) * 10**12)
        used = quota_used(cell, mode)
        # Прогноз до конца месяца — по средней скорости с начала учёта в этом месяце.
        start = max(since.get(nid, 0), time.time() - elapsed)
        span = max(time.time() - start, 1)
        forecast = used + used / span * max(days_in - elapsed, 0) if span > 3600 else None
        cur.append({
            "id": nid, "name": awg.server_name(state, nid), "endpoint": f"{srv['endpoint']}:{srv['port']}",
            "cell": cell, "mode": mode, "quota": quota, "quota_tb": srv.get("quota_tb", 1), "used": used,
            "pct": used / quota * 100 if quota else None, "forecast": forecast,
            "since": since.get(nid), "partial": since.get(nid, 0) > time.time() - elapsed,
            "has_if": nid in state.get("ifstat", {}),
        })
    months = []
    for m in sorted(set(tr) | {period}, reverse=True):
        row = {"period": m, "title": month_title(m), "cells": [], "if_total": 0, "vpn_total": 0}
        for nid, srv in srvs:
            cell = tr.get(m, {}).get(nid, {})
            iff = cell.get("if_rx", 0) + cell.get("if_tx", 0)
            vpn = cell.get("rx", 0) + cell.get("tx", 0)
            row["cells"].append({"if": iff, "vpn": vpn, "used": quota_used(cell, srv.get("quota_mode", "total"))})
            row["if_total"] += iff
            row["vpn_total"] += vpn
        months.append(row)
    return {"period": period, "title": month_title(period), "servers": cur, "months": months,
            "names": [awg.server_name(state, nid) for nid, _ in srvs]}


@app.route("/traffic", methods=["GET", "POST"])
def traffic():
    if request.method == "POST":
        with awg._lock:
            state = awg.load()
            try:
                srv = awg.server_by_id(state, request.form["id"])
                q = float(request.form.get("quota_tb", "0").replace(",", ".") or 0)
                if q < 0 or q > 1000:
                    raise ValueError("Лимит — от 0 до 1000 ТБ")
                mode = request.form.get("quota_mode", "total")
                if mode not in QUOTA_MODES:
                    raise ValueError("Неизвестный способ подсчёта")
                srv["quota_tb"], srv["quota_mode"] = q, mode
                awg.save(state)
                flash("Лимит сохранён", "ok")
            except (KeyError, ValueError) as e:
                flash(str(e), "err")
        return redirect(url_for("traffic"))
    state = awg.load()
    rep = traffic_report(state)
    sel = request.args.get("m") or rep["period"]
    if not re.fullmatch(r"\d{4}-\d{2}", sel):
        sel = rep["period"]
    clients = []
    for c in state["clients"]:
        rx, tx = c.get("monthly", {}).get(sel, [0, 0])
        if rx or tx:
            clients.append({"name": c["name"], "server": awg.server_name(state, awg.node_id(c)),
                            "rx": rx, "tx": tx, "total": rx + tx, "note": c.get("note", "")})
    clients.sort(key=lambda x: -x["total"])
    return render_template("traffic.html", r=rep, sel=sel, sel_title=month_title(sel), clients=clients,
                           modes=QUOTA_MODES)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        # Чтение-изменение-запись state под общей блокировкой (см. awg._StateLock).
        with awg._lock:
            return _settings()
    return _settings()


def _settings():
    state = awg.load()
    s = state["server"]
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "server":
                awg.remember_issued(state)
                s["name"] = request.form.get("name", "").strip()[:40]
                s["endpoint"] = request.form["endpoint"].strip()
                cascade = request.form.get("cascade", "").strip()
                if cascade and not re.fullmatch(r"[A-Za-z0-9.-]+:\d{1,5}", cascade):
                    raise ValueError("Адрес каскада укажите как IP:порт, например 95.163.1.2:443")
                s["cascade"] = cascade
                s["dns"] = request.form["dns"].strip()
                s["mtu"] = int(request.form["mtu"])
                s["keepalive"] = int(request.form["keepalive"])
                s["default_months"] = max(0, min(120, int(request.form.get("default_months") or 0)))
                port = int(request.form["port"])
                restart = port != s["port"]
                s["port"] = port
                awg.save(state)
                awg.apply(state, restart=restart)
                flash("Настройки сервера сохранены", "ok")
            elif action == "obfs":
                for k in awg.params_for(s["proto"]):
                    v = request.form.get(k, "").strip()
                    s["obfs"][k] = int(v) if v.isdigit() else v
                awg.save(state)
                awg.apply(state, restart=True)
                flash("Параметры обфускации применены. Обновите конфиги у всех клиентов!", "ok")
            elif action == "regen":
                s["obfs"] = awg.gen_obfuscation(s["proto"])
                awg.save(state)
                awg.apply(state, restart=True)
                flash("Параметры перегенерированы. Обновите конфиги у всех клиентов!", "ok")
            elif action == "node_add":
                n = nodes.add_node(request.form.get("name", ""), request.form.get("ssh", ""),
                                   request.form.get("endpoint", ""))
                flash(f"Сервер «{n['name']}» подключён: {n['endpoint']}:{n['port']}, подсеть {n['subnet']}", "ok")
            elif action == "node_update":
                cascade = request.form.get("cascade", "").strip()
                if cascade and not re.fullmatch(r"[A-Za-z0-9.-]+:\d{1,5}", cascade):
                    raise ValueError("Адрес каскада укажите как IP:порт, например 95.163.1.2:443")
                fields = {"name": request.form.get("name", "").strip()[:40] or "Сервер",
                          "endpoint": request.form["endpoint"].strip(), "cascade": cascade,
                          "dns": request.form["dns"].strip(), "mtu": int(request.form["mtu"])}
                nodes.update_node(request.form["id"], fields)
                flash(f"Сервер «{fields['name']}» сохранён", "ok")
            elif action == "node_delete":
                nodes.delete_node(request.form["id"])
                flash("Сервер удалён из панели. На нём самом туннель можно остановить: "
                      "systemctl disable --now awg3-quick@<интерфейс>", "ok")
            elif action == "node_push":
                n = awg.server_by_id(state, request.form["id"])
                nodes.push(state, n, force=True)
                flash(f"Конфиг отправлен на «{n['name']}»", "ok")
            elif action == "restart":
                awg.apply(state, restart=True)
                flash("Интерфейс перезапущен", "ok")
            elif action == "backup_now":
                path = jobs.make_backup()
                flash(f"Резервная копия создана: {os.path.basename(path)}", "ok")
            elif action in ("notify", "tg_find", "tg_test"):
                n = jobs.settings(state)
                token = request.form.get("token", "").strip()
                if token and not token.startswith("•"):     # «••••» — токен не меняли
                    if not re.fullmatch(r"\d{5,}:[\w-]{20,}", token):
                        raise ValueError("Токен бота выглядит неправильно — скопируйте его из @BotFather целиком")
                    if token != n["token"]:
                        n["chat_id"], n["chat_title"] = "", ""
                    n["token"] = token
                if action == "notify":
                    n["chat_id"] = request.form.get("chat_id", "").strip() or n["chat_id"]
                    for k in ("digest", "alerts", "send_backup"):
                        n[k] = request.form.get(k) == "on"
                    n["hour"] = max(0, min(23, int(request.form.get("hour") or 10)))
                    n["tz"] = request.form.get("tz", "").strip() or "Europe/Moscow"
                    flash("Настройки уведомлений сохранены", "ok")
                elif action == "tg_find":
                    if not n["token"]:
                        raise ValueError("Сначала вставьте токен бота")
                    n["chat_id"], n["chat_title"] = jobs.find_chat(n["token"])
                    flash(f"Найден чат: {n['chat_title'] or n['chat_id']}", "ok")
                state["notify"] = n
                awg.save(state)
                if action == "tg_test":
                    if not (n["token"] and n["chat_id"]):
                        raise ValueError("Нужны токен бота и chat id")
                    jobs.send("✅ Панель AmneziaWG подключена к Telegram.\n\n" + jobs.digest_text(state), n)
                    flash("Тестовое сообщение отправлено — проверьте Telegram", "ok")
            elif action in ("2fa_start", "2fa_confirm", "2fa_cancel", "2fa_disable", "2fa_codes"):
                _settings_2fa(action)
            elif action == "password":
                cfg = load_panel()
                if not check_password_hash(cfg["password_hash"], request.form["old"]):
                    flash("Текущий пароль неверен", "err")
                elif len(request.form["new"]) < 10:
                    flash("Новый пароль должен быть не короче 10 символов", "err")
                else:
                    cfg["password_hash"] = generate_password_hash(request.form["new"])
                    save_panel(cfg)
                    flash("Пароль изменён", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Ошибка: {e}", "err")
        return redirect(url_for("settings"))
    keys = awg.params_for(s["proto"])
    n = jobs.settings(state)
    masked = ("•" * 8 + n["token"][-4:]) if n["token"] else ""
    cfg = load_panel()
    tfa = {"enabled": bool(cfg.get("totp_enabled")), "left": len(cfg.get("totp_recovery", [])),
           "codes": session.pop("show_recovery", None)}
    if session.get("totp_setup") and not tfa["enabled"]:
        sec = session["totp_setup"]
        uri = totp.uri(sec, cfg["username"], f"AmneziaWG {s['endpoint']}")
        img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf)
        tfa.update(setup=True, secret=" ".join(sec[i:i + 4] for i in range(0, len(sec), 4)),
                   qr=buf.getvalue().decode())
    return render_template("settings.html", s=s, keys=keys, server_conf=awg.server_conf(state),
                           notify=n, token_masked=masked, tfa=tfa, backups=jobs.list_backups(),
                           backup_dir=jobs.BACKUP_DIR, endpoint_is_ip=_is_ip(s["endpoint"]),
                           servers=servers_info(state, awg.status(state)), node_list=state.get("nodes", []),
                           node_key=nodes.public_key(), repo_branch=REPO_BRANCH)


REPO_BRANCH = "claude/amnesiawg-web-panel-1kuz00"


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _settings_2fa(action):
    """Двухфакторный вход: включение (QR → код), отключение, новые запасные коды."""
    cfg = load_panel()
    code = request.form.get("code", "")
    if action == "2fa_start":
        session["totp_setup"] = totp.new_secret()     # в panel.json — только после подтверждения
        return
    if action == "2fa_cancel":
        session.pop("totp_setup", None)
        return
    if action == "2fa_confirm":
        sec = session.get("totp_setup")
        step = sec and totp.verify(sec, code)
        if step is None or not sec:
            raise ValueError("Код не подошёл. Проверьте, что время на телефоне выставлено автоматически")
        codes = totp.new_recovery_codes()
        cfg.update(totp_enabled=True, totp_secret=sec, totp_last=step,
                   totp_recovery=[totp.hash_recovery(c) for c in codes])
        save_panel(cfg)
        session.pop("totp_setup", None)
        session["show_recovery"] = codes
        flash("Двухфакторный вход включён. Сохраните запасные коды — они показываются один раз", "ok")
        return
    # Отключение и новые коды — только с подтверждением кодом из приложения.
    step = totp.verify(cfg.get("totp_secret", ""), code, last_counter=cfg.get("totp_last"))
    if step is None:
        raise ValueError("Неверный код из приложения (если вы только что вошли этим кодом — дождитесь следующего)")
    cfg["totp_last"] = step
    if action == "2fa_disable":
        if not check_password_hash(cfg["password_hash"], request.form.get("password", "")):
            raise ValueError("Неверный пароль")
        for k in ("totp_enabled", "totp_secret", "totp_last", "totp_recovery"):
            cfg.pop(k, None)
        flash("Двухфакторный вход отключён", "ok")
    else:
        codes = totp.new_recovery_codes()
        cfg["totp_recovery"] = [totp.hash_recovery(c) for c in codes]
        session["show_recovery"] = codes
        flash("Новые запасные коды созданы, старые больше не действуют", "ok")
    save_panel(cfg)


@app.get("/backups/<name>")
def backup_download(name):
    path = jobs.backup_path(name)
    if not path:
        abort(404)
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def cli():
    p = argparse.ArgumentParser(description="Управление awg-panel")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="создать конфигурацию сервера")
    i.add_argument("--endpoint", required=True)
    i.add_argument("--port", type=int, default=51820)
    i.add_argument("--wan", required=True)
    i.add_argument("--subnet", default="10.8.0.0/24")
    i.add_argument("--dns", default="1.1.1.1, 1.0.0.1")
    i.add_argument("--proto", type=int, default=3, choices=[1, 2, 3])
    i.add_argument("--iface", default="awg3")
    i.add_argument("--force", action="store_true")
    sp = sub.add_parser("set-proto", help="сменить версию протокола и перегенерировать обфускацию")
    sp.add_argument("proto", type=int, choices=[1, 2, 3])
    pw = sub.add_parser("set-password", help="задать логин/пароль панели")
    pw.add_argument("--username", default="admin")
    pw.add_argument("password")
    se = sub.add_parser("set-endpoint", help="адрес сервера (лучше домен) для конфигов клиентов")
    se.add_argument("endpoint")
    sub.add_parser("render", help="записать серверный конфиг из state.json")
    sub.add_parser("apply", help="применить конфиг к работающему туннелю без перезапуска")
    sub.add_parser("disable-2fa", help="отключить двухфакторный вход (если потерян телефон и коды)")
    a = p.parse_args()
    with awg._lock:
        _run_cli(a)


def _run_cli(a):
    if a.cmd == "init":
        if os.path.exists(awg.STATE_FILE) and not a.force:
            print("Конфигурация уже существует, оставляю её")
        else:
            awg.init_state(a.endpoint, a.port, a.wan, a.subnet, a.dns, a.proto, a.iface)
        awg.write_conf(awg.load())
    elif a.cmd == "set-proto":
        state = awg.load()
        state["server"]["proto"] = a.proto
        state["server"]["obfs"] = awg.gen_obfuscation(a.proto)
        awg.save(state)
        awg.write_conf(state)
    elif a.cmd == "set-password":
        cfg = load_panel() if os.path.exists(PANEL_FILE) else {"secret_key": secrets.token_hex(32)}
        cfg["username"] = a.username
        cfg["password_hash"] = generate_password_hash(a.password)
        save_panel(cfg)
    elif a.cmd == "set-endpoint":
        state = awg.load()
        if state["server"]["endpoint"] == a.endpoint:
            print(f"Endpoint уже {a.endpoint}")
            return
        awg.remember_issued(state)
        old, state["server"]["endpoint"] = state["server"]["endpoint"], a.endpoint
        awg.save(state)
        print(f"Endpoint: {old} → {a.endpoint}")
    elif a.cmd == "render":
        awg.write_conf(awg.load())
    elif a.cmd == "apply":
        awg.apply(awg.load())
    elif a.cmd == "disable-2fa":
        cfg = load_panel()
        for k in ("totp_enabled", "totp_secret", "totp_last", "totp_recovery"):
            cfg.pop(k, None)
        save_panel(cfg)
        print("Двухфакторный вход отключён. Войдите по паролю и включите его заново в настройках.")


def _check_loop():
    while True:
        time.sleep(15)
        try:
            events, down = awg.check()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("Проверка трафика и сроков клиентов: %s", e)
            events, down = [], (awg.LOCAL,)
        try:
            # Уведомления в Telegram, ночная резервная копия, утренняя сводка.
            jobs.tick(events, down, log=app.logger.warning)
        except Exception as e:  # noqa: BLE001
            app.logger.warning("Фоновые задачи: %s", e)


if __name__ != "__main__" and os.environ.get("AWG_PANEL_NO_BG") != "1":
    # Под gunicorn (один воркер) — фоновый учёт трафика, сроков и лимитов клиентов.
    import threading
    threading.Thread(target=_check_loop, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        app.run(host="127.0.0.1", port=8080)
