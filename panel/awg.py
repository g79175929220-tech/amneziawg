"""AmneziaWG state management: keys, configs, obfuscation params, live status."""
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
CONF_DIR = os.environ.get("AWG_CONF_DIR", "/etc/amnezia/amneziawg")

_lock = threading.RLock()

# Parameters understood by each protocol generation.
#   1 -> AmneziaWG 1.x: Jc/Jmin/Jmax, S1/S2, H1-H4 (single values)
#   2 -> AmneziaWG 2.x: + S3/S4, H1-H4 as ranges, I1-I5 signature packets
PARAMS_V1 = ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"]
PARAMS_V2 = PARAMS_V1 + ["S3", "S4", "I1", "I2", "I3", "I4", "I5"]


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
    # S1 + 56 must not equal S2, otherwise init and response packets have equal size.
    while True:
        s1, s2 = _rand(15, 150), _rand(15, 150)
        if s1 + 56 != s2:
            break
    p["S1"], p["S2"] = s1, s2
    if proto >= 2:
        p["S3"] = _rand(8, 64)
        p["S4"] = _rand(1, 16)
        # Four non-overlapping header ranges spread over the 32-bit space.
        span = (2**31 - 1000) // 4
        for i in range(4):
            base = 1000 + i * span
            lo = base + secrets.randbelow(span // 2)
            hi = lo + _rand(1000, span // 4)
            p[f"H{i + 1}"] = f"{lo}-{hi}"
        for i in range(1, 6):
            p[f"I{i}"] = ""
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
               proto=2, iface="awg0", mtu=1376):
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


def obfs_lines(srv):
    keys = PARAMS_V2 if srv["proto"] >= 2 else PARAMS_V1
    out = []
    for k in keys:
        v = srv["obfs"].get(k)
        if v is None or v == "":
            continue
        out.append(f"{k} = {v}")
    return out


def server_conf(state):
    s = state["server"]
    sub, wan, ifc = s["subnet"], s["wan"], s["iface"]
    lines = [
        "# Managed by awg-panel. Manual edits will be overwritten.",
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
        *obfs_lines(s),
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
    """Write config and push it to the running interface."""
    with _lock:
        write_conf(state)
        ifc = state["server"]["iface"]
        if restart or not iface_up(state):
            run(["systemctl", "restart", f"awg-quick@{ifc}"])
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


def add_client(name):
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
        }
        state["clients"].append(c)
        save(state)
        apply(state)
        return c


def update_client(cid, **fields):
    with _lock:
        state = load()
        for c in state["clients"]:
            if c["id"] == cid:
                c.update(fields)
                break
        else:
            raise KeyError(cid)
        save(state)
        apply(state)


def delete_client(cid):
    with _lock:
        state = load()
        state["clients"] = [c for c in state["clients"] if c["id"] != cid]
        save(state)
        apply(state)


def status(state):
    """Return {public_key: {...}} parsed from `awg show <iface> dump`."""
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
