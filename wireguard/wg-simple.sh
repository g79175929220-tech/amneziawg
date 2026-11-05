#!/usr/bin/env bash
# Обычный WireGuard — для официального приложения WireGuard (без обфускации AmneziaWG).
#
#   sudo bash wg-simple.sh                установить и создать первый конфиг
#   sudo bash wg-simple.sh --add ИМЯ      ещё один конфиг (отдельное устройство)
#   sudo bash wg-simple.sh --show [ИМЯ]   показать конфиг ещё раз (--qr — и QR-код для телефона)
#   sudo bash wg-simple.sh --list         конфиги и когда подключались
#   sudo bash wg-simple.sh --remove ИМЯ   удалить конфиг (устройство отключится)
#
# Опции при установке: --name ИМЯ  --port 51820  --endpoint IP|домен
#                      --dns "8.8.8.8, 8.8.4.4"  --mtu 1280
#
# Уже настроенный на сервере WireGuard (wg0 и т.п.) не трогается: скрипт заводит свой
# интерфейс, свободные порт и подсеть. Повторный запуск ничего не пересоздаёт.
set -euo pipefail

DIR=/etc/wireguard/simple            # настройки и конфиги клиентов этого скрипта
ENV=$DIR/server.env

log() { echo -e "\e[1;32m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"

ACTION=install; NAME=""; QR=0; PORT=51820; USER_PORT=0; ENDPOINT=""; DNS="8.8.8.8, 8.8.4.4"; MTU=1280
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add) ACTION=add; NAME="${2:-}"; shift; [[ -n "$NAME" ]] && shift ;;
    --show) ACTION=show; NAME="${2:-}"; shift; [[ -n "$NAME" && "$NAME" != --* ]] && shift || NAME="" ;;
    --list) ACTION=list; shift ;;
    --remove) ACTION=remove; NAME="${2:-}"; shift; [[ -n "$NAME" ]] && shift ;;
    --name) NAME="$2"; shift 2 ;;
    --qr) QR=1; shift ;;
    --port) PORT="$2"; USER_PORT=1; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --dns) DNS="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    *) die "Неизвестный параметр: $1" ;;
  esac
done

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
    echo "QR-код — в приложении WireGuard на телефоне: «+» → «Сканировать QR-код»:"
    qrencode -t ansiutf8 -m 2 < "$f"
  fi
  local host; host=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo "$ENDPOINT")
  cat <<EOF

  Скачать файл на компьютер — в PowerShell на ПК (файл появится на рабочем столе):
      scp root@$host:/root/$1.conf "\$env:USERPROFILE\Desktop\\"
  (вход по ключу — добавьте после scp: -i путь\к\ключу)
  Затем в WireGuard для Windows: «Импорт туннелей из файла» → $1.conf.
  Или «Добавить туннель» → «Добавить пустой туннель» и вставьте текст конфига выше целиком.
  Один конфиг — одно устройство: для второго — bash $0 --add ИМЯ
EOF
}

# ------------------------------------------------------------------ действия с готовой установкой
if [[ $ACTION != install ]]; then
  [[ -f $ENV ]] || die "WireGuard этим скриптом ещё не установлен — сначала: bash $0"
  . $ENV
fi

add_peer() {   # add_peer ИМЯ — ключи, адрес, конфиг клиента, пир на сервере
  local name=$1 priv pub psk ip used n
  check_name "$name"
  [[ -f $(conf_of "$name") ]] && die "Конфиг «$name» уже есть: bash $0 --show $name"
  used=$(grep -h '^AllowedIPs' "$DIR/peers/"*.peer 2>/dev/null | awk '{print $3}' | cut -d/ -f1 | cut -d. -f4 || true)
  for n in $(seq 2 254); do grep -qx "$n" <<<"$used" || { ip="$SUBNET.$n"; break; }; done
  [[ -n "${ip:-}" ]] || die "Подсеть заполнена"
  priv=$(wg genkey); pub=$(wg pubkey <<<"$priv"); psk=$(wg genpsk)
  install -d -m 700 "$DIR/clients" "$DIR/peers"
  umask 077
  cat > "$(conf_of "$name")" <<EOF
[Interface]
PrivateKey = $priv
Address = $ip/32
DNS = $DNS
MTU = $MTU

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
  { cat "$DIR/interface.conf"; cat "$DIR/peers/"*.peer 2>/dev/null || true; } > "/etc/wireguard/$IFACE.conf"
  if systemctl is-active -q "wg-quick@$IFACE"; then
    wg syncconf "$IFACE" <(wg-quick strip "$IFACE")
  else
    systemctl enable -q "wg-quick@$IFACE"
    systemctl restart "wg-quick@$IFACE" || { journalctl -u "wg-quick@$IFACE" -n 20 --no-pager; die "WireGuard не запустился"; }
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
    echo "WireGuard: интерфейс $IFACE, $ENDPOINT:$PORT/udp, подсеть $SUBNET.0/24"
    now=$(date +%s)
    for f in "$DIR/peers/"*.peer; do
      [[ -f "$f" ]] || { echo "  конфигов нет"; break; }
      n=$(basename "$f" .peer); pub=$(awk '/^PublicKey/{print $3}' "$f"); ip=$(awk '/^AllowedIPs/{print $3}' "$f")
      hs=$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk -v k="$pub" '$1==k{print $2}')
      tr=$(wg show "$IFACE" transfer 2>/dev/null | awk -v k="$pub" '$1==k{printf "↓ %.1f МБ ↑ %.1f МБ", $3/1e6, $2/1e6}')
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
  log "WireGuard уже установлен этим скриптом: $IFACE, $ENDPOINT:$PORT/udp"
  [[ -n "$NAME" && ! -f $(conf_of "$NAME") ]] && { add_peer "$NAME"; show_client "$NAME"; exit 0; }
  bash "$0" --list
  echo "  Показать конфиг: bash $0 --show ИМЯ · новый: bash $0 --add ИМЯ"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
log "Установка WireGuard"
apt-get update -y >/dev/null
apt-get install -y --no-install-recommends wireguard-tools qrencode iptables iproute2 curl python3 >/dev/null

# Что уже есть на сервере — не трогаем.
for f in /etc/wireguard/*.conf; do
  [[ -f "$f" ]] && warn "Уже есть WireGuard $(basename "$f" .conf) — его не трогаю"
done
if command -v docker >/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE 'wg|wireguard|amnezia'; then
  warn "В Docker уже работают VPN-контейнеры: $(docker ps --format '{{.Names}}' | grep -iE 'wg|wireguard|amnezia' | tr '\n' ' ')— они продолжат работать"
fi

# Ядро Ubuntu 22.04+/Debian 12 уже умеет WireGuard. Если нет (редкие VPS) — userspace.
if ! ip link add wgsimpletest type wireguard 2>/dev/null; then
  warn "В ядре нет WireGuard — ставлю userspace-версию wireguard-go"
  apt-get install -y --no-install-recommends wireguard-go >/dev/null || die "Не удалось установить wireguard-go"
else
  ip link del wgsimpletest
fi

i=0; while [[ -e /etc/wireguard/wg$i.conf ]] || ip link show "wg$i" >/dev/null 2>&1; do i=$((i+1)); done
IFACE=wg$i

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
[[ -n "$NAME" ]] || NAME="wg-${ENDPOINT##*.}"      # wg-101 — разные имена на разных серверах
check_name "$NAME"

log "Пересылка пакетов"
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-wg-simple.conf
sysctl -qw net.ipv4.ip_forward=1

SRV_PRIV=$(wg genkey); SRV_PUB=$(wg pubkey <<<"$SRV_PRIV")
install -d -m 700 /etc/wireguard "$DIR" "$DIR/clients" "$DIR/peers"
umask 077
# Правила FORWARD — в начало цепочки (-I): у Docker политика DROP. MSS — под MTU туннеля,
# иначе крупные TCP-пакеты теряются и загрузка видео «зависает».
MSS="iptables -t mangle %s FORWARD %s $IFACE -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
cat > "$DIR/interface.conf" <<EOF
# Управляется wg-simple.sh (конфиги клиентов — $DIR). Ручные правки будут перезаписаны.
[Interface]
PrivateKey = $SRV_PRIV
Address = $SUBNET.1/24
ListenPort = $PORT
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
log "Готово! WireGuard: $IFACE, $ENDPOINT:$PORT/udp — откройте этот UDP-порт в файрволе хостинга, если он есть."
show_client "$NAME"
