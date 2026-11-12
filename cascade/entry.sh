#!/usr/bin/env bash
# Входной сервер каскада AmneziaWG: пересылает UDP с этого сервера на выходные.
#
#   телефон ──► этот сервер (РФ) :443  ──► выходной сервер 1 ──► интернет
#   телефон ──► этот сервер (РФ) :8443 ──► выходной сервер 2 ──► интернет
#
# Пакеты пересылаются как есть (DNAT), ничего не расшифровывается и ключи здесь
# не нужны — обфускация AmneziaWG сохраняется на всём пути. Маршрутов может быть
# несколько: каждый на своём UDP-порту этого сервера.
#
# Добавить маршрут:  sudo bash entry.sh --exit 89.125.27.15:51820 --port 443
#                    вместо IP лучше домен: --exit myvpn.duckdns.org:51820 — тогда при переезде
#                    выходного сервера достаточно перевести домен, маршрут переключится сам
# Перенаправить порт на другой выходной сервер: добавьте --replace
# TCP (например, XRay/VLESS на выходном сервере — для мобильных сетей, где режут UDP):
#                    sudo bash entry.sh --exit myvpn.duckdns.org:443 --port 8443 --tcp
# Статус:            sudo bash entry.sh --status
# Удалить маршрут:   sudo bash entry.sh --remove 443        (TCP: --remove 8443/tcp, все: --remove all)
set -euo pipefail

CONF_DIR=/etc/awg-cascade.d
LEGACY_CONF=/etc/awg-cascade.conf          # версия с одним маршрутом
RULES=/usr/local/sbin/awg-cascade-rules
UNIT=/etc/systemd/system/awg-cascade.service
RESOLVE_UNIT=/etc/systemd/system/awg-cascade-resolve

log() { echo -e "\e[1;32m==>\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"

EXIT=""; PORT=""; ACTION=install; RM_PORT=""; REPLACE=0; PROTO=udp
while [[ $# -gt 0 ]]; do
  case "$1" in
    --exit) EXIT="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --remove) ACTION=remove; RM_PORT="${2:-}"; shift; [[ -n "$RM_PORT" ]] && shift ;;
    --status) ACTION=status; shift ;;
    --replace) REPLACE=1; shift ;;
    --tcp) PROTO=tcp; shift ;;
    *) die "Неизвестный параметр: $1" ;;
  esac
done

ufw_on() { command -v ufw >/dev/null && ufw status | grep -q active; }

write_rules_script() {
  cat > $RULES <<'EOF'
#!/bin/bash
# Правила каскада для всех маршрутов из /etc/awg-cascade.d/*.conf.
# Всё в собственных цепочках AWG_CASCADE_* — не трогает Docker/ufw.
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
    # Пересборка на месте: уже установленные потоки (conntrack) продолжают работать.
    sysctl -qw net.ipv4.ip_forward=1
    chain nat PREROUTING AWG_CASCADE_PRE
    chain nat POSTROUTING AWG_CASCADE_POST
    chain filter FORWARD AWG_CASCADE_FWD
    for f in /etc/awg-cascade.d/*.conf; do
      [ -f "$f" ] || continue
      ( PROTO=udp; . "$f"        # PROTO нет в файлах прежних версий — там всегда UDP
        iptables -t nat -A AWG_CASCADE_PRE -i "$WAN" -p "$PROTO" --dport "$LISTEN_PORT" \
          -j DNAT --to-destination "$EXIT_IP:$EXIT_PORT"
        # Ответы выходного сервера должны вернуться через этот сервер.
        iptables -t nat -A AWG_CASCADE_POST -o "$WAN" -p "$PROTO" -d "$EXIT_IP" --dport "$EXIT_PORT" -j MASQUERADE
        iptables -A AWG_CASCADE_FWD -p "$PROTO" -d "$EXIT_IP" --dport "$EXIT_PORT" -j ACCEPT
        iptables -A AWG_CASCADE_FWD -p "$PROTO" -s "$EXIT_IP" --sport "$EXIT_PORT" \
          -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT )
    done
    exit 0 ;;
  refresh)
    # Выходной сервер задан доменом: если домен перевели на другой IP — перенаправить
    # маршрут и сбросить его потоки в conntrack (иначе клиенты так и шли бы на старый IP).
    changed=""
    for f in /etc/awg-cascade.d/*.conf; do
      [ -f "$f" ] || continue
      host=$(. "$f"; echo "${EXIT_HOST:-}"); old=$(. "$f"; echo "$EXIT_IP"); port=$(PROTO=udp; . "$f"; echo "$LISTEN_PORT/$PROTO")
      case "$host" in ""|*[!0-9.]*) ;; *) continue ;; esac     # IP — нечего обновлять
      [ -n "$host" ] || continue
      new=$(getent ahostsv4 "$host" | awk 'NR==1{print $1}')
      [ -n "$new" ] && [ "$new" != "$old" ] || continue
      sed -i "s/^EXIT_IP=.*/EXIT_IP=$new/" "$f"
      logger -t awg-cascade "$host: $old -> $new (порт $port)"
      changed="$changed $port"
    done
    [ -n "$changed" ] || exit 0
    "$0" up
    for port in $changed; do conntrack -D -p "${port#*/}" --orig-port-dst "${port%/*}" >/dev/null 2>&1 || true; done
    exit 0 ;;
  down)
    unchain nat PREROUTING AWG_CASCADE_PRE
    unchain nat POSTROUTING AWG_CASCADE_POST
    unchain filter FORWARD AWG_CASCADE_FWD ;;
  *) echo "usage: $0 up|down|refresh"; exit 1 ;;
esac
EOF
  chmod 755 $RULES
}

write_unit() {
  cat > $UNIT <<EOF
[Unit]
Description=Каскад AmneziaWG: пересылка UDP/TCP на выходные серверы
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$RULES up
ExecReload=$RULES up
ExecStop=$RULES down

[Install]
WantedBy=multi-user.target
EOF
  # Раз в минуту: не сменился ли IP у выходных серверов, заданных доменом.
  cat > $RESOLVE_UNIT.service <<EOF
[Unit]
Description=Каскад AmneziaWG: обновить IP выходных серверов, заданных доменом
Requisite=awg-cascade.service
After=awg-cascade.service

[Service]
Type=oneshot
ExecStart=$RULES refresh
EOF
  cat > $RESOLVE_UNIT.timer <<EOF
[Unit]
Description=Каскад AmneziaWG: проверка DNS выходных серверов раз в минуту

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
}

# Обновление с версии на один маршрут: переносим настройку, ничего не обрывая.
migrate_legacy() {
  [[ -f $LEGACY_CONF ]] || return 0
  install -d -m 755 $CONF_DIR
  local p; p=$(. $LEGACY_CONF; echo "$LISTEN_PORT")
  [[ -f $CONF_DIR/$p.conf ]] || cp $LEGACY_CONF "$CONF_DIR/$p.conf"
  rm -f $LEGACY_CONF
  log "Прежний маршрут перенесён в $CONF_DIR/$p.conf"
}

apply_rules() {
  write_rules_script
  write_unit
  systemctl enable awg-cascade >/dev/null 2>&1
  systemctl enable --now awg-cascade-resolve.timer >/dev/null 2>&1
  if systemctl is-active -q awg-cascade; then systemctl reload awg-cascade; else systemctl start awg-cascade; fi
}

routes() { ls $CONF_DIR/*.conf 2>/dev/null || true; }
# Файл маршрута: UDP — 443.conf (как в прежних версиях), TCP — 8443-tcp.conf
conf_of() { if [[ $2 == tcp ]]; then echo "$CONF_DIR/$1-tcp.conf"; else echo "$CONF_DIR/$1.conf"; fi; }

migrate_legacy

if [[ $ACTION == status ]]; then
  if [[ -z "$(routes)" ]]; then echo "Каскад не настроен"; exit 0; fi
  echo "Маршруты (порт этого сервера → выходной сервер):"
  for f in $(routes); do
    ( PROTO=udp; . "$f"
      pn=17; [[ $PROTO == tcp ]] && pn=6     # iptables -L пишет протокол числом или словом
      pk=$(iptables -t nat -L AWG_CASCADE_PRE -v -n -x 2>/dev/null | awk -v p="dpt:$LISTEN_PORT" -v a="$PROTO" -v n=$pn \
        '($4 == a || $4 == n) && ($0 ~ p" " || $0 ~ p"$") {print $1}' | head -1)
      dst="$EXIT_IP:$EXIT_PORT"
      [[ -n "${EXIT_HOST:-}" && "${EXIT_HOST:-}" != "$EXIT_IP" ]] && dst="$EXIT_HOST:$EXIT_PORT ($EXIT_IP)"
      echo "  :$LISTEN_PORT/$PROTO → $dst   (новых подключений: ${pk:-0})" )
  done
  echo -n "Служба awg-cascade: "; systemctl is-active awg-cascade 2>/dev/null || true
  exit 0
fi

if [[ $ACTION == remove ]]; then
  [[ -n "$RM_PORT" ]] || die "Укажите, какой маршрут удалить: --remove 443 (или --remove all). Список: --status"
  if [[ "$RM_PORT" == all ]]; then
    for f in $(routes); do ( PROTO=udp; . "$f"; ufw_on && ufw delete allow "$LISTEN_PORT/$PROTO" >/dev/null || true ); done
    systemctl disable --now awg-cascade-resolve.timer awg-cascade 2>/dev/null || true
    [[ -x $RULES ]] && $RULES down || true
    rm -rf $CONF_DIR; rm -f $UNIT $RESOLVE_UNIT.service $RESOLVE_UNIT.timer $RULES /etc/sysctl.d/99-awg-cascade.conf
    systemctl daemon-reload
    log "Все маршруты каскада удалены"
    exit 0
  fi
  RM_PROTO=udp; [[ $RM_PORT == */tcp || $PROTO == tcp ]] && RM_PROTO=tcp
  RM_PORT=${RM_PORT%/*}
  RM_CONF=$(conf_of "$RM_PORT" $RM_PROTO)
  [[ -f $RM_CONF ]] || die "Маршрута на порту $RM_PORT/$RM_PROTO нет. Список: bash entry.sh --status"
  rm -f "$RM_CONF"
  ufw_on && ufw delete allow "$RM_PORT/$RM_PROTO" >/dev/null || true
  FLUSH_PORT=$RM_PORT
  if [[ -z "$(routes)" ]]; then
    systemctl disable --now awg-cascade-resolve.timer awg-cascade 2>/dev/null || true
  else
    apply_rules
  fi
  # Уже установленные потоки идут по записям conntrack и без правила — сбрасываем их,
  # иначе удалённый маршрут продолжит работать для подключённых клиентов.
  command -v conntrack >/dev/null && conntrack -D -p $RM_PROTO --orig-port-dst "$FLUSH_PORT" >/dev/null 2>&1 || true
  log "Маршрут :$RM_PORT/$RM_PROTO удалён, остальные работают"
  exit 0
fi

# ------------------------------------------------------------------ добавление маршрута
[[ -n "$EXIT" ]] || die "Укажите выходной сервер: --exit IP:порт"
EXIT_HOST=${EXIT%:*}; EXIT_PORT=${EXIT##*:}
[[ "$EXIT_PORT" =~ ^[0-9]+$ && "$EXIT_HOST" != "$EXIT" ]] || die "Формат --exit: IP:порт или домен:порт (например 89.125.27.15:51820)"
EXIT_HOST=${EXIT_HOST,,}
EXIT_IP=$(getent ahostsv4 "$EXIT_HOST" | awk 'NR==1{print $1}')
[[ -n "$EXIT_IP" ]] || die "Не удалось определить IP для $EXIT_HOST"
LISTEN_PORT=${PORT:-$EXIT_PORT}
[[ "$LISTEN_PORT" =~ ^[0-9]+$ ]] || die "Неверный порт: $LISTEN_PORT"
WAN=$(ip -4 route show default | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[[ -n "$WAN" ]] || die "Не найден внешний интерфейс"

UPDATE=0
CONF=$(conf_of "$LISTEN_PORT" $PROTO)
if [[ -f $CONF ]]; then
  OLD=$(. "$CONF"; echo "${EXIT_HOST:-$EXIT_IP}:$EXIT_PORT")
  if [[ "$OLD" != "$EXIT_HOST:$EXIT_PORT" ]]; then
    [[ $REPLACE == 1 ]] || die "Порт $LISTEN_PORT уже ведёт на $OLD. Для нового выходного сервера возьмите другой
   порт (--port 8443) или перенаправьте этот: добавьте --replace — подключённые через него
   клиенты перейдут на новый сервер. Список маршрутов: bash entry.sh --status"
    log "Порт $LISTEN_PORT перенаправляется: $OLD → $EXIT_HOST:$EXIT_PORT"
  fi
  UPDATE=1                     # тот же порт — переустановка или перенаправление
fi
for f in $(routes); do
  O=$(PROTO=udp; . "$f"; echo "$PROTO:$EXIT_IP:$EXIT_PORT:$LISTEN_PORT")
  if [[ "${O%:*}" == "$PROTO:$EXIT_IP:$EXIT_PORT" && "${O##*:}" != "$LISTEN_PORT" ]]; then
    log "Замечание: на $EXIT_IP:$EXIT_PORT уже ведёт порт ${O##*:} — добавляю ещё один вход"
  fi
done

if [[ $UPDATE == 0 && $PROTO == tcp ]]; then
  if ss -Htln "sport = :$LISTEN_PORT" | grep -q .; then
    die "TCP-порт $LISTEN_PORT на этом сервере уже занят ($(ss -Htlnp "sport = :$LISTEN_PORT" | grep -o '"[^"]*"' | head -1)) — выберите другой: --port 8444"
  fi
  if iptables -t nat -S DOCKER 2>/dev/null | grep -E -- "-p tcp" | grep -qE -- "--dport $LISTEN_PORT( |\$)"; then
    die "TCP-порт $LISTEN_PORT опубликован Docker-контейнером — выберите другой: --port 8444"
  fi
elif [[ $UPDATE == 0 ]]; then
  if ss -Huln "sport = :$LISTEN_PORT" | grep -q .; then
    die "UDP-порт $LISTEN_PORT на этом сервере уже занят — выберите другой: --port 8443"
  fi
  # Порты уже работающих WireGuard/AmneziaWG на этом сервере — их не перехватываем.
  for tool in wg awg; do
    command -v $tool >/dev/null || continue
    if $tool show all listen-port 2>/dev/null | awk '{print $2}' | grep -qx "$LISTEN_PORT"; then
      die "UDP-порт $LISTEN_PORT занят туннелем $tool ($($tool show all listen-port | awk -v p=$LISTEN_PORT '$2==p{print $1}')) — выберите другой, например --port 8443"
    fi
  done
  # Порты, опубликованные Docker (Amnezia ставит WireGuard/AWG в контейнеры). Проверяем
  # правила DNAT Docker — они есть, даже если docker-proxy отключён и ss порт не видит.
  if iptables -t nat -S DOCKER 2>/dev/null | grep -E -- "-p udp" | grep -qE -- "--dport $LISTEN_PORT( |\$)"; then
    die "UDP-порт $LISTEN_PORT опубликован Docker-контейнером ($(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E ":$LISTEN_PORT->" | awk '{print $1}' | tr '\n' ' ')) — выберите другой, например --port 8443"
  fi
fi
if ip -4 -o addr | awk '{sub(/\/.*/,"",$4); print $4}' | grep -qx "$EXIT_IP"; then
  die "$EXIT_IP — это адрес самого этого сервера. В --exit укажите выходной сервер с AmneziaWG"
fi

log "Пересылка ${PROTO^^} :$LISTEN_PORT ($WAN) → $EXIT_IP:$EXIT_PORT"
install -d -m 755 $CONF_DIR
cat > "$CONF" <<EOF
EXIT_HOST=$EXIT_HOST
EXIT_IP=$EXIT_IP
EXIT_PORT=$EXIT_PORT
LISTEN_PORT=$LISTEN_PORT
WAN=$WAN
PROTO=$PROTO
EOF
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-awg-cascade.conf
# conntrack — чтобы при смене выходного сервера сбросить уже установленные потоки.
if ! command -v conntrack >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get install -y -qq conntrack >/dev/null 2>&1 \
    || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq conntrack >/dev/null 2>&1; } || true
fi
apply_rules
# Только для нового порта: сбросить старые записи conntrack, чтобы правило сразу сработало.
# Записи других маршрутов не трогаем — подключённые клиенты не обрываются.
command -v conntrack >/dev/null && conntrack -D -p $PROTO --orig-port-dst "$LISTEN_PORT" >/dev/null 2>&1 || true

if ufw_on; then
  ufw allow "$LISTEN_PORT/$PROTO" >/dev/null && log "ufw: открыт $LISTEN_PORT/$PROTO"
fi

PUB=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || ip -4 addr show "$WAN" | awk '/inet /{sub(/\/.*/,"",$2); print $2; exit}')
echo
log "Готово! Маршрут :$LISTEN_PORT → $EXIT_HOST:$EXIT_PORT работает и переживёт перезагрузку."
if [[ "$EXIT_HOST" != "$EXIT_IP" ]]; then
  echo "  Выходной сервер задан доменом: переведёте $EXIT_HOST на другой IP — маршрут переключится"
  echo "  сам в течение минуты, клиентам каскада ничего менять не нужно."
fi
if [[ $PROTO == tcp ]]; then
  echo "  В ссылках клиентов (vless://…) укажите адрес $PUB:$LISTEN_PORT (или домен этого сервера)."
else
  echo "  В панели выходного сервера ($EXIT_IP): Настройки → «Адрес каскада» = $PUB:$LISTEN_PORT"
  echo "  Затем у нужных клиентов выберите маршрут «Через каскад» и обновите у них конфиг."
fi
echo
bash "$(realpath "$0")" --status
