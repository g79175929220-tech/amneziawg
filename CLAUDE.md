# AmneziaWG 3.1 + веб-панель — заметки для Claude

Общение с пользователем и всё, что он видит (сообщения, тексты в интерфейсе, комментарии,
сообщения коммитов), — **на русском**. Пользователь не программист: давайте готовые команды
для PowerShell (Windows) или для сервера и объясняйте, что увидеть в выводе.

## Что в репозитории

| Путь | Что это |
|---|---|
| `install.sh` | AmneziaWG 3.1 (сборка из исходников в `/opt/awg3`, юнит `awg3-quick@`) + панель в `/opt/awg-panel`. Режимы: `--restore копия.tar.gz`, `--node "ssh-ed25519 …"` (доп. сервер без панели), `--build-only` |
| `panel/` | Flask + gunicorn (1 воркер, `gunicorn_conf.py`), состояние — `/etc/awg-panel/state.json` под `awg._lock` (RLock + flock) |
| `panel/awg.py` | клиенты, конфиги, учёт трафика (накопительный, по месяцам), `Live`/`status()` по всем серверам |
| `panel/nodes.py` | доп. серверы по SSH (ключ `/etc/awg-panel/node_ed25519`, ControlMaster), агент `node/awg-node` (info/dump/apply) |
| `panel/jobs.py` | только stdlib: резервные копии, `import` клиентов с другого сервера, Telegram, ночные задачи |
| `cascade/entry.sh` | входной сервер каскада (DNAT, несколько маршрутов, выход доменом с авто-обновлением IP) |
| `wireguard/wg-simple.sh` | обычный WireGuard без панели; `--awg` — AmneziaWG без панели |
| `cert.sh`, `secure-ssh.sh`, `migrate-from.sh` | Let's Encrypt, защита SSH, перенос клиентов |

Ветка разработки: `claude/amnesiawg-web-panel-1kuz00` (на неё ссылаются все команды установки).
Обновление панели на сервере: `cd ~/amneziawg && git pull && bash install.sh` (туннель не перезапускается).

## Правила

- **Не ломать соседей.** На серверах бывают AmneziaWG 2.0 в Docker (`amnezia-*`), WireGuard,
  сайты. Свои интерфейс/порт/подсеть подбираются автоматически, порты Docker учитываются,
  правила iptables — только для своего интерфейса, `sysctl` — только `ip_forward` (не `--system`),
  `NEEDRESTART_MODE=l` при apt (иначе перезапустится Docker).
- **Секреты.** Пароли и приватные ключи не печатать, не коммитить, не просить присылать в чат.
- **Проверять перед пушем.** Синтаксис (`bash -n`, `python -m pyflakes`), логику — тестами в
  network namespaces (`ip netns`, userspace amneziawg-go/wireguard-go), интерфейс — Playwright.
  В контейнере без systemd — подменный `systemctl` в PATH.
- `pgrep -f`/`pkill -f` с шаблоном из своей же команды убивают собственную оболочку — искать по
  `/proc` или `pgrep -x`.
- Из российских сетей часть сайтов недоступна (Google: go.dev; api.telegram.org с FirstVDS) —
  загрузки делать с таймаутами и зеркалами.
