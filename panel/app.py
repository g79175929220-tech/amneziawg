"""Веб-панель AmneziaWG."""
import argparse
import hmac
import io
import json
import os
import secrets
import sys
import time

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import awg

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


app.jinja_env.filters.update(bytes=human_bytes, ago=ago)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def guard():
    if request.endpoint in ("login", "static", "share_page", "share_redeem"):
        return
    is_api = request.path.startswith("/api/")
    if not session.get("auth"):
        return ({"error": "Требуется вход"}, 401) if is_api else redirect(url_for("login"))
    if request.method not in ("GET", "HEAD"):
        tok = request.headers.get("X-CSRF-Token") if is_api else request.form.get("csrf")
        if not hmac.compare_digest(tok or "", session.get("csrf", "")):
            abort(400, "Неверный CSRF-токен")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        fails = [t for t in _failed.get(ip, []) if time.time() - t < 900]
        if len(fails) >= 5:
            flash("Слишком много попыток. Подождите 15 минут.", "err")
            return render_template("login.html"), 429
        cfg = load_panel()
        if (hmac.compare_digest(request.form.get("username", ""), cfg["username"])
                and check_password_hash(cfg["password_hash"], request.form.get("password", ""))):
            session.clear()
            session.permanent = True
            session["auth"] = True
            _failed.pop(ip, None)
            return redirect(url_for("index"))
        fails.append(time.time())
        _failed[ip] = fails
        time.sleep(1)
        flash("Неверный логин или пароль", "err")
    return render_template("login.html")


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
            "limit_bytes": c.get("limit_bytes"), "limit_period": c.get("limit_period", "total"),
            "rx": rx, "tx": tx, "handshake": hs,
            "endpoint": st.get("endpoint", ""),
            "online": c["enabled"] and bool(hs) and now - hs < 180,
            "share_expires": shares.get(c["id"]),
        })
    return {"up": live is not None, "now": int(now), "clients": out}


@app.post("/api/clients")
def api_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:64]
    if not name:
        return {"error": "Укажите имя клиента"}, 400
    limit, period = _parse_limit(data)
    try:
        c = awg.add_client(name, expires=_parse_expires(data.get("expires")),
                           limit_bytes=limit, limit_period=period)
    except Exception as e:  # noqa: BLE001
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
    return {"url": url_for("share_page", token=token, _external=True), "expires": expires}


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
    conf = awg.client_conf(state, c)
    img = qrcode.make(conf, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    fname = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in c["name"]) or "client"
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
    img = qrcode.make(awg.client_conf(state, c), image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/clients/<cid>/config")
def api_config(cid):
    state, c = _get_client(cid)
    return Response(awg.client_conf(state, c), mimetype="text/plain",
                    headers={"Cache-Control": "no-store"})


@app.get("/clients/<cid>/download")
def download(cid):
    state, c = _get_client(cid)
    fname = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in c["name"]) or "client"
    return Response(awg.client_conf(state, c), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.conf"'})


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
                s["endpoint"] = request.form["endpoint"].strip()
                s["dns"] = request.form["dns"].strip()
                s["mtu"] = int(request.form["mtu"])
                s["keepalive"] = int(request.form["keepalive"])
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
            elif action == "restart":
                awg.apply(state, restart=True)
                flash("Интерфейс перезапущен", "ok")
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
    return render_template("settings.html", s=s, keys=keys, server_conf=awg.server_conf(state))


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
    sub.add_parser("render", help="записать серверный конфиг из state.json")
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
    elif a.cmd == "render":
        awg.write_conf(awg.load())


def _check_loop():
    while True:
        time.sleep(15)
        try:
            awg.check()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("Проверка трафика и сроков клиентов: %s", e)


if __name__ != "__main__" and os.environ.get("AWG_PANEL_NO_BG") != "1":
    # Под gunicorn (один воркер) — фоновый учёт трафика, сроков и лимитов клиентов.
    import threading
    threading.Thread(target=_check_loop, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        app.run(host="127.0.0.1", port=8080)
