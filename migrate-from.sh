#!/usr/bin/env bash
# Перенос клиентов с другого сервера на этот — БЕЗ удаления своих клиентов.
#
#   sudo bash migrate-from.sh 89.125.27.15          забрать копию по SSH (спросит пароль root)
#   sudo bash migrate-from.sh /root/backup.tar.gz   из уже скачанной копии
#
# Свои клиенты этого сервера не меняются и не переподключаются. Перенесённые сохраняют
# сроки, лимиты, трафик и заметки; им нужно один раз выдать новый конфиг — в панели
# они во вкладке «Выдать заново». Старый сервер не трогается: пока клиент не получил
# новый конфиг, он работает через старый.
set -euo pipefail

log() { echo -e "\e[1;32m==>\e[0m $*"; }
die() { echo -e "\e[1;31m!!\e[0m $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Запустите от имени root"
[[ $# -eq 1 ]] || die "Укажите старый сервер или файл копии: bash migrate-from.sh 89.125.27.15"
[[ -f /etc/awg-panel/state.json ]] || die "Панель на этом сервере не установлена — сначала bash install.sh"

PY=/opt/awg-panel/venv/bin/python
JOBS=/opt/awg-panel/jobs.py
set -a; . /etc/awg-panel/panel.env; set +a

SRC="$1"
if [[ -f "$SRC" ]]; then
  FILE="$SRC"
else
  FILE=/root/migrate-$(echo "$SRC" | tr -c 'A-Za-z0-9.-' _)-$(date +%Y%m%d-%H%M%S).tar.gz
  log "Делаю свежую копию на $SRC (введите пароль root от $SRC, если спросит)"
  ssh -o StrictHostKeyChecking=accept-new "root@$SRC" \
    'set -a; . /etc/awg-panel/panel.env; set +a; cat "$(/opt/awg-panel/venv/bin/python /opt/awg-panel/jobs.py backup)"' \
    > "$FILE" || { rm -f "$FILE"; die "Не удалось забрать копию с $SRC. Скачайте её в панели старого сервера
   (Настройки → Резервные копии) и запустите: bash migrate-from.sh /root/<файл>.tar.gz"; }
  chmod 600 "$FILE"
  log "Копия сохранена: $FILE"
fi

echo
"$PY" "$JOBS" import "$FILE" --dry-run
echo
read -r -p "Добавить этих клиентов к клиентам этого сервера? [y/N] " ok
[[ "$ok" =~ ^[yYдД] ]] || { echo "Отменено, ничего не изменено."; exit 0; }
"$PY" "$JOBS" import "$FILE"
echo
log "Готово. Откройте панель → вкладка «Выдать заново» и отправьте каждому одноразовую ссылку."
