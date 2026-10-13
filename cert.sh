#!/usr/bin/env bash
# Официальный сертификат Let's Encrypt для веб-панели вместо самоподписанного.
#
#   sudo bash cert.sh myvpn.duckdns.org [--email you@mail.ru]   получить и подключить
#   sudo bash cert.sh --self-signed                              вернуть самоподписанный
#
# Нужно: домен (A-запись) указывает на этот сервер, порт 80/tcp свободен и открыт
# снаружи — по нему Let's Encrypt проверяет, что домен ваш. Продление автоматическое
# (certbot.timer), после продления панель перезапускается сама.
set -euo pipefail

PANEL_DIR=${AWG_PANEL_DIR:-/etc/awg-panel}
LE_LIVE=${LE_LIVE:-/etc/letsencrypt/live}
HOOK=/etc/letsencrypt/renewal-hooks/deploy/awg-panel.sh

log() { echo -e "\e[1;32m==>\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"
[[ -d $PANEL_DIR ]] || die "Панель не установлена ($PANEL_DIR нет) — сначала bash install.sh"

DOMAIN=""; EMAIL=""; MODE=le
while [[ $# -gt 0 ]]; do
  case "$1" in
    --email) EMAIL="$2"; shift 2 ;;
    --self-signed) MODE=self; shift ;;
    -*) die "Неизвестный параметр: $1" ;;
    *) DOMAIN="${1,,}"; shift ;;
  esac
done

panel_port() { sed -n 's/^PANEL_BIND=.*://p' "$PANEL_DIR/panel.env" 2>/dev/null || echo 8443; }

restart_panel() {
  systemctl restart awg-panel
  sleep 2
  systemctl is-active -q awg-panel || { journalctl -u awg-panel -n 20 --no-pager; die "Панель не запустилась"; }
}

if [[ $MODE == self ]]; then
  # Вернуть самоподписанный: убрать ссылки на Let's Encrypt и сгенерировать заново.
  if [[ -L $PANEL_DIR/tls.crt || -L $PANEL_DIR/tls.key ]]; then
    rm -f "$PANEL_DIR/tls.crt" "$PANEL_DIR/tls.key"
  fi
  if [[ -f $PANEL_DIR/tls.crt.selfsigned ]]; then
    mv "$PANEL_DIR/tls.crt.selfsigned" "$PANEL_DIR/tls.crt"
    mv "$PANEL_DIR/tls.key.selfsigned" "$PANEL_DIR/tls.key"
  elif [[ ! -f $PANEL_DIR/tls.crt ]]; then
    IP=$(curl -4 -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$IP" \
      -keyout "$PANEL_DIR/tls.key" -out "$PANEL_DIR/tls.crt" 2>/dev/null
  fi
  chmod 600 "$PANEL_DIR/tls.key"
  rm -f "$HOOK"
  restart_panel
  log "Панель снова на самоподписанном сертификате: https://<IP>:$(panel_port)"
  exit 0
fi

[[ -n "$DOMAIN" ]] || die "Укажите домен: bash cert.sh myvpn.duckdns.org"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "«$DOMAIN» не похоже на доменное имя"

# 1. Домен должен указывать на этот сервер — иначе Let's Encrypt откажет.
MYIP=$(curl -4 -fsS --max-time 5 https://api.ipify.org || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
[[ -n "$DNSIP" ]] || die "Домен $DOMAIN не найден в DNS. Создайте A-запись на ${MYIP:-IP сервера} и подождите 5–15 минут"
if [[ -n "$MYIP" && "$DNSIP" != "$MYIP" ]]; then
  die "$DOMAIN указывает на $DNSIP, а у этого сервера IP $MYIP. Исправьте A-запись и подождите обновления DNS"
fi
log "DNS в порядке: $DOMAIN → $DNSIP"

# 2. Порт 80 нужен для проверки (и при каждом продлении).
if ss -Htln "sport = :80" | grep -q .; then
  WHO=$(ss -Htlnp "sport = :80" | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1)
  die "Порт 80 занят (${WHO:-неизвестный процесс}). Освободите его — он нужен Let's Encrypt для проверки домена"
fi
if command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow 80/tcp >/dev/null && log "ufw: открыт 80/tcp"
fi

# 3. certbot. needrestart не должен перезапускать Docker с контейнерами Amnezia.
if ! command -v certbot >/dev/null; then
  log "Установка certbot"
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
  apt-get update -y >/dev/null
  apt-get install -y --no-install-recommends certbot >/dev/null
fi

log "Получение сертификата для $DOMAIN"
if [[ -n "$EMAIL" ]]; then ACC=(--email "$EMAIL"); else ACC=(--register-unsafely-without-email); fi
certbot certonly --standalone --non-interactive --agree-tos "${ACC[@]}" \
  --preferred-challenges http -d "$DOMAIN" --keep-until-expiring \
  || die "Let's Encrypt не выдал сертификат. Проверьте, что порт 80/tcp открыт в файрволе хостинга"

LIVE="$LE_LIVE/$DOMAIN"
[[ -f "$LIVE/fullchain.pem" && -f "$LIVE/privkey.pem" ]] || die "Не найден сертификат в $LIVE"

# 4. Подключить к панели: tls.crt/tls.key становятся ссылками на файлы Let's Encrypt
#    (live/ всегда указывает на актуальную версию). Самоподписанный сохраняем для отката.
for f in crt key; do
  if [[ -f $PANEL_DIR/tls.$f && ! -L $PANEL_DIR/tls.$f ]]; then
    mv "$PANEL_DIR/tls.$f" "$PANEL_DIR/tls.$f.selfsigned"
  fi
done
ln -sfn "$LIVE/fullchain.pem" "$PANEL_DIR/tls.crt"
ln -sfn "$LIVE/privkey.pem" "$PANEL_DIR/tls.key"

mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<'EOF'
#!/bin/sh
# Перезапуск панели после продления сертификата Let's Encrypt.
systemctl restart awg-panel
EOF
chmod 755 "$HOOK"
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

restart_panel
PORT=$(panel_port)
if curl -fsS --max-time 10 -o /dev/null "https://$DOMAIN:$PORT/login"; then
  log "Сертификат проверен: браузеры доверяют https://$DOMAIN:$PORT"
else
  echo "  (проверка изнутри сервера не прошла — откройте адрес в браузере сами)"
fi
echo
log "Готово!"
echo "  Панель:   https://$DOMAIN:$PORT"
echo "  Продление автоматическое, срок: $(openssl x509 -enddate -noout -in "$LIVE/fullchain.pem" | cut -d= -f2)"
echo "  Одноразовые ссылки будут с доменом, если открывать панель по https://$DOMAIN:$PORT"
echo "  Вернуть самоподписанный: bash cert.sh --self-signed"
