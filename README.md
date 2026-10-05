# AmneziaWG + веб-панель

Установщик сервера AmneziaWG и лёгкая веб-панель для управления клиентами.

## Установка на сервер (Ubuntu 20.04+ / Debian 11+)

```bash
ssh root@<IP_СЕРВЕРА>
git clone -b claude/amnesiawg-web-panel-1kuz00 https://github.com/g79175929220-tech/amneziawg.git
cd amneziawg
sudo bash install.sh            # опции: --port 51820 --panel-port 8443 --endpoint <IP> --proto 2|1
```

В конце скрипт выведет адрес панели (`https://<IP>:8443`), логин `admin` и сгенерированный пароль.

Что делает `install.sh`:
1. Ставит `amneziawg` (модуль ядра, DKMS) и `amneziawg-tools` из PPA Amnezia.
   Если модуль собрать не удалось — собирает userspace-реализацию `amneziawg-go`.
2. Включает IP forwarding, NAT через iptables.
3. Генерирует ключи сервера и параметры обфускации, поднимает `awg-quick@awg0`.
   По умолчанию используются параметры **AmneziaWG 2.x** (S3/S4, диапазоны H1–H4, I1–I5);
   если установленная версия их не принимает — автоматический откат на 1.x.
4. Ставит панель в `/opt/awg-panel` (Flask + gunicorn, HTTPS с самоподписанным сертификатом),
   сервис `awg-panel`.

## Панель

- список клиентов: онлайн-статус, последний handshake, трафик Rx/Tx;
- добавление / отключение / удаление / переименование клиента;
- QR-код и `.conf` для приложений AmneziaVPN / AmneziaWG;
- настройки endpoint, порта, DNS, MTU, keepalive;
- редактирование и перегенерация параметров обфускации (Jc, Jmin, Jmax, S1–S4, H1–H4, I1–I5);
- смена пароля, защита от перебора и CSRF.

## Файлы на сервере

| Путь | Назначение |
|---|---|
| `/etc/awg-panel/state.json` | ключи, клиенты, параметры (источник истины) |
| `/etc/awg-panel/panel.json` | логин/хеш пароля панели |
| `/etc/amnezia/amneziawg/awg0.conf` | генерируется панелью, вручную не править |

## Полезные команды

```bash
awg show                                    # состояние туннеля
systemctl status awg-quick@awg0 awg-panel
/opt/awg-panel/venv/bin/python /opt/awg-panel/app.py set-password 'НовыйПароль'
/opt/awg-panel/venv/bin/python /opt/awg-panel/app.py set-proto 1   # принудительно AWG 1.x
```

Рекомендуется ограничить доступ к порту панели (например, `ufw allow from <ваш IP> to any port 8443`).
