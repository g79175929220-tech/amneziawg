# Настройки gunicorn для панели (подключается через -c в awg-panel.service).
#
# Защита от «зависших» соединений. У gunicorn (воркер gthread) сокеты блокирующие и без
# таймаута: сканер портов, который начал TLS-рукопожатие или запрос и замолчал, навсегда
# занимает поток. Несколько таких соединений — и панель перестаёт отвечать, хотя сервис
# «active (running)». Поэтому любое чтение/запись по TLS-сокету ограничено IO_TIMEOUT
# секундами, а по таймауту соединение просто закрывается.
import errno
import ssl

IO_TIMEOUT = 10


class _TimeoutSSLSocket(ssl.SSLSocket):
    def settimeout(self, value):
        super().settimeout(IO_TIMEOUT if value is None else value)

    def setblocking(self, flag):
        # gunicorn переводит сокет в блокирующий режим без таймаута — подменяем на IO_TIMEOUT
        # (неблокирующий режим для poller'а остаётся как есть).
        self.settimeout(None if flag else 0.0)

    def _io(self, fn, *args):
        try:
            return fn(*args)
        except TimeoutError:
            # Для gunicorn это обычный обрыв соединения клиентом: без трейсбэка в журнале.
            raise ConnectionResetError(errno.ECONNRESET, "таймаут клиента") from None

    def do_handshake(self, *a):
        return self._io(super().do_handshake, *a)

    def recv(self, *a):
        return self._io(super().recv, *a)

    def recv_into(self, *a):
        return self._io(super().recv_into, *a)

    def read(self, *a):
        return self._io(super().read, *a)

    def sendall(self, *a):
        return self._io(super().sendall, *a)

    def send(self, *a):
        return self._io(super().send, *a)


def ssl_context(conf, default_ssl_context_factory):
    ctx = default_ssl_context_factory()
    ctx.sslsocket_class = _TimeoutSSLSocket
    return ctx


workers = 1          # один процесс: фоновый поток учёта трафика должен быть один
threads = 16
