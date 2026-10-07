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
    if request.endpoint in ("login", "static"):
        return
    if not session.get("auth"):
        return redirect(url_for("login"))
    if request.method == "POST":
        tok = request.form.get("csrf", "")
        if not hmac.compare_digest(tok, session.get("csrf", "")):
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
    state = awg.load()
    live = awg.status(state)
    now = time.time()
    rows = []
    for c in state["clients"]:
        st = (live or {}).get(c["public_key"], {})
        hs = st.get("handshake", 0)
        rows.append({**c, "rx": st.get("rx", 0), "tx": st.get("tx", 0),
                     "handshake": hs, "endpoint": st.get("endpoint", ""),
                     "online": bool(hs) and now - hs < 180})
    return render_template("index.html", s=state["server"], clients=rows, up=live is not None)


@app.post("/clients")
def create_client():
    name = request.form.get("name", "").strip()[:64]
    if not name:
        flash("Укажите имя клиента", "err")
        return redirect(url_for("index"))
    try:
        c = awg.add_client(name)
    except Exception as e:  # noqa: BLE001
        flash(f"Ошибка: {e}", "err")
        return redirect(url_for("index"))
    return redirect(url_for("client", cid=c["id"]))


def _get_client(cid):
    state = awg.load()
    for c in state["clients"]:
        if c["id"] == cid:
            return state, c
    abort(404)


@app.get("/clients/<cid>")
def client(cid):
    state, c = _get_client(cid)
    conf = awg.client_conf(state, c)
    img = qrcode.make(conf, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return render_template("client.html", c=c, conf=conf, qr=buf.getvalue().decode())


@app.get("/clients/<cid>/download")
def download(cid):
    state, c = _get_client(cid)
    fname = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in c["name"]) or "client"
    return Response(awg.client_conf(state, c), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.conf"'})


@app.post("/clients/<cid>/toggle")
def toggle(cid):
    _, c = _get_client(cid)
    awg.update_client(cid, enabled=not c["enabled"])
    return redirect(url_for("index"))


@app.post("/clients/<cid>/rename")
def rename(cid):
    name = request.form.get("name", "").strip()[:64]
    if name:
        awg.update_client(cid, name=name)
    return redirect(url_for("client", cid=cid))


@app.post("/clients/<cid>/delete")
def delete(cid):
    awg.delete_client(cid)
    flash("Клиент удалён", "ok")
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
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
                for k in (awg.PARAMS_V2 if s["proto"] >= 2 else awg.PARAMS_V1):
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
    keys = awg.PARAMS_V2 if s["proto"] >= 2 else awg.PARAMS_V1
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
    i.add_argument("--proto", type=int, default=2, choices=[1, 2])
    i.add_argument("--iface", default="awg0")
    i.add_argument("--force", action="store_true")
    sp = sub.add_parser("set-proto", help="сменить версию протокола и перегенерировать обфускацию")
    sp.add_argument("proto", type=int, choices=[1, 2])
    pw = sub.add_parser("set-password", help="задать логин/пароль панели")
    pw.add_argument("--username", default="admin")
    pw.add_argument("password")
    sub.add_parser("render", help="записать серверный конфиг из state.json")
    a = p.parse_args()

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


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        app.run(host="127.0.0.1", port=8080)
