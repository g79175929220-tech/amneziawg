"""Резервные копии, уведомления в Telegram и ежедневные задачи панели.

Только стандартная библиотека: `python3 jobs.py restore ...` работает на чистом
сервере ещё до установки зависимостей панели (см. install.sh --restore).
"""
import argparse
import html
import io
import ipaddress
import json
import os
import re
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

import awg

BACKUP_DIR = os.environ.get("AWG_BACKUP_DIR", "/var/backups/awg-panel")
BACKUP_KEEP = 14
BACKUP_HOUR = 3                     # ночная копия, по часовому поясу из настроек
BACKUP_FILES = ("state.json", "panel.json", "panel.env",
                "node_ed25519", "node_ed25519.pub", "known_hosts")   # доступ к доп. серверам
BACKUP_RE = re.compile(r"^awg-backup-\d{8}-\d{6}\.tar\.gz$")

DEFAULT_NOTIFY = {
    "token": "", "chat_id": "", "chat_title": "",
    "digest": True,          # утренняя сводка
    "alerts": True,          # мгновенные: отключения, падение туннеля
    "send_backup": False,    # ночная копия в Telegram (в ней ключи клиентов!)
    "hour": 10,              # час сводки
    "tz": "Europe/Moscow",
}
SOON_DAYS = 3
TG_API = os.environ.get("AWG_TG_API", "https://api.telegram.org")   # подмена — для тестов


# ------------------------------------------------------------------ резервные копии

def make_backup():
    """Архив с настройками и клиентами. Возвращает путь к файлу."""
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    name = time.strftime("awg-backup-%Y%m%d-%H%M%S.tar.gz")
    path = os.path.join(BACKUP_DIR, name)
    with awg._lock:      # снимок state.json, согласованный с фоновым учётом трафика
        with tarfile.open(path, "w:gz") as tar:
            for f in BACKUP_FILES:
                src = os.path.join(awg.STATE_DIR, f)
                if os.path.exists(src):
                    tar.add(src, arcname=f)
            meta = json.dumps({"created": int(time.time()), "version": 1}).encode()
            info = tarfile.TarInfo("backup.json")
            info.size, info.mtime = len(meta), int(time.time())
            tar.addfile(info, io.BytesIO(meta))
    os.chmod(path, 0o600)
    for old in list_backups()[BACKUP_KEEP:]:
        os.unlink(os.path.join(BACKUP_DIR, old["name"]))
    return path


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for n in os.listdir(BACKUP_DIR):
        if BACKUP_RE.match(n):
            st = os.stat(os.path.join(BACKUP_DIR, n))
            out.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def backup_path(name):
    """Путь к копии по имени — только существующие файлы нашего формата."""
    if not BACKUP_RE.match(name or ""):
        return None
    p = os.path.join(BACKUP_DIR, name)
    return p if os.path.isfile(p) else None


def restore(path):
    """Распаковать копию в каталог панели (поверх текущих файлов)."""
    with tarfile.open(path, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        if "state.json" not in members:
            raise RuntimeError("В архиве нет state.json — это не копия awg-panel")
        state = json.load(tar.extractfile(members["state.json"]))
        if "server" not in state or "clients" not in state:
            raise RuntimeError("state.json в архиве повреждён")
        os.makedirs(awg.STATE_DIR, mode=0o700, exist_ok=True)
        for f in BACKUP_FILES:        # только известные файлы, без путей из архива
            if f in members:
                dst = os.path.join(awg.STATE_DIR, f)
                with open(dst, "wb") as out:
                    out.write(tar.extractfile(members[f]).read())
                os.chmod(dst, 0o600)
    return state


def fixup_after_restore(public_ip, wan):
    """Новый сервер: обновить внешний интерфейс и, если endpoint был IP-адресом, — IP."""
    with awg._lock:
        state = awg.load()
        s = state["server"]
        msg = []
        if wan and s.get("wan") != wan:
            s["wan"] = wan
            msg.append(f"внешний интерфейс → {wan}")
        try:
            ipaddress.ip_address(s["endpoint"])
            is_ip = True
        except ValueError:
            is_ip = False             # домен — клиентам ничего менять не нужно
        if is_ip and public_ip and s["endpoint"] != public_ip:
            awg.remember_issued(state)
            msg.append(f"адрес сервера {s['endpoint']} → {public_ip}: клиентам нужен новый конфиг "
                       f"(ключи прежние, поменялся только Endpoint)")
            s["endpoint"] = public_ip
        awg.save(state)
        awg.write_conf(state)
        return msg


def read_backup_state(path):
    with tarfile.open(path, "r:gz") as tar:
        m = next((m for m in tar.getmembers() if m.isfile() and m.name == "state.json"), None)
        if not m:
            raise RuntimeError("В архиве нет state.json — это не копия awg-panel")
        state = json.load(tar.extractfile(m))
    if "server" not in state or "clients" not in state:
        raise RuntimeError("state.json в архиве повреждён")
    return state


def import_clients(path, dry_run=False):
    """Добавить клиентов из копии другого сервера к клиентам этого (ничего не удаляя).

    Ключи сервера и обфускация у серверов разные, поэтому перенесённым клиентам нужен
    новый конфиг: они помечаются «старый конфиг». Сроки, лимиты, трафик, заметки и
    ключи клиентов сохраняются; адрес в подсети меняется, только если занят."""
    src = read_backup_state(path)
    src_ep = f"{src['server']['endpoint']}:{src['server']['port']}"
    report = {"added": [], "skipped": [], "readdressed": []}
    with awg._lock:
        state = awg.load()
        if src["server"].get("public_key") == state["server"].get("public_key"):
            raise RuntimeError("Это копия этого же сервера — для неё есть restore")
        keys = {c["public_key"] for c in state["clients"]}
        ids = {c["id"] for c in state["clients"]}
        net = ipaddress.ip_network(state["server"]["subnet"])
        for c in src["clients"]:
            if c["public_key"] in keys:
                report["skipped"].append(c["name"])      # уже перенесён раньше
                continue
            c = dict(c)
            while c["id"] in ids:
                c["id"] = uuid.uuid4().hex[:12]
            used = {state["server"]["address"].split("/")[0]} | {x["ip"] for x in state["clients"]}
            if ipaddress.ip_address(c["ip"]) not in net or c["ip"] in used:
                old_ip, c["ip"] = c["ip"], awg.next_ip(state)
                report["readdressed"].append(f"{c['name']}: {old_ip} → {c['ip']}")
            c["last"] = {"rx": 0, "tx": 0}       # счётчики нового пира начинаются с нуля
            c["issued"] = "import:" + src_ep     # конфиг от другого сервера — всегда «старый»
            state["clients"].append(c)
            keys.add(c["public_key"]); ids.add(c["id"])
            report["added"].append(c["name"])
        if dry_run or not report["added"]:
            return report
        report["backup"] = make_backup()     # копия этого сервера до слияния
        awg.save(state)
        awg.apply(state)                      # добавляет пиров, подключённых не трогает
    return report


# ------------------------------------------------------------------ Telegram

def settings(state=None):
    state = state or awg.load()
    return {**DEFAULT_NOTIFY, **state.get("notify", {})}


def tg(token, method, data=None, files=None, timeout=20):
    """Вызов Bot API. files: {"document": (имя, bytes)}."""
    url = f"{TG_API}/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (data or {}).items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in files.items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                       f"Content-Type: application/octet-stream\r\n\r\n".encode())
            body.write(content + b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=body.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data or {}).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"Telegram: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Нет связи с api.telegram.org: {e.reason}") from None
    if not res.get("ok"):
        desc = res.get("description", "ошибка")
        if "Unauthorized" in desc:
            desc = "неверный токен бота"
        elif "chat not found" in desc:
            desc = "чат не найден — напишите боту /start"
        raise RuntimeError(f"Telegram: {desc}")
    return res["result"]


def send(text, cfg=None):
    cfg = cfg or settings()
    if not (cfg["token"] and cfg["chat_id"]):
        return False
    tg(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": text,
                                     "parse_mode": "HTML", "disable_web_page_preview": "true"})
    return True


def find_chat(token):
    """chat id последнего, кто написал боту (после /start)."""
    upd = tg(token, "getUpdates", {"timeout": 0})
    for u in reversed(upd):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            title = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
            return str(chat["id"]), title
    raise RuntimeError("Боту ещё никто не писал: откройте его в Telegram и нажмите «Старт» (/start)")


def _h(s):
    return html.escape(str(s))


def _now_local(cfg):
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(cfg["tz"]))
    except Exception:  # noqa: BLE001 — нет tzdata или неверный пояс
        return datetime.now()


def _days(n):
    m10, m100 = n % 10, n % 100
    word = "день" if m10 == 1 and m100 != 11 else "дня" if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else "дней"
    return f"{n} {word}"


def _fmt_day(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def server_title(state):
    s = state["server"]
    return f"{s['endpoint']}:{s['port']}"


def digest_text(state):
    now = time.time()
    live = awg.status(state) or {}
    cl = state["clients"]
    online = sum(1 for c in cl if c["enabled"] and now - live.get(c["public_key"], {}).get("handshake", 0) < 180)
    soon = sorted((c for c in cl if c["enabled"] and c.get("expires") and 0 < c["expires"] - now <= SOON_DAYS * 86400),
                  key=lambda c: c["expires"])
    gone = [c for c in cl if not c["enabled"] and now - c.get("disabled_at", 0) <= 86400
            and c.get("disabled_reason") in ("expired", "limit")]
    lines = [f"☀️ <b>Сводка</b> · {_h(server_title(state))}",
             f"Клиентов: {len(cl)} · активных: {sum(c['enabled'] for c in cl)} · онлайн: {online}"
             f" · тест: {sum(1 for c in cl if c.get('trial'))}"]
    if soon:
        lines.append(f"\n⏳ <b>Истекают в ближайшие {SOON_DAYS} дня:</b>")
        for c in soon:
            # Календарные дни до даты окончания — как в панели.
            days = (datetime.fromtimestamp(c["expires"]).date() - datetime.fromtimestamp(now).date()).days
            note = f" — {_h(c['note'])}" if c.get("note") else ""
            tag = " [ТЕСТ]" if c.get("trial") else ""
            lines.append(f"• {_h(c['name'])}{tag}: до {_fmt_day(c['expires'])}"
                         f" ({'последний день' if days <= 0 else _days(days)}){note}")
    if gone:
        lines.append("\n⛔ <b>Отключены за сутки:</b>")
        for c in gone:
            why = "срок истёк" if c["disabled_reason"] == "expired" else "лимит трафика"
            note = f" — {_h(c['note'])}" if c.get("note") else ""
            lines.append(f"• {_h(c['name'])}: {why}{note}")
    if not soon and not gone:
        lines.append("\nВсё спокойно: никто не истекает и никто не отключался.")
    tq = _quota_lines(state)
    if tq:
        lines.append("\n📊 <b>Трафик серверов за месяц:</b>")
        lines += tq
    return "\n".join(lines)


def event_text(state, ev):
    c = next((x for x in state["clients"] if x["id"] == ev["id"]), {"name": ev["name"]})
    note = f"\n{_h(c['note'])}" if c.get("note") else ""
    why = {"expired": "срок действия истёк", "limit": "исчерпан лимит трафика"}[ev["type"]]
    return f"⛔ <b>{_h(c['name'])}</b> отключён: {why}.{note}\n<i>{_h(server_title(state))}</i>"


# ------------------------------------------------------------------ расписание

_tunnel = {}        # id сервера -> {"fails": n, "down": bool}


def tick(events, down=(), log=print):
    """Вызывается фоновым потоком панели после awg.check() (раз в 15 секунд).
    down — серверы, которые не ответили (awg.LOCAL — этот)."""
    if isinstance(down, bool):                  # старый вызов: tick(events, tunnel_up)
        down = () if down else (awg.LOCAL,)
    state = awg.load()
    cfg = settings(state)
    tg_on = bool(cfg["token"] and cfg["chat_id"])

    if tg_on and cfg["alerts"]:
        for ev in events:
            _safe(lambda: send(event_text(state, ev), cfg), log)
        # Тревога после двух неудачных проверок подряд (~30 с), чтобы не шуметь.
        srvs = [(awg.LOCAL, state["server"])] + [(n["id"], n) for n in state.get("nodes", [])]
        for nid, srv in srvs:
            t = _tunnel.setdefault(nid, {"fails": 0, "down": False})
            up = nid not in down
            t["fails"] = 0 if up else t["fails"] + 1
            title = f"{awg.server_name(state, nid)} · {srv['endpoint']}:{srv['port']}"
            if t["fails"] >= 2 and not t["down"]:
                t["down"] = True
                err = "" if nid == awg.LOCAL else f"\n<i>{_h(_node_error(nid))}</i>"
                _safe(lambda: send(f"🔴 <b>Сервер не отвечает</b>: {_h(title)}\n"
                                   f"Его клиенты не могут подключиться.{err}", cfg), log)
            elif up and t["down"]:
                t["down"] = False
                _safe(lambda: send(f"🟢 Сервер снова работает: {_h(title)}", cfg), log)

    if tg_on and cfg["alerts"]:
        _quota_alerts(state, cfg, log)

    now = _now_local(cfg)
    today = now.strftime("%Y-%m-%d")
    jobs = state.get("jobs", {})
    if now.hour >= BACKUP_HOUR and jobs.get("backup") != today:
        _mark(state, "backup", today)
        path = _safe(make_backup, log)
        if path and tg_on and cfg["send_backup"]:
            _safe(lambda: send_backup(path, cfg, state), log)
    if tg_on and cfg["digest"] and now.hour >= int(cfg["hour"]) and jobs.get("digest") != today:
        _mark(state, "digest", today)
        _safe(lambda: send(digest_text(state), cfg), log)


def send_backup(path, cfg, state):
    with open(path, "rb") as f:
        tg(cfg["token"], "sendDocument",
           {"chat_id": cfg["chat_id"], "caption": f"💾 Резервная копия · {server_title(state)}"},
           files={"document": (os.path.basename(path), f.read())}, timeout=60)


QUOTA_LEVELS = (80, 100)


def _quota_used(state, nid, srv, period):
    cell = state.get("traffic", {}).get(period, {}).get(nid, {})
    mode = srv.get("quota_mode", "total")
    return {"out": cell.get("if_tx", 0), "in": cell.get("if_rx", 0)}.get(
        mode, cell.get("if_rx", 0) + cell.get("if_tx", 0))


def _quota_lines(state):
    period = awg._period()
    out = []
    for nid, srv in [(awg.LOCAL, state["server"])] + [(n["id"], n) for n in state.get("nodes", [])]:
        if nid not in state.get("ifstat", {}):
            continue
        used = _quota_used(state, nid, srv, period)
        q = float(srv.get("quota_tb", 1)) * 10**12
        pct = f" из {q / 10**12:g} ТБ ({used / q * 100:.0f}%)" if q else ""
        out.append(f"• {_h(awg.server_name(state, nid))}: {used / 10**12:.2f} ТБ{pct}")
    return out


def _quota_alerts(state, cfg, log):
    """Трафик сервера за месяц дошёл до 80% / 100% лимита хостинга — один раз на уровень."""
    period = awg._period()
    srvs = [(awg.LOCAL, state["server"])] + [(n["id"], n) for n in state.get("nodes", [])]
    for nid, srv in srvs:
        q = float(srv.get("quota_tb", 1)) * 10**12
        if not q:
            continue
        used = _quota_used(state, nid, srv, period)
        for lvl in QUOTA_LEVELS:
            key = f"quota:{nid}:{period}:{lvl}"
            if used >= q * lvl / 100 and not state.get("jobs", {}).get(key):
                _mark(state, key, period)
                icon = "🔴" if lvl >= 100 else "🟠"
                _safe(lambda: send(f"{icon} <b>Трафик сервера: {used / 10**12:.2f} ТБ из {q / 10**12:g} ТБ</b> ({lvl}%)\n"
                                   f"{_h(awg.server_name(state, nid))} · {_h(srv['endpoint'])}\n"
                                   f"За {period}. Подробно — страница «Трафик» в панели.", cfg), log)
    # старые отметки (прошлые месяцы) не копим
    old = [k for k in state.get("jobs", {}) if k.startswith("quota:") and k.split(":")[2] != period]
    if old:
        with awg._lock:
            st = awg.load()
            for k in old:
                st.get("jobs", {}).pop(k, None)
            awg.save(st)


def _node_error(nid):
    import nodes
    return nodes.error(nid) or ""


def _mark(state, job, day):
    """Отметить задачу выполненной сегодня (до запуска: при ошибке не повторять каждые 15 с)."""
    with awg._lock:
        st = awg.load()
        st.setdefault("jobs", {})[job] = day
        awg.save(st)
    state.setdefault("jobs", {})[job] = day


def _safe(fn, log):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log(f"jobs: {e}")
        return None


# ------------------------------------------------------------------ CLI

def main():
    p = argparse.ArgumentParser(description="Резервные копии awg-panel")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup", help="сделать копию сейчас")
    sub.add_parser("list", help="список копий")
    r = sub.add_parser("restore", help="восстановить из копии")
    r.add_argument("file")
    m = sub.add_parser("import", help="добавить клиентов из копии другого сервера (свои остаются)")
    m.add_argument("file")
    m.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    f = sub.add_parser("fixup", help="поправить адрес/интерфейс после переноса")
    f.add_argument("--public-ip", default="")
    f.add_argument("--wan", default="")
    a = p.parse_args()
    if a.cmd == "backup":
        print(make_backup())
    elif a.cmd == "list":
        for b in list_backups():
            print(f"{b['name']}  {b['size'] // 1024} КБ")
    elif a.cmd == "restore":
        st = restore(a.file)
        print(f"Восстановлено: {len(st['clients'])} клиентов, сервер {server_title(st)}")
    elif a.cmd == "import":
        src = read_backup_state(a.file)
        mine = len(awg.load()["clients"])
        print(f"Копия сервера {server_title(src)}: {len(src['clients'])} клиентов; здесь сейчас {mine}")
        r = import_clients(a.file, a.dry_run)
        for n in r["readdressed"]:
            print(f"  новый адрес в подсети: {n}")
        if r["skipped"]:
            print(f"  уже есть здесь, пропущены: {', '.join(r['skipped'])}")
        if a.dry_run:
            print(f"Будет добавлено: {len(r['added'])}. Ничего не изменено (--dry-run).")
        else:
            print(f"Добавлено: {len(r['added'])}, клиенты этого сервера не тронуты.")
            if r.get("backup"):
                print(f"  Копия до слияния: {r['backup']}")
            if r["added"]:
                print("  Перенесённым нужен новый конфиг — в панели они помечены «старый конфиг».")
    elif a.cmd == "fixup":
        for m in fixup_after_restore(a.public_ip, a.wan):
            print(f"  {m}")


if __name__ == "__main__":
    main()
