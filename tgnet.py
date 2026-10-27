"""Сетевой слой бота: прямое подключение, HTTP-прокси (CONNECT), SOCKS5
и свой адрес API (например, Cloudflare Worker). Только стандартная библиотека.

Одна строка настройки:
    пусто                      — напрямую к api.telegram.org
    http://host:port           — HTTP-прокси (метод CONNECT)
    http://user:pass@host:port — то же, с логином и паролем
    socks5://host:port         — SOCKS5-прокси (имя хоста резолвит сам прокси)
    socks5://user:pass@host:port
    https://адрес              — свой адрес API (обратный прокси, Cloudflare Worker)
"""
import http.client
import socket
import ssl
import urllib.parse
import json
import base64

DEFAULT_BASE = "https://api.telegram.org"


class ProxyError(OSError):
    """Прокси не смог установить соединение."""


def parse_network(value):
    """Возвращает (api_base, proxy). Бросает ValueError с понятным текстом."""
    value = (value or "").strip()
    if not value:
        return DEFAULT_BASE, None
    if "://" not in value:
        raise ValueError("Укажи схему: socks5://host:port, http://host:port или https://адрес-api")
    u = urllib.parse.urlsplit(value)
    if u.scheme in ("http", "socks5", "socks5h"):
        try:
            port = u.port
        except ValueError:
            port = None
        if not u.hostname or not port:
            raise ValueError("В адресе прокси нужны хост и порт, например socks5://1.2.3.4:1080")
        return DEFAULT_BASE, u
    if u.scheme == "https" and u.hostname:
        return value.rstrip("/"), None
    raise ValueError("Поддерживаются схемы: socks5://, http:// (прокси) и https:// (адрес API)")


def describe(api_base, proxy):
    """Короткое описание для журнала — без логина, пароля и путей."""
    if proxy is not None:
        scheme = "socks5" if proxy.scheme.startswith("socks5") else "http"
        return f"через прокси {scheme}://{proxy.hostname}:{proxy.port}"
    host = urllib.parse.urlsplit(api_base).hostname
    if api_base != DEFAULT_BASE:
        return f"через свой адрес API ({host})"
    return "напрямую"


# ---------- установка соединения через прокси ----------
def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError("прокси закрыл соединение")
        buf += chunk
    return buf


def _http_connect(sock, host, port, proxy):
    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if proxy.username:
        cred = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode())
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("HTTP-прокси закрыл соединение")
        head += chunk
        if len(head) > 16384:
            raise ProxyError("HTTP-прокси прислал слишком длинный ответ")
    status = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = status.split(" ", 2)
    if len(parts) < 2 or parts[1] != "200":
        raise ProxyError(f"HTTP-прокси отказал: {status}")


_SOCKS_ERRORS = {
    1: "общий сбой", 2: "запрещено правилами прокси", 3: "сеть недоступна",
    4: "хост недоступен", 5: "соединение отклонено", 6: "TTL истёк",
    7: "команда не поддерживается", 8: "тип адреса не поддерживается",
}


def _socks5_connect(sock, host, port, proxy):
    user = urllib.parse.unquote(proxy.username) if proxy.username else None
    pwd = urllib.parse.unquote(proxy.password or "") if proxy.username else None
    methods = b"\x00\x02" if user is not None else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    ver, method = _recv_exact(sock, 2)
    if ver != 5:
        raise ProxyError("это не SOCKS5-прокси")
    if method == 0xFF:
        raise ProxyError("SOCKS5-прокси не принял способ авторизации")
    if method == 0x02:
        if user is None:
            raise ProxyError("SOCKS5-прокси требует логин и пароль")
        u, p = user.encode(), pwd.encode()
        sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        if _recv_exact(sock, 2)[1] != 0:
            raise ProxyError("SOCKS5: неверный логин или пароль")
    # адрес передаём именем — так DNS-запрос делает прокси, а не телефон
    h = host.encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + port.to_bytes(2, "big"))
    _, rep, _, atyp = _recv_exact(sock, 4)
    if rep != 0:
        raise ProxyError(f"SOCKS5-прокси отказал: {_SOCKS_ERRORS.get(rep, rep)}")
    skip = {1: 4, 4: 16}.get(atyp)
    if skip is None:
        skip = _recv_exact(sock, 1)[0]
    _recv_exact(sock, skip + 2)


def open_socket(host, port, proxy, timeout):
    if proxy is None:
        return socket.create_connection((host, port), timeout)
    try:
        sock = socket.create_connection((proxy.hostname, proxy.port), timeout)
    except OSError as e:
        raise ProxyError(f"не удалось подключиться к прокси {proxy.hostname}:{proxy.port}: {e}")
    try:
        if proxy.scheme == "http":
            _http_connect(sock, host, port, proxy)
        else:
            _socks5_connect(sock, host, port, proxy)
    except BaseException:
        sock.close()
        raise
    return sock


class _TlsConn(http.client.HTTPSConnection):
    def __init__(self, host, port, proxy, ctx, timeout):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._proxy = proxy

    def connect(self):
        sock = open_socket(self.host, self.port, self._proxy, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PlainConn(http.client.HTTPConnection):  # нужен только для тестов
    def __init__(self, host, port, proxy, timeout):
        super().__init__(host, port, timeout=timeout)
        self._proxy = proxy

    def connect(self):
        self.sock = open_socket(self.host, self.port, self._proxy, self.timeout)


def post_json(base, path, payload, timeout, proxy, ctx):
    """POST JSON на base+path. Возвращает (HTTP-статус, тело ответа в байтах)."""
    u = urllib.parse.urlsplit(base)
    tls = u.scheme == "https"
    port = u.port or (443 if tls else 80)
    conn = (_TlsConn(u.hostname, port, proxy, ctx, timeout) if tls
            else _PlainConn(u.hostname, port, proxy, timeout))
    try:
        conn.request(
            "POST", u.path.rstrip("/") + path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    except http.client.HTTPException as e:  # приводим к обычной сетевой ошибке
        raise ConnectionError(f"обрыв HTTP-обмена: {e}")
    finally:
        conn.close()
