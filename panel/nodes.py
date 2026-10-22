"""Дополнительные серверы AmneziaWG, которыми управляет эта панель.

На дополнительном сервере нет своей панели — только AmneziaWG 3.1 и маленький агент
(`install.sh --node`). Панель ходит к нему по SSH ключом /etc/awg-panel/node_ed25519,
а на сервере этот ключ ограничен принудительной командой: им можно только вызвать агент
(info / dump / apply), но не получить shell.

Одно SSH-соединение на сервер держится открытым (ControlMaster), поэтому опрос
счётчиков раз в несколько секунд почти ничего не стоит.
"""
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import uuid

import awg

KEY = os.path.join(awg.STATE_DIR, "node_ed25519")
KNOWN_HOSTS = os.path.join(awg.STATE_DIR, "known_hosts")
CTL_DIR = os.environ.get("AWG_NODE_CTL_DIR", "/run/awg-panel")
# Подмена транспорта для тестов: команда, которой передаётся (host, действие).
SSH_CMD = os.environ.get("AWG_NODE_SSH")
TIMEOUT = 15
DUMP_TTL = 3            # секунд: список клиентов в браузере обновляется каждую секунду
RETRY = 30              # недоступный сервер не опрашиваем чаще — чтобы не тормозить панель

_cache = {}             # node_id -> (время, peers | None, ошибка)
_pushed = {}            # node_id -> хеш последнего отправленного конфига
_errors = {}            # node_id -> текст последней ошибки
_failed = {}            # node_id -> время последней неудачи (monotonic)
_locks = {}
_glock = threading.Lock()


def _lock_for(nid):
    with _glock:
        return _locks.setdefault(nid, threading.Lock())


def ensure_key():
    """Ключ панели для доступа к серверам. Возвращает публичную часть."""
    if not os.path.exists(KEY):
        os.makedirs(awg.STATE_DIR, exist_ok=True)
        awg.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "awg-panel", "-f", KEY])
    with open(KEY + ".pub") as f:
        return f.read().strip()


def public_key():
    try:
        with open(KEY + ".pub") as f:
            return f.read().strip()
    except OSError:
        return ensure_key()


def parse_host(addr):
    """'1.2.3.4', 'host:2222', 'root@host' -> (user@host, порт)."""
    addr = addr.strip()
    m = re.fullmatch(r"(?:([a-z_][a-z0-9_-]*)@)?([A-Za-z0-9.-]+)(?::(\d{1,5}))?", addr)
    if not m:
        raise ValueError("Адрес сервера: IP или домен, при необходимости :порт SSH (например 1.2.3.4:2222)")
    return f"{m.group(1) or 'root'}@{m.group(2)}", int(m.group(3) or 22)


def call(node, action, inp=None, timeout=TIMEOUT):
    """Вызвать агент на сервере: info | dump | apply (конфиг в stdin)."""
    target, port = parse_host(node["ssh"])
    if SSH_CMD:
        cmd = [SSH_CMD, target, action]
    else:
        ensure_key()
        os.makedirs(CTL_DIR, mode=0o700, exist_ok=True)
        cmd = ["ssh", "-i", KEY, "-p", str(port),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
               "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
               "-o", "ControlMaster=auto", "-o", f"ControlPath={CTL_DIR}/%C", "-o", "ControlPersist=300",
               target, action]
    try:
        res = subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"сервер {node['ssh']} не ответил за {timeout} с") from None
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip().splitlines()
        err = err[-1] if err else f"код {res.returncode}"
        if "Permission denied" in err:
            err = "нет доступа по ключу панели — на сервере не выполнен install.sh --node с этим ключом"
        raise RuntimeError(f"{node['ssh']}: {err}")
    return res.stdout


def info(addr):
    """Сведения о новом сервере (интерфейс, порт, внешний интерфейс, IP, занятые сети)."""
    out = call({"ssh": addr}, "info")
    try:
        data = json.loads(out)
    except ValueError:
        raise RuntimeError(f"{addr}: неожиданный ответ агента") from None
    for k in ("iface", "port", "wan"):
        if not data.get(k):
            raise RuntimeError(f"{addr}: агент не сообщил {k}")
    return data


def push(state, node, force=False):
    """Отправить конфиг сервера с его клиентами; ничего не делает, если он не менялся."""
    conf = awg.server_conf(state, node["id"])
    h = hashlib.sha256(conf.encode()).hexdigest()
    with _lock_for(node["id"]):
        if not force and _pushed.get(node["id"]) == h:
            return False
        if not force and node["id"] in _failed and time.monotonic() - _failed[node["id"]] < RETRY:
            _pushed.pop(node["id"], None)      # отправим, когда сервер снова ответит
            return False
        try:
            call(node, "apply", inp=conf, timeout=60)
        except Exception as e:  # noqa: BLE001
            _errors[node["id"]] = str(e)
            _failed[node["id"]] = time.monotonic()
            _pushed.pop(node["id"], None)
            raise
        _pushed[node["id"]] = h
        _failed.pop(node["id"], None)
        _errors.pop(node["id"], None)
        _cache.pop(node["id"], None)
        return True


def push_all(state, log=None):
    """Разослать конфиги всем серверам. Недоступный сервер не мешает остальным
    (повтор — из фоновой проверки)."""
    for n in state.get("nodes", []):
        try:
            push(state, n)
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"сервер {n['name']}: {e}")


def dump(node, max_age=DUMP_TTL):
    """Пиры сервера {public_key: {...}} или None, если он недоступен."""
    def fresh(hit):
        return hit and time.monotonic() - hit[0] < (max_age if hit[1] is not None else max(max_age, RETRY))
    if fresh(_cache.get(node["id"])):
        return _cache[node["id"]][1]
    with _lock_for(node["id"]):
        if fresh(_cache.get(node["id"])):
            return _cache[node["id"]][1]
        try:
            peers = awg.parse_dump(call(node, "dump", timeout=10))
            _errors.pop(node["id"], None)
            _failed.pop(node["id"], None)
        except Exception as e:  # noqa: BLE001
            peers = None
            _errors[node["id"]] = str(e)
            _failed[node["id"]] = time.monotonic()
            _pushed.pop(node["id"], None)     # после восстановления — отправить конфиг заново
        _cache[node["id"]] = (time.monotonic(), peers, _errors.get(node["id"]))
        return peers


def pushed(nid):
    return nid in _pushed


def error(nid):
    return _errors.get(nid)


def forget(nid):
    for d in (_cache, _pushed, _errors, _failed):
        d.pop(nid, None)


# ------------------------------------------------------------------ добавление / удаление

def _free_subnet(state, taken_nets):
    """Подсеть для нового сервера: не совпадает с подсетями других серверов панели и не
    пересекается с сетями на самом сервере (Docker и т.п.)."""
    used = [ipaddress.ip_network(state["server"]["subnet"])]
    used += [ipaddress.ip_network(n["subnet"]) for n in state.get("nodes", [])]
    for t in taken_nets:
        try:
            net = ipaddress.ip_network(t, strict=False)
        except ValueError:
            continue
        if net.prefixlen > 0:
            used.append(net)
    for i in range(10, 250):
        net = ipaddress.ip_network(f"10.{i}.0.0/24")
        if not any(net.overlaps(u) for u in used):
            return net
    raise RuntimeError("Нет свободной подсети")


def add_node(name, addr, endpoint=""):
    name = name.strip()[:40] or addr
    parse_host(addr)
    with awg._lock:
        state = awg.load()
        host = parse_host(addr)[0].split("@")[1]
        if any(parse_host(n["ssh"])[0].split("@")[1] == host for n in state.get("nodes", [])):
            raise ValueError(f"Сервер {host} уже добавлен")
    inf = info(addr)                        # по SSH, без блокировки панели
    with awg._lock:
        state = awg.load()
        net = _free_subnet(state, inf.get("nets", []))
        priv, pub = awg.genkey()
        local = state["server"]
        node = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "ssh": addr.strip(),
            "endpoint": endpoint.strip() or inf.get("public_ip") or host,
            "port": int(inf["port"]),
            "iface": inf["iface"],
            "wan": inf["wan"],
            "subnet": str(net),
            "address": f"{next(net.hosts())}/{net.prefixlen}",
            "private_key": priv,
            "public_key": pub,
            "dns": local["dns"],
            "mtu": local.get("mtu", 1280),
            "keepalive": local.get("keepalive", 25),
            "proto": 3,
            "obfs": awg.gen_obfuscation(3),
            "cascade": "",
            "created": int(time.time()),
        }
        state.setdefault("nodes", []).append(node)
        push(state, node, force=True)       # сервер поднимает туннель (пока без клиентов)
        awg.save(state)
        return node


def update_node(nid, fields):
    with awg._lock:
        state = awg.load()
        node = awg.server_by_id(state, nid)
        if any(k in fields and fields[k] != node.get(k) for k in ("endpoint", "port", "cascade")):
            awg.remember_issued(state)
        node.update(fields)
        awg.save(state)
        try:
            push(state, node)
        except Exception:  # noqa: BLE001
            pass                             # повторит фоновая проверка; ошибка видна в настройках
        return node


def delete_node(nid):
    with awg._lock:
        state = awg.load()
        n = sum(1 for c in state["clients"] if awg.node_id(c) == nid)
        if n:
            raise ValueError(f"На сервере остались клиенты ({n}) — сначала перенесите их на другой сервер или удалите")
        state["nodes"] = [x for x in state.get("nodes", []) if x["id"] != nid]
        awg.save(state)
    forget(nid)
