#!/usr/bin/env bash
# Входной сервер каскада AmneziaWG: пересылает UDP с этого сервера на выходной.
#
#   телефон ──► этот сервер (РФ) ──► выходной сервер с AmneziaWG ──► интернет
#
# Пакеты пересылаются как есть (DNAT), ничего не расшифровывается и ключи здесь
# не нужны — обфускация AmneziaWG сохраняется на всём пути.
#
# Установка:  sudo bash entry.sh --exit 89.125.27.15:51820 [--port 443]
# Статус:     sudo bash entry.sh --status
# Удаление:   sudo bash entry.sh --remove
set -euo pipefail

CONF=/etc/awg-cascade.conf
RULES=/usr/local/sbin/awg-cascade-rules
UNIT=/etc/systemd/system/awg-cascade.service

log() { echo -e "\e[1;32m==>\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"

EXIT=""; PORT=""; ACTION=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exit) EXIT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --remove) ACTION=remove; shift ;;
    --status) ACTION=status; shift ;;
    *) die "Неизвестный параметр: $1" ;;
  esac
done

if [[ $ACTION == status ]]; then
  [[ -f $CONF ]] && cat $CONF || echo "Каскад не настроен"
  systemctl is-active awg-cascade 2>/dev/null || true
  iptables -t nat -S AWG_CASCADE_PRE 2>/dev/null || true
  iptables -t nat -L AWG_CASCADE_PRE -v -n 2>/dev/null | tail -n +3 || true
  exit 0
fi

if [[ $ACTION == remove ]]; then
  systemctl disable --now awg-cascade 2>/dev/null || true
  [[ -x $RULES ]] && $RULES down || true
  if [[ -f $CONF ]] && command -v ufw >/dev/null && ufw status | grep -q active; then
    . $CONF; ufw delete allow "$LISTEN_PORT/udp" >/dev/null || true
  fi
  rm -f $UNIT $RULES $CONF /etc/sysctl.d/99-awg-cascade.conf
  systemctl daemon-reload
  log "Каскад удалён"
  exit 0
fi

[[ -n "$EXIT" ]] || die "Укажите выходной сервер: --exit IP:порт"
EXIT_HOST=${EXIT%:*}; EXIT_PORT=${EXIT##*:}
[[ "$EXIT_PORT" =~ ^[0-9]+$ && "$EXIT_HOST" != "$EXIT" ]] || die "Формат --exit: IP:порт (например 89.125.27.15:51820)"
EXIT_IP=$(getent ahostsv4 "$EXIT_HOST" | awk 'NR==1{print $1}')
[[ -n "$EXIT_IP" ]] || die "Не удалось определить IP для $EXIT_HOST"
LISTEN_PORT=${PORT:-$EXIT_PORT}
WAN=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[[ -n "$WAN" ]] || die "Не найден внешний интерфейс"

if ss -Huln "sport = :$LISTEN_PORT" | grep -q .; then
  die "UDP-порт $LISTEN_PORT на этом сервере уже занят — выберите другой: --port 51821"
fi
# Порты уже работающих WireGuard/AmneziaWG на этом сервере — их не перехватываем.
for tool in wg awg; do
  command -v $tool >/dev/null || continue
  if $tool show all listen-port 2>/dev/null | awk '{print $2}' | grep -qx "$LISTEN_PORT"; then
    die "UDP-порт $LISTEN_PORT занят туннелем $tool ($($tool show all listen-port | awk -v p=$LISTEN_PORT '$2==p{print $1}')) — выберите другой, например --port 8443"
  fi
done
if ip -4 -o addr | awk '{sub(/\/.*/,"",$4); print $4}' | grep -qx "$EXIT_IP"; then
  die "$EXIT_IP — это адрес самого этого сервера. В --exit укажите выходной сервер с AmneziaWG"
fi

log "Пересылка UDP :$LISTEN_PORT ($WAN) → $EXIT_IP:$EXIT_PORT"
cat > $CONF <<EOF
EXIT_IP=$EXIT_IP
EXIT_PORT=$EXIT_PORT
LISTEN_PORT=$LISTEN_PORT
WAN=$WAN
EOF

cat > $RULES <<'EOF'
#!/bin/bash
# Правила каскада. Всё в собственных цепочках AWG_CASCADE_* — не трогает Docker/ufw.
. /etc/awg-cascade.conf
chain() {  # chain <таблица> <встроенная цепочка> <своя цепочка>
  iptables -t "$1" -N "$3" 2>/dev/null || iptables -t "$1" -F "$3"
  iptables -t "$1" -C "$2" -j "$3" 2>/dev/null || iptables -t "$1" -I "$2" 1 -j "$3"
}
unchain() {
  while iptables -t "$1" -D "$2" -j "$3" 2>/dev/null; do :; done
  iptables -t "$1" -F "$3" 2>/dev/null; iptables -t "$1" -X "$3" 2>/dev/null
  return 0
}
case "$1" in
  up)
    sysctl -qw net.ipv4.ip_forward=1
    chain nat PREROUTING AWG_CASCADE_PRE
    iptables -t nat -A AWG_CASCADE_PRE -i "$WAN" -p udp --dport "$LISTEN_PORT" \
      -j DNAT --to-destination "$EXIT_IP:$EXIT_PORT"
    chain nat POSTROUTING AWG_CASCADE_POST
    # Ответы выходного сервера должны вернуться через этот сервер.
    iptables -t nat -A AWG_CASCADE_POST -o "$WAN" -p udp -d "$EXIT_IP" --dport "$EXIT_PORT" -j MASQUERADE
    chain filter FORWARD AWG_CASCADE_FWD
    iptables -A AWG_CASCADE_FWD -p udp -d "$EXIT_IP" --dport "$EXIT_PORT" -j ACCEPT
    iptables -A AWG_CASCADE_FWD -p udp -s "$EXIT_IP" --sport "$EXIT_PORT" \
      -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    # Сбросить старые записи conntrack, чтобы новые правила сразу применились.
    command -v conntrack >/dev/null && conntrack -D -p udp --dport "$LISTEN_PORT" >/dev/null 2>&1
    exit 0 ;;
  down)
    unchain nat PREROUTING AWG_CASCADE_PRE
    unchain nat POSTROUTING AWG_CASCADE_POST
    unchain filter FORWARD AWG_CASCADE_FWD ;;
  *) echo "usage: $0 up|down"; exit 1 ;;
esac
EOF
chmod 755 $RULES

echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-awg-cascade.conf
cat > $UNIT <<EOF
[Unit]
Description=Каскад AmneziaWG: пересылка UDP на выходной сервер
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$RULES up
ExecStop=$RULES down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable awg-cascade >/dev/null 2>&1
systemctl restart awg-cascade

if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow "$LISTEN_PORT/udp" >/dev/null && log "ufw: открыт $LISTEN_PORT/udp"
fi

PUB=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || ip -4 addr show "$WAN" | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
echo
log "Готово! Каскад работает и переживёт перезагрузку."
echo "  В панели: Настройки → «Адрес каскада» = $PUB:$LISTEN_PORT"
echo "  Затем у нужных клиентов выберите маршрут «Через каскад» и обновите у них конфиг."
echo "  Проверка счётчиков пакетов: sudo bash entry.sh --status"
