#!/usr/bin/env bash
# Обычный WireGuard — для официального приложения WireGuard (без обфускации AmneziaWG),
# или с --awg — AmneziaWG 3.1 (обфускация, для приложений AmneziaVPN / AmneziaWG), без панели.
#
#   sudo bash wg-simple.sh                установить и создать первый конфиг
#   sudo bash wg-simple.sh --add ИМЯ      ещё один конфиг (отдельное устройство)
#   sudo bash wg-simple.sh --show [ИМЯ]   показать конфиг ещё раз (--qr — и QR-код для телефона)
#   sudo bash wg-simple.sh --list         конфиги и когда подключались
#   sudo bash wg-simple.sh --remove ИМЯ   удалить конфиг (устройство отключится)
#
# Опции при установке: --name ИМЯ  --port 51820  --endpoint IP|домен
#                      --dns "8.8.8.8, 8.8.4.4"  --mtu 1280
#                      --awg [--proto 3|2]  — AmneziaWG (2 — для старых приложений 2.x)
# Для --awg скрипт запускается из папки репозитория (собирает AmneziaWG через install.sh).
#
# Уже настроенный на сервере WireGuard (wg0 и т.п.) не трогается: скрипт заводит свой
# интерфейс, свободные порт и подсеть. Повторный запуск ничего не пересоздаёт.
set -euo pipefail


log() { echo -e "\e[1;32m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"

ACTION=install; NAME=""; QR=0; AWG=0; PROTO=3; PORT=51820; USER_PORT=0; ENDPOINT=""; DNS="8.8.8.8, 8.8.4.4"; MTU=1280
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add) ACTION=add; NAME="${2:-}"; shift; [[ -n "$NAME" ]] && shift ;;
    --show) ACTION=show; NAME="${2:-}"; shift; [[ -n "$NAME" && "$NAME" != --* ]] && shift || NAME="" ;;
    --list) ACTION=list; shift ;;
    --remove) ACTION=remove; NAME="${2:-}"; shift; [[ -n "$NAME" ]] && shift ;;
    --name) NAME="$2"; shift 2 ;;
    --qr) QR=1; shift ;;
    --awg) AWG=1; shift ;;
    --proto) PROTO="$2"; shift 2 ;;
    --port) PORT="$2"; USER_PORT=1; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    *) die "Неизвестный параметр: $1" ;;
  esac
done

# Без --awg действия (--list и т.п.) относятся к той установке, что есть на сервере.
if [[ $AWG == 0 && ! -f /etc/wireguard/simple/server.env && -f /etc/awg3/simple/server.env ]]; then AWG=1; fi
if [[ $AWG == 1 ]]; then
  export PATH=/opt/awg3/bin:$PATH
  WG=awg; WGQ=awg-quick; CONFD=/etc/awg3; UNIT=awg3-quick@; IFPFX=awg; LABEL=AmneziaWG
  APP="AmneziaVPN или AmneziaWG (обычное приложение WireGuard такой конфиг не примет)"
else
  WG=wg; WGQ=wg-quick; CONFD=/etc/wireguard; UNIT=wg-quick@; IFPFX=wg; LABEL=WireGuard
  APP="WireGuard"
fi
DIR=$CONFD/simple                    # настройки и конфиги клиентов этого скрипта
ENV=$DIR/server.env

# Имя конфига = имя туннеля в приложении WireGuard: латиница, цифры, _ - , до 15 символов.
check_name() {
  [[ "$1" =~ ^[A-Za-z0-9_-]{1,15}$ ]] || die "Имя «$1»: латинские буквы, цифры, _ и -, не длиннее 15 символов (так требует приложение WireGuard)"
}

conf_of() { echo "$DIR/clients/$1.conf"; }

show_client() {
  local f; f=$(conf_of "$1")
  [[ -f "$f" ]] || die "Конфига «$1» нет. Список: bash $0 --list"
  cp "$f" "/root/$1.conf"; chmod 600 "/root/$1.conf"
  echo
  echo "──────────── конфиг «$1» (файл: /root/$1.conf) ────────────"
  cat "$f"
  echo "──────────────────────────────────────────────────────────────"
  if [[ $QR == 1 ]] && command -v qrencode >/dev/null; then
    echo "QR-код — в приложении $APP на телефоне: «+» → «Сканировать QR-код»:"
    qrencode -t ansiutf8 -m 2 < "$f"
  fi
  local host; host=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "$ENDPOINT")
  cat <<EOF

  Скачать файл на компьютер — в PowerShell на ПК (файл появится на рабочем столе):
      scp root@$host:/root/$1.conf "\$env:USERPROFILE\Desktop\\"
  (вход по ключу — добавьте после scp: -i путь\к\ключу)
  Затем в приложении $APP:
  импорт туннеля / конфигурации из файла → $1.conf (или вставьте текст конфига выше целиком).
  Один конфиг — одно устройство: для второго — bash $0 --add ИМЯ
EOF
}

# ------------------------------------------------------------------ действия с готовой установкой
if [[ $ACTION != install ]]; then
  [[ -f $ENV ]] || die "$LABEL этим скриптом ещё не установлен — сначала: bash $0"
  . $ENV
fi

add_peer() {   # add_peer ИМЯ — ключи, адрес, конфиг клиента, пир на сервере
  local name=$1 priv pub psk ip used n
  check_name "$name"
  [[ -f $(conf_of "$name") ]] && die "Конфиг «$name» уже есть: bash $0 --show $name"
  used=$(grep -h '^AllowedIPs' "$DIR/peers/"*.peer 2>/dev/null | awk '{print $3}' | cut -d/ -f1 | cut -d. -f4 || true)
  for n in $(seq 2 254); do grep -qx "$n" <<<"$used" || { ip="$SUBNET.$n"; break; }; done
  [[ -n "${ip:-}" ]] || die "Подсеть заполнена"
  priv=$($WG genkey); pub=$($WG pubkey <<<"$priv"); psk=$($WG genpsk)
  install -d -m 700 "$DIR/clients" "$DIR/peers"
  umask 077
  cat > "$(conf_of "$name")" <<EOF
[Interface]
PrivateKey = $priv
Address = $ip/32
DNS = $DNS
MTU = $MTU
$(cat "$DIR/obfs.client" 2>/dev/null)

[Peer]
PublicKey = $SRV_PUB
PresharedKey = $psk
Endpoint = $ENDPOINT:$PORT
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOF
  cat > "$DIR/peers/$name.peer" <<EOF

# $name
[Peer]
PublicKey = $pub
PresharedKey = $psk
AllowedIPs = $ip/32
EOF
  render
}

render() {     # собрать конфиг сервера из шаблона и пиров, применить без перезапуска
  umask 077
  { cat "$DIR/interface.conf"; cat "$DIR/peers/"*.peer 2>/dev/null || true; } > "$CONFD/$IFACE.conf"
  if systemctl is-active -q "$UNIT$IFACE"; then
    $WG syncconf "$IFACE" <($WGQ strip "$CONFD/$IFACE.conf")
  else
    systemctl enable -q "$UNIT$IFACE"
    systemctl restart "$UNIT$IFACE" || { journalctl -u "$UNIT$IFACE" -n 20 --no-pager; die "$LABEL не запустился"; }
  fi
}

case $ACTION in
  add)
    [[ -n "$NAME" ]] || die "Укажите имя: bash $0 --add ИМЯ"
    add_peer "$NAME"; log "Конфиг «$NAME» создан, подключённые устройства не затронуты"; show_client "$NAME"; exit 0 ;;
  show)
    if [[ -z "$NAME" ]]; then NAME=$(ls "$DIR/clients" 2>/dev/null | head -1 | sed 's/\.conf$//'); fi
    [[ -n "$NAME" ]] || die "Конфигов нет: bash $0 --add ИМЯ"
    show_client "$NAME"; exit 0 ;;
  list)
    echo "$LABEL: интерфейс $IFACE, $ENDPOINT:$PORT/udp, подсеть $SUBNET.0/24"
    now=$(date +%s)
    for f in "$DIR/peers/"*.peer; do
      [[ -f "$f" ]] || { echo "  конфигов нет"; break; }
      n=$(basename "$f" .peer); pub=$(awk '/^PublicKey/{print $3}' "$f"); ip=$(awk '/^AllowedIPs/{print $3}' "$f")
      hs=$($WG show "$IFACE" latest-handshakes 2>/dev/null | awk -v k="$pub" '$1==k{print $2}')
      tr=$($WG show "$IFACE" transfer 2>/dev/null | awk -v k="$pub" '$1==k{printf "↓ %.1f МБ ↑ %.1f МБ", $3/1e6, $2/1e6}')
      if [[ -z "$hs" || "$hs" == 0 ]]; then when="ещё не подключался"
      elif (( now - hs < 180 )); then when="в сети"
      else when="был $(( (now - hs) / 60 )) мин назад"; fi
      printf "  %-15s %-14s %-22s %s\n" "$n" "$ip" "$when" "${tr:-}"
    done
    exit 0 ;;
  remove)
    [[ -n "$NAME" && -f "$DIR/peers/$NAME.peer" ]] || die "Конфига «$NAME» нет. Список: bash $0 --list"
    rm -f "$DIR/peers/$NAME.peer" "$(conf_of "$NAME")" "/root/$NAME.conf"
    render; log "Конфиг «$NAME» удалён — это устройство больше не подключится"; exit 0 ;;
esac

# ------------------------------------------------------------------ установка
if [[ -f $ENV ]]; then
  . $ENV
  log "$LABEL уже установлен этим скриптом: $IFACE, $ENDPOINT:$PORT/udp"
  [[ -n "$NAME" && ! -f $(conf_of "$NAME") ]] && { add_peer "$NAME"; show_client "$NAME"; exit 0; }
  bash "$0" --list
  echo "  Показать конфиг: bash $0 --show ИМЯ · новый: bash $0 --add ИМЯ"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
log "Установка $LABEL"
apt-get update -y >/dev/null
apt-get install -y --no-install-recommends wireguard-tools qrencode iptables iproute2 curl python3 >/dev/null

# Что уже есть на сервере — не трогаем.
for f in /etc/wireguard/*.conf /etc/awg3/*.conf; do
  [[ -f "$f" ]] && warn "Уже есть туннель $(basename "$f" .conf) — его не трогаю"
done
if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE 'wg|wireguard|amnezia'; then
  warn "В Docker уже работают VPN-контейнеры: $(docker ps --format '{{.Names}}' | grep -iE 'wg|wireguard|amnezia' | tr '\n' ' ')— они продолжат работать"
fi

if [[ $AWG == 1 ]]; then
  # AmneziaWG 3.1 (userspace amneziawg-go) — тем же установщиком, что и для панели.
  if [[ ! -x /opt/awg3/bin/awg ]] || ! systemctl cat awg3-quick@.service >/dev/null 2>&1; then
    INST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/install.sh"
    [[ -f "$INST" ]] || die "Для --awg запустите скрипт из папки репозитория:
   git clone -b claude/amnesiawg-web-panel-1kuz00 https://github.com/g79175929220-tech/amneziawg.git && cd amneziawg && bash wireguard/wg-simple.sh --awg"
    log "Сборка AmneziaWG 3.1 (около 5 минут)"
    bash "$INST" --build-only
  fi
  [[ "$PROTO" == 2 || "$PROTO" == 3 ]] || die "--proto: 3 (AmneziaWG 3.x) или 2 (приложения 2.x)"
# Ядро Ubuntu 22.04+/Debian 12 уже умеет WireGuard. Если нет (редкие VPS) — userspace.
elif ! ip link add wgsimpletest type wireguard 2>/dev/null; then
  warn "В ядре нет WireGuard — ставлю userspace-версию wireguard-go"
  apt-get install -y --no-install-recommends wireguard-go >/dev/null || die "Не удалось установить wireguard-go"
else
  ip link del wgsimpletest
fi

i=0; while [[ -e $CONFD/$IFPFX$i.conf ]] || ip link show "$IFPFX$i" >/dev/null 2>&1; do i=$((i+1)); done
IFACE=$IFPFX$i

udp_busy() {
  ss -Huln "sport = :$1" | grep -q . && return 0
  iptables -t nat -S DOCKER 2>/dev/null | grep -E -- "-p udp" | grep -qE -- "--dport $1( |\$)"
}
if udp_busy "$PORT"; then
  [[ $USER_PORT == 1 ]] && die "UDP-порт $PORT уже занят — укажите другой: --port 51830"
  while udp_busy "$PORT"; do PORT=$((PORT+1)); done
  warn "UDP-порт занят, беру свободный: $PORT"
fi

SUBNET=$(python3 - <<'PY'
import ipaddress, subprocess
used = []
for cmd in (["ip", "-4", "-o", "addr"], ["ip", "-4", "route"]):
    for tok in subprocess.run(cmd, capture_output=True, text=True).stdout.split():
        try:
            n = ipaddress.ip_network(tok, strict=False)
            if n.prefixlen > 0:
                used.append(n)
        except ValueError:
            pass
for i in range(66, 250):
    net = ipaddress.ip_network(f"10.{i}.0.0/24")
    if not any(net.overlaps(u) for u in used):
        print(net); break
PY
)
[[ -n "$SUBNET" ]] || die "Нет свободной подсети"
SUBNET=${SUBNET%.0/24}                      # 10.66.0 — первые три числа подсети
WAN=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[[ -n "$WAN" ]] || die "Не найден внешний интерфейс"
[[ -n "$ENDPOINT" ]] || ENDPOINT=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
  || ip -4 addr show "$WAN" | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
[[ -n "$NAME" ]] || NAME="$IFPFX-${ENDPOINT##*.}"   # wg-101 / awg-101 — разные имена на разных серверах
check_name "$NAME"

log "Пересылка пакетов"
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-wg-simple.conf
sysctl -qw net.ipv4.ip_forward=1

SRV_PRIV=$($WG genkey); SRV_PUB=$($WG pubkey <<<"$SRV_PRIV")
install -d -m 700 "$CONFD" "$DIR" "$DIR/clients" "$DIR/peers"
umask 077
if [[ $AWG == 1 ]]; then
  # Параметры обфускации — общие для сервера и всех его конфигов (как в панели).
  python3 - "$PROTO" "$($WG genkey)" "$DIR" <<'PY'
import secrets, sys
proto, hpk, d = int(sys.argv[1]), sys.argv[2], sys.argv[3]
r = lambda lo, hi: lo + secrets.randbelow(hi - lo + 1)
jmin = r(40, 80)
p = {"Jc": r(4, 10), "Jmin": jmin, "Jmax": r(jmin + 200, 1000)}
while True:          # S1 + 56 != S2: иначе init и response одного размера
    s1, s2 = r(15, 150), r(15, 150)
    if s1 + 56 != s2:
        break
p["S1"], p["S2"] = s1, s2
p["S3"] = r(12 if proto >= 3 else 8, 64)
p["S4"] = r(12, 32) if proto >= 3 else r(1, 16)
span = (2**31 - 1000) // 4
for i in range(4):   # четыре непересекающихся диапазона заголовков
    lo = 1000 + i * span + secrets.randbelow(span // 2)
    p[f"H{i + 1}"] = f"{lo}-{lo + r(1000, span // 4)}"
if proto >= 3:
    p.update(HeaderProtectionKey=hpk, ContentPaddingAddition="0-64", RandomTrailers="on")
cli = "\n".join(f"{k} = {v}" for k, v in p.items())
open(f"{d}/obfs.client", "w").write(cli + "\n")
open(f"{d}/obfs.server", "w").write(cli + ("\nDisableCookies = off" if proto >= 3 else "") + "\n")
PY
fi
# Правила FORWARD — в начало цепочки (-I): у Docker политика DROP. MSS — под MTU туннеля,
# иначе крупные TCP-пакеты теряются и загрузка видео «зависает».
MSS="iptables -t mangle %s FORWARD %s $IFACE -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
cat > "$DIR/interface.conf" <<EOF
# Управляется wg-simple.sh ($LABEL, конфиги клиентов — $DIR). Ручные правки будут перезаписаны.
[Interface]
PrivateKey = $SRV_PRIV
Address = $SUBNET.1/24
ListenPort = $PORT
$( [[ $AWG == 1 ]] && { echo "MTU = $MTU"; cat "$DIR/obfs.server"; } )
PostUp = iptables -t nat -A POSTROUTING -s $SUBNET.0/24 -o $WAN -j MASQUERADE; iptables -I FORWARD -i $IFACE -j ACCEPT; iptables -I FORWARD -o $IFACE -j ACCEPT; $(printf "$MSS" -A -o); $(printf "$MSS" -A -i)
PostDown = iptables -t nat -D POSTROUTING -s $SUBNET.0/24 -o $WAN -j MASQUERADE; iptables -D FORWARD -i $IFACE -j ACCEPT; iptables -D FORWARD -o $IFACE -j ACCEPT; $(printf "$MSS" -D -o); $(printf "$MSS" -D -i)
EOF
cat > $ENV <<EOF
IFACE=$IFACE
PORT=$PORT
SUBNET=$SUBNET
ENDPOINT=$ENDPOINT
DNS="$DNS"
MTU=$MTU
SRV_PUB=$SRV_PUB
EOF
add_peer "$NAME"

if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow "$PORT/udp" >/dev/null && log "ufw: открыт $PORT/udp"
fi
echo
log "Готово! $LABEL: $IFACE, $ENDPOINT:$PORT/udp — откройте этот UDP-порт в файрволе хостинга, если он есть."
show_client "$NAME"
