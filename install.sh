#!/usr/bin/env bash
# Установщик сервера AmneziaWG 3.1 и веб-панели (Ubuntu 20.04+ / Debian 11+).
# Запуск: sudo bash install.sh [--port 51820] [--panel-port 8443] [--endpoint IP]
#                              [--proto 3|2|1] [--iface awg3] [--subnet 10.9.0.0/24]
#
# AmneziaWG 3.1 ставится изолированно в /opt/awg3 (userspace amneziawg-go + awg-tools 3.1),
# конфиги — в /etc/awg3, юнит — awg3-quick@. Системные awg/awg-quick, модуль ядра и уже
# работающие туннели (например, AmneziaWG 2.0 на хосте или в Docker) не затрагиваются.
# Если интерфейс, порт или подсеть заняты, скрипт сам подберёт свободные.
set -euo pipefail

AWG_GO_TAG=v3.1.20260828
AWG_TOOLS_TAG=v3.1.20260812
PREFIX=/opt/awg3
CONF_DIR=/etc/awg3

AWG_PORT=51820
PANEL_PORT=8443
ENDPOINT=""
PROTO=3
IFACE=""
SUBNET=""
USER_PORT=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) AWG_PORT="$2"; USER_PORT=1; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --proto) PROTO="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --subnet) SUBNET="$2"; shift 2 ;;
    *) echo "Неизвестный параметр: $1"; exit 1 ;;
  esac
done

log() { echo -e "\e[1;32m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
[[ $EUID -eq 0 ]] || { echo "Запустите от имени root"; exit 1; }
[[ -c /dev/net/tun ]] || { echo "Нет /dev/net/tun — включите TUN/TAP в панели хостинга"; exit 1; }

export DEBIAN_FRONTEND=noninteractive
STATE=/etc/awg-panel/state.json

log "Установка базовых пакетов"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl iptables iproute2 openssl \
  python3 python3-venv python3-pip git make gcc libc6-dev

# ---------------------------------------------------------------- AmneziaWG 3.1
install_go() {
  if command -v go >/dev/null && go version | grep -qE 'go1\.(2[5-9]|[3-9][0-9])'; then return; fi
  if [[ -x /usr/local/go/bin/go ]] && /usr/local/go/bin/go version | grep -qE 'go1\.(2[5-9]|[3-9][0-9])'; then
    export PATH=/usr/local/go/bin:$PATH; return
  fi
  log "Установка Go (нужен для сборки amneziawg-go)"
  local arch gover
  arch=$(dpkg --print-architecture)
  gover=$(curl -fsSL "https://go.dev/VERSION?m=text" | head -1)
  rm -rf /usr/local/go
  curl -fsSL "https://go.dev/dl/${gover}.linux-${arch}.tar.gz" | tar -C /usr/local -xz
  export PATH=/usr/local/go/bin:$PATH
}

build_awg3() {
  local build; build=$(mktemp -d)
  install_go
  log "Сборка amneziawg-go $AWG_GO_TAG"
  git clone -q --depth 1 -b "$AWG_GO_TAG" https://github.com/amnezia-vpn/amneziawg-go "$build/go"
  make -C "$build/go" >/dev/null
  log "Сборка amneziawg-tools $AWG_TOOLS_TAG"
  git clone -q --depth 1 -b "$AWG_TOOLS_TAG" https://github.com/amnezia-vpn/amneziawg-tools "$build/tools"
  make -C "$build/tools/src" >/dev/null

  install -d "$PREFIX/bin"
  install -m 0755 "$build/go/amneziawg-go" "$PREFIX/bin/amneziawg-go"
  install -m 0755 "$build/tools/src/wg" "$PREFIX/bin/awg"
  install -m 0755 "$build/tools/src/wg-quick/linux.bash" "$PREFIX/bin/awg-quick"
  # Всегда используем userspace amneziawg-go 3.1: модуль ядра на хосте может быть
  # старой версии (2.0) и не понимает параметры 3.x.
  python3 - "$PREFIX/bin/awg-quick" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s, n = re.subn(r"add_if\(\) \{.*?\n\}\n",
               'add_if() {\n\tcmd amneziawg-go "$INTERFACE"\n}\n', s, count=1, flags=re.S)
if n != 1:
    sys.exit("не удалось пропатчить awg-quick")
open(p, "w").write(s)
PY
  echo "$AWG_GO_TAG $AWG_TOOLS_TAG" > "$PREFIX/VERSION"
  rm -rf "$build"
}

if [[ "$(cat "$PREFIX/VERSION" 2>/dev/null)" == "$AWG_GO_TAG $AWG_TOOLS_TAG" ]]; then
  log "AmneziaWG 3.1 уже собран в $PREFIX"
else
  build_awg3
fi
"$PREFIX/bin/awg" --version || true

cat > /etc/systemd/system/awg3-quick@.service <<UNIT
[Unit]
Description=AmneziaWG 3.1 (userspace) для %I
After=network-online.target nss-lookup.target
Wants=network-online.target nss-lookup.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PATH=$PREFIX/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=WG_ENDPOINT_RESOLUTION_RETRIES=infinity
ExecStart=$PREFIX/bin/awg-quick up $CONF_DIR/%i.conf
ExecStop=$PREFIX/bin/awg-quick down $CONF_DIR/%i.conf

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

# ---------------------------------------------------- соседство с другими туннелями
udp_busy() { ss -Huln "sport = :$1" | grep -q .; }
tcp_busy() { ss -Htln "sport = :$1" | grep -q .; }
iface_taken() {
  ip link show "$1" >/dev/null 2>&1 || [[ -f "/etc/amnezia/amneziawg/$1.conf" ]] \
    || [[ -f "/etc/wireguard/$1.conf" ]]
}

log "Проверка, что не мешаем существующим туннелям"
if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qi amnezia; then
  warn "Найдены Docker-контейнеры Amnezia: $(docker ps --format '{{.Names}}' | grep -i amnezia | tr '\n' ' ')— они продолжат работать"
fi
for ifc in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^(awg|wg|amn)' || true); do
  warn "Уже есть интерфейс $ifc — его не трогаю"
done

if [[ -f "$STATE" ]]; then
  # Повторный запуск: оставляем прежние интерфейс/порт/подсеть.
  IFACE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["iface"])' "$STATE")
else
  if [[ -z "$IFACE" ]]; then
    i=3; while iface_taken "awg$i"; do i=$((i+1)); done
    IFACE="awg$i"
  elif iface_taken "$IFACE"; then
    echo "Интерфейс $IFACE уже занят другим туннелем. Укажите другой, например --iface awg4"; exit 1
  fi
  if udp_busy "$AWG_PORT"; then
    if [[ $USER_PORT == 1 ]]; then echo "UDP-порт $AWG_PORT уже занят"; exit 1; fi
    while udp_busy "$AWG_PORT"; do AWG_PORT=$((AWG_PORT+1)); done
    warn "UDP-порт занят, беру свободный: $AWG_PORT"
  fi
  # Подсеть, не пересекающаяся с адресами и маршрутами сервера.
  SUBNET=$(python3 - "$SUBNET" <<'PY'
import ipaddress, subprocess, sys
used = []
for cmd in (["ip", "-4", "-o", "addr"], ["ip", "-4", "route"]):
    for tok in subprocess.run(cmd, capture_output=True, text=True).stdout.split():
        try:
            used.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            pass
used = [n for n in used if n.prefixlen > 0]
want = sys.argv[1]
cands = [want] if want else [f"10.{i}.0.0/24" for i in range(9, 250)]
for c in cands:
    net = ipaddress.ip_network(c, strict=False)
    if not any(net.overlaps(u) for u in used):
        print(net); break
else:
    sys.exit("Подсеть %s пересекается с существующей" % want if want else "Нет свободной подсети")
PY
)
fi
if ! systemctl is-active -q awg-panel; then
  while tcp_busy "$PANEL_PORT"; do PANEL_PORT=$((PANEL_PORT+1)); done
fi

log "Включение пересылки IP-пакетов"
cat > /etc/sysctl.d/99-amneziawg.conf <<SYSCTL
net.ipv4.ip_forward = 1
SYSCTL
sysctl --system >/dev/null

WAN=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[[ -n "$ENDPOINT" ]] || ENDPOINT=$(curl -4 -fsS --max-time 5 https://api.ipify.org || ip -4 addr show "$WAN" | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
log "Внешний интерфейс: $WAN, адрес сервера: $ENDPOINT"

# ------------------------------------------------------------------------ панель
log "Установка панели в /opt/awg-panel"
install -d /opt/awg-panel /etc/awg-panel "$CONF_DIR"
chmod 700 /etc/awg-panel "$CONF_DIR"
cp -r "$SRC_DIR/panel/." /opt/awg-panel/
python3 -m venv /opt/awg-panel/venv
/opt/awg-panel/venv/bin/pip install -q --upgrade pip
/opt/awg-panel/venv/bin/pip install -q -r /opt/awg-panel/requirements.txt

export PATH="$PREFIX/bin:$PATH" AWG_CONF_DIR="$CONF_DIR" AWG_SERVICE="awg3-quick@{iface}"
PANEL_PY="/opt/awg-panel/venv/bin/python /opt/awg-panel/app.py"

$PANEL_PY init --endpoint "$ENDPOINT" --port "$AWG_PORT" --wan "$WAN" --proto "$PROTO" \
  --iface "$IFACE" ${SUBNET:+--subnet "$SUBNET"}
AWG_PORT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["port"])' "$STATE")
SUBNET=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["subnet"])' "$STATE")
PROTO=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["server"]["proto"])' "$STATE")

log "Запуск awg3-quick@$IFACE"
systemctl enable "awg3-quick@$IFACE" >/dev/null 2>&1
if ! systemctl restart "awg3-quick@$IFACE"; then
  journalctl -u "awg3-quick@$IFACE" --no-pager -n 30; exit 1
fi

if [[ ! -f /etc/awg-panel/panel.json ]]; then
  PANEL_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  $PANEL_PY set-password --username admin "$PANEL_PASS"
else
  PANEL_PASS="(не изменён — используйте прежний)"
fi

if [[ ! -f /etc/awg-panel/tls.crt ]]; then
  log "Генерация самоподписанного TLS-сертификата"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$ENDPOINT" \
    -addext "subjectAltName=IP:$ENDPOINT" \
    -keyout /etc/awg-panel/tls.key -out /etc/awg-panel/tls.crt 2>/dev/null \
  || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$ENDPOINT" \
    -keyout /etc/awg-panel/tls.key -out /etc/awg-panel/tls.crt 2>/dev/null
  chmod 600 /etc/awg-panel/tls.key
fi

if [[ -f /etc/awg-panel/panel.env ]] && systemctl is-active -q awg-panel; then
  PANEL_PORT=$(sed -n 's/^PANEL_BIND=.*://p' /etc/awg-panel/panel.env)
fi
cat > /etc/awg-panel/panel.env <<ENV
PANEL_BIND=0.0.0.0:$PANEL_PORT
PATH=$PREFIX/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
AWG_CONF_DIR=$CONF_DIR
AWG_SERVICE=awg3-quick@{iface}
ENV
install -m 0644 "$SRC_DIR/systemd/awg-panel.service" /etc/systemd/system/awg-panel.service
systemctl daemon-reload
systemctl enable awg-panel >/dev/null 2>&1
systemctl restart awg-panel

if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow "$AWG_PORT/udp"; ufw allow "$PANEL_PORT/tcp"
fi

echo
log "Готово!"
echo "  AmneziaWG:  $ENDPOINT:$AWG_PORT/udp (протокол $PROTO.x, интерфейс $IFACE, подсеть $SUBNET)"
echo "  Панель:     https://$ENDPOINT:$PANEL_PORT"
echo "  Логин:      admin"
echo "  Пароль:     $PANEL_PASS"
echo "  (сертификат самоподписанный — браузер покажет предупреждение)"
