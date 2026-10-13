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
BACKUP_FILES = ("state.json", "panel.json", "panel.env")
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
            msg.append(f"адрес сервера {s['endpoint']} → {public_ip}: клиентам нужен новый конфиг "
                       f"(ключи прежние, поменялся только Endpoint)")
            s["endpoint"] = public_ip
        awg.save(state)
        awg.write_conf(state)
        return msg


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
    return "\n".join(lines)


def event_text(state, ev):
    c = next((x for x in state["clients"] if x["id"] == ev["id"]), {"name": ev["name"]})
    note = f"\n{_h(c['note'])}" if c.get("note") else ""
    why = {"expired": "срок действия истёк", "limit": "исчерпан лимит трафика"}[ev["type"]]
    return f"⛔ <b>{_h(c['name'])}</b> отключён: {why}.{note}\n<i>{_h(server_title(state))}</i>"


# ------------------------------------------------------------------ расписание

_tunnel = {"fails": 0, "down": False}


def tick(events, tunnel_up, log=print):
    """Вызывается фоновым потоком панели после awg.check() (раз в 15 секунд)."""
    state = awg.load()
    cfg = settings(state)
    tg_on = bool(cfg["token"] and cfg["chat_id"])

    if tg_on and cfg["alerts"]:
        for ev in events:
            _safe(lambda: send(event_text(state, ev), cfg), log)
        # Туннель: тревога после двух неудачных проверок подряд (~30 с), чтобы не шуметь.
        _tunnel["fails"] = 0 if tunnel_up else _tunnel["fails"] + 1
        if _tunnel["fails"] >= 2 and not _tunnel["down"]:
            _tunnel["down"] = True
            _safe(lambda: send(f"🔴 <b>Туннель {_h(state['server']['iface'])} не отвечает</b>\n"
                               f"{_h(server_title(state))}\nКлиенты не могут подключиться.", cfg), log)
        elif tunnel_up and _tunnel["down"]:
            _tunnel["down"] = False
            _safe(lambda: send(f"🟢 Туннель {_h(state['server']['iface'])} снова работает\n"
                               f"{_h(server_title(state))}", cfg), log)

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
    elif a.cmd == "fixup":
        for m in fixup_after_restore(a.public_ip, a.wan):
            print(f"  {m}")


if __name__ == "__main__":
    main()
