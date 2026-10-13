#!/usr/bin/env bash
# Защита SSH: fail2ban (бан IP за подбор пароля) + вход только по ключу.
#
#   sudo bash secure-ssh.sh --add-key "ssh-ed25519 AAAA... имя"   добавить ключ и включить защиту
#   sudo bash secure-ssh.sh                                        включить (ключ уже добавлен)
#   sudo bash secure-ssh.sh --confirm      вход по ключу проверен — отменить автооткат
#   sudo bash secure-ssh.sh --rollback     вернуть вход по паролю (fail2ban остаётся)
#   sudo bash secure-ssh.sh --keep-passwords   только fail2ban, пароли не трогать
#   sudo bash secure-ssh.sh --status
#
# Страховки от потери доступа:
#   * пароли выключаются, только если у root уже есть рабочий SSH-ключ;
#   * текущий IP администратора fail2ban не банит;
#   * через 10 минут вход по паролю возвращается сам, если не выполнить --confirm
#     (то есть если по ключу войти не получилось — достаточно подождать).
set -euo pipefail

DROPIN=/etc/ssh/sshd_config.d/00-awg-hardening.conf
JAIL=/etc/fail2ban/jail.d/awg-sshd.local
AUTH_KEYS=/root/.ssh/authorized_keys
ROLLBACK_UNIT=awg-ssh-rollback
ROLLBACK_MIN=10

log() { echo -e "\e[1;32m==>\e[0m $*"; }
warn() { echo -e "\e[1;33m!!\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"

ACTION=apply; NEWKEY=""; KEEP_PW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add-key) NEWKEY="$2"; shift 2 ;;
    --keep-passwords) KEEP_PW=1; shift ;;
    --confirm) ACTION=confirm; shift ;;
    --rollback) ACTION=rollback; shift ;;
    --status) ACTION=status; shift ;;
    *) die "Неизвестный параметр: $1" ;;
  esac
done

sshd_bin() { command -v sshd || echo /usr/sbin/sshd; }
reload_ssh() {
  # Ubuntu 22.10+: ssh может работать через socket-активацию (ssh.service запускается по
  # подключению и при каждом запуске читает конфиг заново) — тогда перезагружать нечего.
  for u in ssh sshd; do
    if systemctl is-active -q "$u" 2>/dev/null; then systemctl reload "$u"; return; fi
  done
}
valid_keys() {   # число рабочих ключей в authorized_keys
  [[ -s $AUTH_KEYS ]] || { echo 0; return; }
  local n=0 line
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    # Ключи-заглушки хостинга (command="...;exit 142" — «войдите как ubuntu») не считаем.
    [[ "$line" == *command=* ]] && continue
    if ssh-keygen -lf <(echo "$line") >/dev/null 2>&1; then n=$((n+1)); fi
  done < "$AUTH_KEYS"
  echo $n
}

if [[ $ACTION == confirm ]]; then
  systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null || true
  systemctl reset-failed "$ROLLBACK_UNIT.service" 2>/dev/null || true
  log "Автооткат отменён: вход только по ключу остаётся включённым"
  exit 0
fi

if [[ $ACTION == rollback ]]; then
  rm -f "$DROPIN"
  systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null || true
  "$(sshd_bin)" -t && reload_ssh
  log "Вход по паролю снова разрешён (fail2ban продолжает работать)"
  exit 0
fi

if [[ $ACTION == status ]]; then
  echo "Ключей у root: $(valid_keys)"
  "$(sshd_bin)" -T 2>/dev/null | grep -Ei '^(port|passwordauthentication|kbdinteractiveauthentication|permitrootlogin|maxauthtries) ' || true
  if systemctl is-active -q "$ROLLBACK_UNIT.timer" 2>/dev/null; then
    warn "Ждёт подтверждения: без --confirm вход по паролю вернётся сам"
    systemctl list-timers "$ROLLBACK_UNIT.timer" --no-pager | sed -n 2p
  fi
  fail2ban-client status sshd 2>/dev/null || echo "fail2ban: не установлен или не запущен"
  exit 0
fi

# ------------------------------------------------------------------ 1. ключ
if [[ -n "$NEWKEY" ]]; then
  if [[ "$NEWKEY" == *"BEGIN SSH2 PUBLIC KEY"* ]]; then
    die "Это ключ в формате PuTTY. В PuTTYgen скопируйте строку из поля
   «Public key for pasting into OpenSSH authorized_keys file» — она начинается с ssh-ed25519 или ssh-rsa"
  fi
  NEWKEY=$(echo "$NEWKEY" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  ssh-keygen -lf <(echo "$NEWKEY") >/dev/null 2>&1 \
    || die "Это не похоже на публичный SSH-ключ. Нужна одна строка вида: ssh-ed25519 AAAAC3... имя"
  [[ "$NEWKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-) ]] || die "Неизвестный тип ключа"
  install -d -m 700 /root/.ssh
  touch "$AUTH_KEYS"; chmod 600 "$AUTH_KEYS"
  KEYBODY=$(echo "$NEWKEY" | awk '{print $2}')
  if grep -qF "$KEYBODY" "$AUTH_KEYS"; then
    log "Этот ключ уже добавлен"
  else
    echo "$NEWKEY" >> "$AUTH_KEYS"
    log "Ключ добавлен: $(ssh-keygen -lf <(echo "$NEWKEY") | awk '{print $1, $2, $NF}')"
  fi
fi

# ------------------------------------------------------------------ 2. fail2ban
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1
if ! command -v fail2ban-client >/dev/null; then
  log "Установка fail2ban"
  apt-get update -y >/dev/null
  # python3-systemd — чтобы читать журнал sshd из journald (в новых Ubuntu нет auth.log)
  apt-get install -y --no-install-recommends fail2ban python3-systemd >/dev/null
fi
PORTS=$("$(sshd_bin)" -T 2>/dev/null | awk '$1=="port"{print $2}' | sort -u | paste -sd, -)
ME=${SSH_CLIENT%% *}
cat > "$JAIL" <<EOF
# awg-panel: защита SSH от подбора паролей
[sshd]
enabled = true
backend = systemd
port = ${PORTS:-ssh}
maxretry = 5
findtime = 10m
bantime = 1h
# повторные нарушители — дольше, до недели
bantime.increment = true
bantime.maxtime = 1w
# свой IP не баним, чтобы не запереть себя опечатками
ignoreip = 127.0.0.1/8 ::1${ME:+ $ME}
EOF
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban
sleep 2
if fail2ban-client status sshd >/dev/null 2>&1; then
  log "fail2ban работает: 5 неудачных попыток за 10 минут → бан IP на час (повторно — дольше)"
  [[ -n "$ME" ]] && log "Ваш IP $ME в белом списке fail2ban"
else
  warn "fail2ban не запустился: journalctl -u fail2ban -n 30"
fi

# ------------------------------------------------------------------ 3. вход только по ключу
if [[ $KEEP_PW == 1 ]]; then
  log "Пароли не трогаю (--keep-passwords). Готово."
  exit 0
fi
KEYS=$(valid_keys)
if [[ $KEYS -eq 0 ]]; then
  warn "У root нет SSH-ключа — вход по паролю НЕ отключаю, иначе вы потеряете доступ."
  cat <<'EOF'
   Как сделать ключ в Windows (PuTTY):
   1. Запустите PuTTYgen → тип EdDSA (Ed25519) → Generate → двигайте мышью.
   2. «Save private key» — сохраните .ppk у себя (можно с паролем).
   3. Скопируйте строку из поля «Public key for pasting into OpenSSH authorized_keys file».
   4. На сервере: bash secure-ssh.sh --add-key "ssh-ed25519 AAAA... (вставленная строка)"
   5. В PuTTY: Connection → SSH → Auth → Credentials → Private key file → ваш .ppk.
      Session → Save, чтобы не выбирать каждый раз.
EOF
  exit 0
fi

grep -qiE '^\s*Include\s+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config \
  || sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
install -d /etc/ssh/sshd_config.d
# 00- — раньше 50-cloud-init.conf: в sshd побеждает первое найденное значение.
cat > "$DROPIN" <<'EOF'
# awg-panel: вход только по ключу. Откат: bash secure-ssh.sh --rollback
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
MaxAuthTries 4
LoginGraceTime 30
EOF
if ! "$(sshd_bin)" -t; then
  rm -f "$DROPIN"
  die "Конфигурация sshd не прошла проверку — ничего не изменено"
fi
EFF=$("$(sshd_bin)" -T 2>/dev/null | awk '$1=="passwordauthentication"{print $2}')
[[ "$EFF" == "no" ]] || { rm -f "$DROPIN"; die "Вход по паролю не отключился (в конфиге есть переопределение) — ничего не изменено"; }

# Страховка: если не подтвердить, через N минут пароли вернутся сами.
systemctl stop "$ROLLBACK_UNIT.timer" 2>/dev/null || true
systemctl reset-failed "$ROLLBACK_UNIT.service" 2>/dev/null || true
systemd-run --quiet --unit "$ROLLBACK_UNIT" --on-active="${ROLLBACK_MIN}m" \
  /bin/sh -c "rm -f $DROPIN; $(sshd_bin) -t && (systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true)"
reload_ssh

echo
log "Вход только по ключу включён (ключей у root: $KEYS)."
cat <<EOF

  НЕ ЗАКРЫВАЙТЕ это окно. Сейчас:
  1. Откройте НОВОЕ окно PuTTY и войдите по ключу.
  2. Получилось — в новом окне выполните:  bash $(realpath "$0") --confirm
  3. Не получилось — в этом окне:           bash $(realpath "$0") --rollback
     (или просто подождите ${ROLLBACK_MIN} минут — вход по паролю вернётся сам)
EOF
