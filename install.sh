#!/usr/bin/env bash
# AmneziaWG server + web panel installer (Ubuntu 20.04+/Debian 11+).
# Usage: sudo bash install.sh [--port 51820] [--panel-port 8443] [--endpoint IP] [--proto 2|1]
set -euo pipefail

AWG_PORT=51820
PANEL_PORT=8443
ENDPOINT=""
PROTO=2
IFACE=awg0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) AWG_PORT="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --proto) PROTO="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

log() { echo -e "\e[1;32m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }

. /etc/os-release
export DEBIAN_FRONTEND=noninteractive

log "Installing base packages"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl gnupg iptables openssl \
  python3 python3-venv python3-pip git make gcc build-essential dkms qrencode \
  "linux-headers-$(uname -r)" || apt-get install -y --no-install-recommends ca-certificates \
  curl gnupg iptables openssl python3 python3-venv python3-pip git make gcc build-essential qrencode

install_ppa() {
  log "Adding Amnezia PPA"
  local codename="$VERSION_CODENAME"
  if [[ "$ID" != "ubuntu" ]]; then codename=focal; fi   # Debian: use focal build of the PPA
  install -d /etc/apt/keyrings
  # Signing key straight from Launchpad (no hardcoded key ID).
  curl -fsSL "https://api.launchpad.net/1.0/~amnezia/+archive/ubuntu/ppa?ws.op=getSigningKeyData" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin))' \
    | gpg --dearmor --yes -o /etc/apt/keyrings/amnezia.gpg
  echo "deb [signed-by=/etc/apt/keyrings/amnezia.gpg] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu $codename main" \
    > /etc/apt/sources.list.d/amnezia.list
  echo "deb-src [signed-by=/etc/apt/keyrings/amnezia.gpg] https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu $codename main" \
    >> /etc/apt/sources.list.d/amnezia.list
  apt-get update -y
}

install_go_userspace() {
  warn "Kernel module unavailable, building userspace amneziawg-go"
  if ! command -v go >/dev/null || ! go version | grep -qE 'go1\.(2[2-9]|[3-9][0-9])'; then
    local arch; arch=$(dpkg --print-architecture)
    local gover; gover=$(curl -fsSL "https://go.dev/VERSION?m=text" | head -1)
    curl -fsSL "https://go.dev/dl/${gover}.linux-${arch}.tar.gz" | tar -C /usr/local -xz
    export PATH=/usr/local/go/bin:$PATH
  fi
  rm -rf /tmp/amneziawg-go
  git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go /tmp/amneziawg-go
  make -C /tmp/amneziawg-go
  install -m 0755 /tmp/amneziawg-go/amneziawg-go /usr/bin/amneziawg-go
}

install_tools_from_source() {
  warn "Building amneziawg-tools from source"
  rm -rf /tmp/amneziawg-tools
  git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-tools /tmp/amneziawg-tools
  make -C /tmp/amneziawg-tools/src
  make -C /tmp/amneziawg-tools/src install WITH_WGQUICK=yes WITH_SYSTEMDUNITS=yes
}

log "Installing AmneziaWG"
install_ppa || warn "PPA setup failed"
apt-get install -y amneziawg amneziawg-tools || apt-get install -y amneziawg-tools || true
command -v awg >/dev/null || install_tools_from_source
if ! modprobe amneziawg 2>/dev/null; then
  install_go_userspace
fi

log "Enabling IP forwarding"
cat > /etc/sysctl.d/99-amneziawg.conf <<SYSCTL
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl --system >/dev/null

WAN=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[[ -n "$ENDPOINT" ]] || ENDPOINT=$(curl -4 -fsS --max-time 5 https://api.ipify.org || ip -4 addr show "$WAN" | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
log "WAN interface: $WAN, endpoint: $ENDPOINT"

log "Installing panel to /opt/awg-panel"
install -d /opt/awg-panel /etc/awg-panel
chmod 700 /etc/awg-panel
cp -r "$SRC_DIR/panel/." /opt/awg-panel/
python3 -m venv /opt/awg-panel/venv
/opt/awg-panel/venv/bin/pip install -q --upgrade pip
/opt/awg-panel/venv/bin/pip install -q -r /opt/awg-panel/requirements.txt
PANEL_PY="/opt/awg-panel/venv/bin/python /opt/awg-panel/app.py"

$PANEL_PY init --endpoint "$ENDPOINT" --port "$AWG_PORT" --wan "$WAN" --proto "$PROTO"

log "Starting awg-quick@$IFACE"
systemctl enable "awg-quick@$IFACE" >/dev/null 2>&1 || true
if ! systemctl restart "awg-quick@$IFACE"; then
  if [[ "$PROTO" == "2" ]]; then
    warn "AmneziaWG 2.x parameters rejected by installed version, falling back to 1.x"
    $PANEL_PY set-proto 1
    systemctl restart "awg-quick@$IFACE"
  else
    journalctl -u "awg-quick@$IFACE" --no-pager -n 30; exit 1
  fi
fi

if [[ ! -f /etc/awg-panel/panel.json ]]; then
  PANEL_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
  $PANEL_PY set-password --username admin "$PANEL_PASS"
else
  PANEL_PASS="(не изменён — используйте прежний)"
fi

if [[ ! -f /etc/awg-panel/tls.crt ]]; then
  log "Generating self-signed TLS certificate"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$ENDPOINT" \
    -addext "subjectAltName=IP:$ENDPOINT" \
    -keyout /etc/awg-panel/tls.key -out /etc/awg-panel/tls.crt 2>/dev/null \
  || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$ENDPOINT" \
    -keyout /etc/awg-panel/tls.key -out /etc/awg-panel/tls.crt 2>/dev/null
  chmod 600 /etc/awg-panel/tls.key
fi

echo "PANEL_BIND=0.0.0.0:$PANEL_PORT" > /etc/awg-panel/panel.env
install -m 0644 "$SRC_DIR/systemd/awg-panel.service" /etc/systemd/system/awg-panel.service
systemctl daemon-reload
systemctl enable --now awg-panel
systemctl restart awg-panel

if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow "$AWG_PORT/udp"; ufw allow "$PANEL_PORT/tcp"
fi

PROTO_NOW=$(python3 -c 'import json;print(json.load(open("/etc/awg-panel/state.json"))["server"]["proto"])')
echo
log "Готово!"
echo "  AmneziaWG:  $ENDPOINT:$AWG_PORT/udp (протокол $PROTO_NOW.x)"
echo "  Панель:     https://$ENDPOINT:$PANEL_PORT"
echo "  Логин:      admin"
echo "  Пароль:     $PANEL_PASS"
echo "  (сертификат самоподписанный — браузер покажет предупреждение)"
