"""Управление AmneziaWG: ключи, конфиги, параметры обфускации, текущий статус."""
import fcntl
import hashlib
import ipaddress
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import uuid

STATE_DIR = os.environ.get("AWG_PANEL_DIR", "/etc/awg-panel")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONF_DIR = os.environ.get("AWG_CONF_DIR", "/etc/awg3")
# Шаблон systemd-юнита, поднимающего интерфейс ({iface} подставляется).
SERVICE = os.environ.get("AWG_SERVICE", "awg3-quick@{iface}")

class _StateLock:
    """Блокировка state.json: между потоками (RLock) и между процессами (flock),
    чтобы фоновый учёт трафика и консольные команды не затирали изменения друг друга."""

    def __init__(self):
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._rlock.acquire()
        if self._depth == 0:
            os.makedirs(STATE_DIR, exist_ok=True)
            self._fd = os.open(os.path.join(STATE_DIR, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._rlock.release()


_lock = _StateLock()

# Параметры, которые понимает каждая версия протокола.
#   1 -> AmneziaWG 1.x: Jc/Jmin/Jmax, S1/S2, H1-H4 (одиночные значения)
#   2 -> AmneziaWG 2.x: + S3/S4, H1-H4 как диапазоны, I1-I5 сигнатурные пакеты
PARAMS_V1 = ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"]
#   3 -> AmneziaWG 3.x: + HeaderProtectionKey (шифрование заголовков, S1-S4 >= 12),
#        ContentPaddingAddition, RandomTrailers (3.1), DisableCookies (3.1)
PARAMS_V2 = PARAMS_V1 + ["S3", "S4", "I1", "I2", "I3", "I4", "I5"]
PARAMS_V3 = PARAMS_V2 + ["HeaderProtectionKey", "ContentPaddingAddition",
                         "RandomTrailers", "DisableCookies"]
# Параметры только для сервера — в клиентский конфиг не попадают.
SERVER_ONLY = {"DisableCookies"}


def params_for(proto):
    return {1: PARAMS_V1, 2: PARAMS_V2}.get(proto, PARAMS_V3)


def run(cmd, inp=None, check=True):
    res = subprocess.run(cmd, input=inp, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def genkey():
    priv = run(["awg", "genkey"])
    pub = run(["awg", "pubkey"], inp=priv + "\n")
    return priv, pub


def genpsk():
    return run(["awg", "genpsk"])


def _rand(lo, hi):
    return lo + secrets.randbelow(hi - lo + 1)


def gen_obfuscation(proto):
    jmin = _rand(40, 80)
    p = {"Jc": _rand(4, 10), "Jmin": jmin, "Jmax": _rand(jmin + 200, 1000)}
    # S1 + 56 не должно равняться S2, иначе пакеты init и response будут одного размера.
    # Для защиты заголовков в 3.x все S1-S4 должны быть не меньше 12.
    while True:
        s1, s2 = _rand(15, 150), _rand(15, 150)
        if s1 + 56 != s2:
            break
    p["S1"], p["S2"] = s1, s2
    if proto >= 2:
        p["S3"] = _rand(12 if proto >= 3 else 8, 64)
        p["S4"] = _rand(12, 32) if proto >= 3 else _rand(1, 16)
        # Четыре непересекающихся диапазона заголовков в 32-битном пространстве.
        span = (2**31 - 1000) // 4
        for i in range(4):
            base = 1000 + i * span
            lo = base + secrets.randbelow(span // 2)
            hi = lo + _rand(1000, span // 4)
            p[f"H{i + 1}"] = f"{lo}-{hi}"
        for i in range(1, 6):
            p[f"I{i}"] = ""
        if proto >= 3:
            p["HeaderProtectionKey"] = genkey()[0]
            p["ContentPaddingAddition"] = "0-64"
            p["RandomTrailers"] = "on"
            p["DisableCookies"] = "off"
    else:
        hs = set()
        while len(hs) < 4:
            hs.add(_rand(5, 2**31 - 1))
        for i, h in enumerate(sorted(hs)):
            p[f"H{i + 1}"] = h
    return p


def load():
    with _lock:
        with open(STATE_FILE) as f:
            return json.load(f)


def save(state):
    with _lock:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)


def init_state(endpoint, port, wan, subnet="10.8.0.0/24", dns="1.1.1.1, 1.0.0.1",
               proto=3, iface="awg3", mtu=1376):
    net = ipaddress.ip_network(subnet, strict=False)
    priv, pub = genkey()
    state = {
        "server": {
            "iface": iface,
            "private_key": priv,
            "public_key": pub,
            "subnet": str(net),
            "address": f"{next(net.hosts())}/{net.prefixlen}",
            "port": int(port),
            "endpoint": endpoint,
            "wan": wan,
            "dns": dns,
            "mtu": int(mtu),
            "keepalive": 25,
            "proto": int(proto),
            "obfs": gen_obfuscation(int(proto)),
        },
        "clients": [],
    }
    save(state)
    return state


def obfs_lines(srv, client=False):
    out = []
    for k in params_for(srv["proto"]):
        if client and k in SERVER_ONLY:
            continue
        v = srv["obfs"].get(k)
        if v is None or v == "":
            continue
        out.append(f"{k} = {v}")
    return out


def server_conf(state):
    s = state["server"]
    sub, wan, ifc = s["subnet"], s["wan"], s["iface"]
    lines = [
        "# Управляется awg-panel. Ручные правки будут перезаписаны.",
        "[Interface]",
        f"PrivateKey = {s['private_key']}",
        f"Address = {s['address']}",
        f"ListenPort = {s['port']}",
        f"MTU = {s['mtu']}",
        *obfs_lines(s),
        f"PostUp = iptables -t nat -A POSTROUTING -s {sub} -o {wan} -j MASQUERADE; "
        f"iptables -A FORWARD -i {ifc} -j ACCEPT; iptables -A FORWARD -o {ifc} -j ACCEPT",
        f"PostDown = iptables -t nat -D POSTROUTING -s {sub} -o {wan} -j MASQUERADE; "
        f"iptables -D FORWARD -i {ifc} -j ACCEPT; iptables -D FORWARD -o {ifc} -j ACCEPT",
    ]
    for c in state["clients"]:
        if not c["enabled"]:
            continue
        lines += ["", f"# {c['name']}", "[Peer]",
                  f"PublicKey = {c['public_key']}",
                  f"PresharedKey = {c['psk']}",
                  f"AllowedIPs = {c['ip']}/32"]
    return "\n".join(lines) + "\n"


def client_conf(state, c):
    s = state["server"]
    lines = [
        "[Interface]",
        f"PrivateKey = {c['private_key']}",
        f"Address = {c['ip']}/32",
        f"DNS = {s['dns']}",
        f"MTU = {s['mtu']}",
        *obfs_lines(s, client=True),
        "",
        "[Peer]",
        f"PublicKey = {s['public_key']}",
        f"PresharedKey = {c['psk']}",
        f"Endpoint = {s['endpoint']}:{s['port']}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"PersistentKeepalive = {s['keepalive']}",
    ]
    return "\n".join(lines) + "\n"


def conf_path(state):
    return os.path.join(CONF_DIR, f"{state['server']['iface']}.conf")


def write_conf(state):
    os.makedirs(CONF_DIR, exist_ok=True)
    path = conf_path(state)
    fd, tmp = tempfile.mkstemp(dir=CONF_DIR)
    with os.fdopen(fd, "w") as f:
        f.write(server_conf(state))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def iface_up(state):
    return subprocess.run(["awg", "show", state["server"]["iface"]],
                          capture_output=True).returncode == 0


def apply(state, restart=False):
    """Записать конфиг и применить его к работающему интерфейсу."""
    with _lock:
        write_conf(state)
        ifc = state["server"]["iface"]
        if restart or not iface_up(state):
            run(["systemctl", "restart", SERVICE.format(iface=ifc)])
            return
        stripped = run(["awg-quick", "strip", conf_path(state)])
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(stripped + "\n")
            tmp = f.name
        try:
            run(["awg", "syncconf", ifc, tmp])
        finally:
            os.unlink(tmp)


def next_ip(state):
    net = ipaddress.ip_network(state["server"]["subnet"])
    used = {state["server"]["address"].split("/")[0]} | {c["ip"] for c in state["clients"]}
    for h in net.hosts():
        if str(h) not in used:
            return str(h)
    raise RuntimeError("Подсеть заполнена")


def add_client(name, expires=None, limit_bytes=None, limit_period="total"):
    with _lock:
        state = load()
        priv, pub = genkey()
        c = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "private_key": priv,
            "public_key": pub,
            "psk": genpsk(),
            "ip": next_ip(state),
            "enabled": True,
            "created": int(time.time()),
            "expires": expires,
            "limit_bytes": limit_bytes,
            "limit_period": limit_period,
        }
        state["clients"].append(c)
        save(state)
        apply(state)
        return c


# ------------------------------------------------------------ учёт трафика
# Счётчики `awg show` обнуляются при перезапуске интерфейса и при отключении
# клиента (пир удаляется), поэтому трафик копится в state: usage += прирост.

def _period():
    return time.strftime("%Y-%m")


def _delta(cur, last):
    return cur - last if cur >= last else cur      # счётчик сбросился


def live_usage(c, live):
    """Накопленный трафик клиента с учётом ещё не сохранённого прироста."""
    u = c.get("usage", {"rx": 0, "tx": 0})
    last = c.get("last", {"rx": 0, "tx": 0})
    st = (live or {}).get(c["public_key"])
    if not st:
        return u["rx"], u["tx"]
    return u["rx"] + _delta(st["rx"], last["rx"]), u["tx"] + _delta(st["tx"], last["tx"])


def over_limit(c):
    u = c.get("usage", {"rx": 0, "tx": 0})
    return bool(c.get("limit_bytes")) and u["rx"] + u["tx"] >= c["limit_bytes"]


def _account(state, live):
    """Перенести прирост счётчиков в usage, применить срок и лимит.
    Возвращает True, если поменялся набор включённых клиентов."""
    now = time.time()
    period = _period()
    changed = False
    for c in state["clients"]:
        u = c.setdefault("usage", {"rx": 0, "tx": 0})
        if live is not None:
            st = live.get(c["public_key"])
            last = c.get("last", {"rx": 0, "tx": 0})
            if st:
                u["rx"] += _delta(st["rx"], last["rx"])
                u["tx"] += _delta(st["tx"], last["tx"])
                c["last"] = {"rx": st["rx"], "tx": st["tx"]}
            else:
                c["last"] = {"rx": 0, "tx": 0}
        # Месячный лимит: в новом месяце счётчик обнуляется.
        if c.get("limit_period") == "month" and c.get("period") != period:
            if c.get("period"):
                c["usage"] = u = {"rx": 0, "tx": 0}
                if not c["enabled"] and c.get("disabled_reason") == "limit":
                    c["enabled"], c["disabled_reason"] = True, None
                    changed = True
            c["period"] = period
        if c["enabled"] and c.get("expires") and c["expires"] < now:
            c["enabled"], c["disabled_reason"] = False, "expired"
            changed = True
        elif c["enabled"] and over_limit(c):
            c["enabled"], c["disabled_reason"] = False, "limit"
            changed = True
    return changed


def check():
    """Периодическая проверка: учёт трафика, сроки, лимиты."""
    with _lock:
        state = load()
        changed = _account(state, status(state))
        save(state)
        if changed:
            apply(state)


def update_client(cid, **fields):
    with _lock:
        state = load()
        # Сначала учесть трафик: при отключении пир удаляется вместе со счётчиками.
        _account(state, status(state))
        for c in state["clients"]:
            if c["id"] == cid:
                if fields.pop("reset_usage", False):
                    c["usage"] = {"rx": 0, "tx": 0}
                c.update(fields)
                if "enabled" in fields:
                    c["disabled_reason"] = None if c["enabled"] else "manual"
                elif (not c["enabled"] and c.get("disabled_reason") in ("expired", "limit")
                      and not over_limit(c)
                      and not (c.get("expires") and c["expires"] < time.time())):
                    # Ограничение сняли (продлили срок, подняли лимит, сбросили счётчик).
                    c["enabled"], c["disabled_reason"] = True, None
                # Отказываем только при явном включении; если лимит или срок просто
                # ужесточили, клиент отключится ниже в _account().
                if fields.get("enabled") and over_limit(c):
                    raise ValueError("Лимит трафика исчерпан — увеличьте лимит или сбросьте счётчик")
                if fields.get("enabled") and c.get("expires") and c["expires"] < time.time():
                    raise ValueError("Срок действия истёк — продлите его")
                break
        else:
            raise KeyError(cid)
        _account(state, None)
        save(state)
        apply(state)


def delete_client(cid):
    with _lock:
        state = load()
        state["clients"] = [c for c in state["clients"] if c["id"] != cid]
        state["shares"] = {k: v for k, v in state.get("shares", {}).items() if v["cid"] != cid}
        save(state)
        apply(state)


# ------------------------------------------------------------ одноразовые ссылки
# В state хранится только SHA-256 токена, сам токен знает лишь получатель ссылки.

def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_share(cid, ttl):
    with _lock:
        state = load()
        if not any(c["id"] == cid for c in state["clients"]):
            raise KeyError(cid)
        now = int(time.time())
        # Новая ссылка отменяет прежние для этого клиента; заодно чистим просроченные.
        shares = {k: v for k, v in state.get("shares", {}).items()
                  if v["cid"] != cid and v["expires"] > now}
        token = secrets.token_urlsafe(24)
        shares[_token_hash(token)] = {"cid": cid, "created": now, "expires": now + int(ttl)}
        state["shares"] = shares
        save(state)
        return token, now + int(ttl)


def share_info(token):
    """Клиент по действующей ссылке (без погашения) или None."""
    state = load()
    sh = state.get("shares", {}).get(_token_hash(token))
    if not sh or sh["expires"] < time.time():
        return None
    return next((c for c in state["clients"] if c["id"] == sh["cid"]), None)


def redeem_share(token):
    """Погасить ссылку и вернуть (state, client) либо None."""
    with _lock:
        state = load()
        sh = state.get("shares", {}).pop(_token_hash(token), None)
        if not sh or sh["expires"] < time.time():
            return None
        save(state)
        c = next((c for c in state["clients"] if c["id"] == sh["cid"]), None)
        return (state, c) if c else None


def active_shares(state):
    now = time.time()
    return {v["cid"]: v["expires"] for v in state.get("shares", {}).values() if v["expires"] > now}


def status(state):
    """Вернуть {public_key: {...}} из вывода `awg show <iface> dump`."""
    res = subprocess.run(["awg", "show", state["server"]["iface"], "dump"],
                         capture_output=True, text=True)
    peers = {}
    if res.returncode != 0:
        return None
    for line in res.stdout.strip().splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 8:
            continue
        peers[f[0]] = {
            "endpoint": "" if f[2] == "(none)" else f[2],
            "handshake": int(f[4]),
            "rx": int(f[5]),
            "tx": int(f[6]),
        }
    return peers
