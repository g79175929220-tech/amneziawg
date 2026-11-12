"""XRay (VLESS + Reality, TCP) на сервере панели — для мобильных сетей, где режут UDP.

Те же клиенты, что и у AmneziaWG: у каждого свой UUID (c["xray_id"]), отключение, срок и
лимит действуют и здесь, трафик XRay идёт в общий учёт. Настройки — state["xray"]:
  enabled, port (TCP), sni (сайт для маскировки), link (адрес в ссылках, например вход
  каскада 1xcvet.ru:8443; пусто — endpoint сервера и port), private_key, public_key, short_id.

Пользователи добавляются и убираются через API XRay на лету — подключённые клиенты
не обрываются; перезапуск только при смене порта/ключей/маскировки.
"""
import json
import os
import subprocess
import tempfile
import urllib.parse
import uuid

BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
DIR = os.environ.get("XRAY_DIR", "/usr/local/etc/xray")
CFG = os.path.join(DIR, "config.json")
API = os.environ.get("XRAY_API", "127.0.0.1:10085")
SERVICE = os.environ.get("XRAY_SERVICE", "xray")
TAG = "vless-reality"
FLOW = "xtls-rprx-vision"
# Safari, а не Chrome: приветствие Chrome (с постквантовым ключом, ~1.8 КБ) не влезает в один
# TCP-пакет, и фильтры провайдеров теряют его продолжение — подключение не проходит.
FINGERPRINT = "safari"
DEFAULT_SNI = "www.apple.com"
TIMEOUT = 15


def installed():
    return os.access(BIN, os.X_OK)


def settings(state):
    return state.get("xray") or {}


def enabled(state):
    return bool(settings(state).get("enabled")) and installed()


def _run(args, inp=None):
    res = subprocess.run(args, input=inp, capture_output=True, text=True, timeout=TIMEOUT)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-300:] or f"{args[0]}: код {res.returncode}")
    return res.stdout


def gen_keys():
    """(private, public) — пара x25519 для Reality. Вывод разных версий XRay:
    «Private key/Public key» и «PrivateKey/Password (PublicKey)»."""
    priv = pub = ""
    for line in _run([BIN, "x25519"]).splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower(), v.strip()
        if "private" in k:
            priv = v
        elif "public" in k or k.startswith("password"):
            pub = pub or v
    if not (priv and pub):
        raise RuntimeError("xray x25519: не удалось получить ключи")
    return priv, pub


def _adopt_existing():
    """Ключи XRay, поставленного раньше вручную: прежние ссылки клиентов продолжат работать."""
    try:
        with open(CFG) as f:
            rs = json.load(f)["inbounds"][0]["streamSettings"]["realitySettings"]
        out = {"private_key": rs["privateKey"], "short_id": rs["shortIds"][0],
               "sni": rs["serverNames"][0]}
        with open(os.path.join(DIR, "public.key")) as f:
            out["public_key"] = f.read().strip()
        return out if all(out.values()) else {}
    except (OSError, ValueError, KeyError, IndexError):
        return {}


def ensure_keys(x):
    """Ключи Reality в настройках x (state["xray"]) — создать при первом включении."""
    if x.get("private_key") and x.get("public_key") and x.get("short_id"):
        return False
    old = _adopt_existing()
    if old:
        x.update(old)
    else:
        x["private_key"], x["public_key"] = gen_keys()
        x["short_id"] = os.urandom(8).hex()
    x.setdefault("sni", DEFAULT_SNI)
    return True


def ensure_ids(state):
    """UUID XRay у каждого клиента. True — что-то добавили (state нужно сохранить)."""
    changed = False
    for c in state["clients"]:
        if not c.get("xray_id"):
            c["xray_id"] = str(uuid.uuid4())
            changed = True
    return changed


def users(state):
    return [{"id": c["xray_id"], "email": c["id"], "flow": FLOW}
            for c in state["clients"] if c["enabled"] and c.get("xray_id")]


def _inbound(x, clients):
    return {"tag": TAG, "listen": "0.0.0.0", "port": int(x["port"]), "protocol": "vless",
            "settings": {"clients": clients, "decryption": "none"}}


def config(state):
    x = settings(state)
    sni = x.get("sni") or DEFAULT_SNI
    inb = _inbound(x, users(state))
    inb["streamSettings"] = {"network": "tcp", "security": "reality", "realitySettings": {
        "dest": f"{sni}:443", "serverNames": [sni],
        "privateKey": x["private_key"], "shortIds": [x["short_id"]]}}
    inb["sniffing"] = {"enabled": True, "destOverride": ["http", "tls", "quic"]}
    return {
        "log": {"loglevel": "warning", "access": "none"},
        "api": {"tag": "api", "listen": API, "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
        "inbounds": [inb],
        "outbounds": [{"protocol": "freedom", "tag": "direct"},
                      {"protocol": "blackhole", "tag": "block"}],
        "routing": {"rules": [{"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"}]},
    }


def link_address(state):
    x = settings(state)
    return x.get("link") or f"{state['server']['endpoint']}:{x.get('port', 443)}"


def link(state, c, title=None):
    """vless:// ссылка клиента — её понимают AmneziaVPN, v2RayTun, Hiddify, v2rayNG."""
    x = settings(state)
    host, _, port = link_address(state).rpartition(":")
    q = urllib.parse.urlencode({
        "type": "tcp", "security": "reality", "encryption": "none", "flow": FLOW,
        "pbk": x["public_key"], "fp": FINGERPRINT, "sni": x.get("sni") or DEFAULT_SNI,
        "sid": x["short_id"]})
    name = urllib.parse.quote(title or c.get("config_name") or c["name"], safe="")
    return f"vless://{c['xray_id']}@{host}:{port}?{q}#{name}"


def _active():
    return subprocess.run(["systemctl", "is-active", "-q", SERVICE]).returncode == 0


def _write(cfg):
    os.makedirs(DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DIR, suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        _run([BIN, "run", "-test", "-c", tmp])
    except Exception:
        os.unlink(tmp)
        raise
    # Сервис работает от nobody: ему нужен доступ на чтение (в файле приватный ключ Reality).
    try:
        import grp
        os.chown(tmp, 0, grp.getgrnam("nogroup").gr_gid)
        os.chmod(tmp, 0o640)
        os.chown(DIR, 0, grp.getgrnam("nogroup").gr_gid)
        os.chmod(DIR, 0o750)
    except (KeyError, PermissionError):
        os.chmod(tmp, 0o644)
    os.replace(tmp, CFG)


def _load_current():
    try:
        with open(CFG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _live_update(old_users, new_users, x):
    """Добавить/убрать пользователей в работающем XRay через API."""
    old = {u["email"]: u for u in old_users}
    new = {u["email"]: u for u in new_users}
    gone = [e for e in old if e not in new or old[e]["id"] != new[e]["id"]]
    added = [u for e, u in new.items() if e not in old or old[e]["id"] != u["id"]]
    if gone:
        _run([BIN, "api", "rmu", f"--server={API}", f"-tag={TAG}", *gone])
    if added:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"inbounds": [_inbound(x, added)]}, f)
            tmp = f.name
        try:
            out = _run([BIN, "api", "adu", f"--server={API}", tmp])
        finally:
            os.unlink(tmp)
        if f"Added {len(added)} user" not in out:
            raise RuntimeError(f"xray api adu: {out.strip()[-200:]}")


def sync(state, log=print):
    """Привести XRay в соответствие со state (вызывается из awg.apply)."""
    if not enabled(state):
        if installed() and _active():
            subprocess.run(["systemctl", "disable", "--now", SERVICE], capture_output=True)
        return
    x = settings(state)
    new = config(state)
    cur = _load_current()
    if cur == new and _active():
        return

    def same_but_users(a, b):
        """Отличаются только пользователи — тогда хватит API, без перезапуска."""
        try:
            a, b = json.loads(json.dumps(a)), json.loads(json.dumps(b))
            a["inbounds"][0]["settings"]["clients"] = b["inbounds"][0]["settings"]["clients"] = []
            return a == b
        except (KeyError, IndexError, TypeError):      # чужой конфиг другого вида
            return False

    if cur and cur.get("api", {}).get("listen") != API and not os.path.exists(CFG + ".before-panel"):
        # Конфиг XRay, поставленного не панелью, — сохранить перед заменой.
        with open(CFG) as src, open(CFG + ".before-panel", "w") as dst:
            dst.write(src.read())
        os.chmod(CFG + ".before-panel", 0o600)
    _write(new)
    if cur and _active() and same_but_users(cur, new):
        try:
            _live_update(cur["inbounds"][0]["settings"]["clients"], users(state), x)
            return
        except Exception as e:  # noqa: BLE001
            log(f"XRay: не удалось обновить пользователей на лету ({e}) — перезапуск")
    subprocess.run(["systemctl", "enable", SERVICE], capture_output=True)
    _run(["systemctl", "restart", SERVICE])


def stats():
    """{email (= id клиента): {"rx": от клиента, "tx": к клиенту}} с момента запуска XRay
    или None, если XRay не отвечает."""
    try:
        out = _run([BIN, "api", "statsquery", f"--server={API}", "-pattern", "user>>>"])
    except Exception:  # noqa: BLE001
        return None
    res = {}
    for st in (json.loads(out or "{}").get("stat") or []):
        parts = st.get("name", "").split(">>>")       # user>>>email>>>traffic>>>uplink
        if len(parts) == 4 and parts[0] == "user":
            d = res.setdefault(parts[1], {"rx": 0, "tx": 0})
            d["rx" if parts[3] == "uplink" else "tx"] = int(st.get("value", 0))
    return res


def port_busy(port):
    """Занят ли TCP-порт кем-то, кроме самого XRay."""
    res = subprocess.run(["ss", "-Htlnp", f"sport = :{int(port)}"], capture_output=True, text=True)
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return bool(lines) and not all('"xray"' in ln for ln in lines)
