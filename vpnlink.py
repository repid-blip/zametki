"""Разбор VPN-ключей и подписок (как в Happ / v2rayNG) и генерация конфига Xray.

Поддерживаются ссылки:  vless://  vmess://  trojan://  ss://
и ссылки-подписки (https://…), которые отдают список таких ссылок
(обычный текст или base64).
"""
import base64
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

SCHEMES = ("vless", "vmess", "trojan", "ss")
SUB_USER_AGENT = "v2rayNG/1.9.0"
MAX_SUB_BYTES = 5 * 1024 * 1024


@dataclass
class Node:
    protocol: str   # vless | vmess | trojan | ss
    name: str
    host: str
    port: int
    secret: str     # UUID (vless/vmess) или пароль (trojan/ss)
    params: dict = field(default_factory=dict)


# ---------- вспомогательное ----------
def clean_name(name, fallback):
    """Убираем эмодзи и флаги: шрифт приложения их не рисует."""
    name = "".join(c for c in (name or "") if ord(c) < 0x2000 and c.isprintable())
    name = re.sub(r"\s+", " ", name).strip(" -|")
    return name[:40] or fallback


def _b64(s):
    s = re.sub(r"\s+", "", s).replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def _qs(query):
    return {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}


def _truthy(v):
    return str(v).lower() in ("1", "true", "yes")


def _split_url(link):
    try:
        u = urllib.parse.urlsplit(link)
        port = u.port
    except ValueError:
        raise ValueError("некорректный адрес в ссылке")
    if not u.hostname or not port:
        raise ValueError("в ссылке нет адреса или порта")
    return u, u.hostname, port


# ---------- разбор отдельных ссылок ----------
def _parse_vless(link):
    u, host, port = _split_url(link)
    uid = urllib.parse.unquote(u.username or "")
    if not uid:
        raise ValueError("в ссылке VLESS нет UUID")
    name = clean_name(urllib.parse.unquote(u.fragment), f"{host}:{port}")
    return Node("vless", name, host, port, uid, _qs(u.query))


def _parse_trojan(link):
    u, host, port = _split_url(link)
    pw = urllib.parse.unquote(u.username or "")
    if u.password is not None:
        pw += ":" + urllib.parse.unquote(u.password)
    if not pw:
        raise ValueError("в ссылке Trojan нет пароля")
    name = clean_name(urllib.parse.unquote(u.fragment), f"{host}:{port}")
    return Node("trojan", name, host, port, pw, _qs(u.query))


def _parse_vmess(link):
    body = link[len("vmess://"):]
    if "@" in body:  # «URL-стиль»: vmess://uuid@host:port?...
        u, host, port = _split_url(link)
        uid = urllib.parse.unquote(u.username or "")
        if not uid:
            raise ValueError("в ссылке VMess нет UUID")
        p = _qs(u.query)
        p.setdefault("scy", p.get("encryption", "auto"))
        name = clean_name(urllib.parse.unquote(u.fragment), f"{host}:{port}")
        return Node("vmess", name, host, port, uid, p)
    try:
        d = json.loads(_b64(body.split("#")[0]).decode("utf-8"))
        host, uid = str(d["add"]).strip(), str(d["id"]).strip()
        port = int(d["port"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise ValueError("не удалось разобрать VMess-ссылку")
    if not host or not uid or not port:
        raise ValueError("в ссылке VMess нет адреса, порта или UUID")
    net = str(d.get("net") or "tcp")
    tls = str(d.get("tls", "")).lower() in ("tls", "1", "true")
    p = {
        "type": net, "security": "tls" if tls else "none",
        "sni": d.get("sni") or "", "host": d.get("host") or "", "path": d.get("path") or "",
        "fp": d.get("fp") or "", "alpn": d.get("alpn") or "",
        "headerType": d.get("type") or "none", "aid": d.get("aid") or 0, "scy": d.get("scy") or "auto",
    }
    if net == "grpc":
        p["serviceName"] = d.get("path") or ""
        p["mode"] = "multi" if d.get("type") == "multi" else "gun"
    name = clean_name(str(d.get("ps") or ""), f"{host}:{port}")
    return Node("vmess", name, host, port, uid, p)


def _parse_ss(link):
    rest = link[len("ss://"):]
    rest, _, frag = rest.partition("#")
    rest, _, query = rest.partition("?")
    rest = rest.rstrip("/")
    if _qs(query).get("plugin"):
        raise ValueError("Shadowsocks с плагином не поддерживается")
    try:
        if "@" not in rest:  # старый формат: ss://base64(method:pass@host:port)
            rest = _b64(urllib.parse.unquote(rest)).decode("utf-8")
        userinfo, _, hostport = rest.rpartition("@")
        ui = urllib.parse.unquote(userinfo)
        if ":" not in ui:    # SIP002: base64(method:pass)
            ui = _b64(ui).decode("utf-8")
        method, _, password = ui.partition(":")
        host, _, port_s = hostport.rpartition(":")
        host, port = host.strip("[]"), int(port_s)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("не удалось разобрать Shadowsocks-ссылку")
    if not (method and password and host and port):
        raise ValueError("в ссылке Shadowsocks не хватает данных")
    name = clean_name(urllib.parse.unquote(frag), f"{host}:{port}")
    return Node("ss", name, host, port, password, {"method": method})


_PARSERS = {"vless": _parse_vless, "vmess": _parse_vmess, "trojan": _parse_trojan, "ss": _parse_ss}


def parse_link(link):
    link = link.strip()
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""
    if scheme not in _PARSERS:
        raise ValueError("неизвестная схема ссылки")
    return _PARSERS[scheme](link)


# ---------- подписки ----------
def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def decode_subscription(body):
    """Тело ответа подписки -> список строк-ссылок."""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return []

    def links_of(t):
        return [l.strip() for l in t.splitlines()
                if "://" in l and l.strip().split("://", 1)[0].lower() in SCHEMES]

    found = links_of(text)
    if found:
        return found
    try:
        return links_of(_b64(text).decode("utf-8", "replace"))
    except ValueError:
        return []


def fetch_subscription(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": SUB_USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        body = r.read(MAX_SUB_BYTES)
    return decode_subscription(body)


def split_sources(text):
    return [t for t in re.split(r"\s+", (text or "").strip()) if t]


def validate_source(text):
    """Быстрая проверка введённого текста (для интерфейса). Возвращает (ссылок, подписок)."""
    links = subs = 0
    for src in split_sources(text):
        low = src.lower()
        if low.startswith(("http://", "https://")):
            subs += 1
        elif "://" in low and low.split("://", 1)[0] in SCHEMES:
            parse_link(src)  # бросит ValueError с понятным текстом
            links += 1
        else:
            raise ValueError("ожидаю vless://, vmess://, trojan://, ss:// или https://-подписку")
    if not (links or subs):
        raise ValueError("ключ пуст")
    return links, subs


def load_nodes(text, log=lambda m: None, cache=None, fetch=fetch_subscription):
    """Список узлов из ключей и подписок.
    cache — словарь {id подписки: [ссылки]}: обновляется при успехе,
    при недоступной подписке берутся прошлые данные."""
    cache = cache if cache is not None else {}
    nodes, seen, n_sub = [], set(), 0
    for src in split_sources(text):
        if src.lower().startswith(("http://", "https://")):
            n_sub += 1
            key = "sub:" + hashlib.sha1(src.encode()).hexdigest()[:12]
            try:
                links = fetch(src)
                if links:
                    cache[key] = links
                else:
                    log(f"Подписка №{n_sub}: пустой список или неподдерживаемый формат")
                    links = cache.get(key, [])
            except Exception as e:
                log(f"Подписка №{n_sub} недоступна ({type(e).__name__}); беру сохранённые узлы")
                links = cache.get(key, [])
        else:
            links = [src]
        for link in links:
            try:
                node = parse_link(link)
            except ValueError as e:
                log(f"Узел пропущен: {e}")
                continue
            ident = (node.protocol, node.host, node.port, node.secret)
            if ident not in seen:
                seen.add(ident)
                nodes.append(node)
    return nodes


# ---------- конфиг Xray ----------
def resolve_host(host, port):
    """Резолвим адрес сервера системным резолвером Android: Go-программы
    без cgo на Android не умеют читать системные DNS-настройки."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return host


def _stream(node):
    p = node.params
    net = (p.get("type") or "tcp").lower()
    sec = (p.get("security") or ("tls" if node.protocol == "trojan" else "none")).lower()
    host_hdr = p.get("host") or ""
    st = {}
    if net in ("tcp", "raw"):
        st["network"] = "tcp"
        if (p.get("headerType") or "none") == "http":
            st["tcpSettings"] = {"header": {"type": "http", "request": {
                "path": [p.get("path") or "/"],
                "headers": {"Host": [h for h in host_hdr.split(",") if h] or [node.host]}}}}
    elif net == "ws":
        st["network"] = "ws"
        st["wsSettings"] = {"path": p.get("path") or "/", "headers": {"Host": host_hdr or node.host}}
    elif net == "grpc":
        st["network"] = "grpc"
        st["grpcSettings"] = {"serviceName": p.get("serviceName") or p.get("path") or "",
                              "multiMode": (p.get("mode") or "") == "multi"}
    elif net == "httpupgrade":
        st["network"] = "httpupgrade"
        st["httpupgradeSettings"] = {"path": p.get("path") or "/", "host": host_hdr or node.host}
    elif net in ("xhttp", "splithttp"):
        st["network"] = "xhttp"
        x = {"path": p.get("path") or "/", "host": host_hdr or node.host, "mode": p.get("mode") or "auto"}
        if p.get("extra"):
            try:
                x["extra"] = json.loads(p["extra"])
            except ValueError:
                pass
        st["xhttpSettings"] = x
    else:
        raise ValueError(f"транспорт «{net}» не поддерживается")

    if sec == "tls":
        t = {"serverName": p.get("sni") or p.get("peer") or node.host,
             "fingerprint": p.get("fp") or "chrome"}
        if p.get("alpn"):
            t["alpn"] = [a for a in p["alpn"].split(",") if a]
        if _truthy(p.get("allowInsecure") or p.get("insecure") or ""):
            t["allowInsecure"] = True
        st["security"], st["tlsSettings"] = "tls", t
    elif sec == "reality":
        if not p.get("pbk"):
            raise ValueError("в ссылке Reality нет publicKey (pbk)")
        st["security"] = "reality"
        st["realitySettings"] = {
            "serverName": p.get("sni") or node.host, "fingerprint": p.get("fp") or "chrome",
            "publicKey": p["pbk"], "shortId": p.get("sid") or "", "spiderX": p.get("spx") or "",
        }
    elif sec not in ("none", ""):
        raise ValueError(f"защита «{sec}» не поддерживается")
    return st


def _outbound(node, addr):
    p = node.params
    if node.protocol == "vless":
        user = {"id": node.secret, "encryption": p.get("encryption") or "none"}
        if p.get("flow"):
            user["flow"] = p["flow"]
        settings = {"vnext": [{"address": addr, "port": node.port, "users": [user]}]}
        proto = "vless"
    elif node.protocol == "vmess":
        try:
            aid = int(p.get("aid") or 0)
        except ValueError:
            aid = 0
        user = {"id": node.secret, "alterId": aid, "security": p.get("scy") or "auto"}
        settings = {"vnext": [{"address": addr, "port": node.port, "users": [user]}]}
        proto = "vmess"
    elif node.protocol == "trojan":
        settings = {"servers": [{"address": addr, "port": node.port, "password": node.secret}]}
        proto = "trojan"
    elif node.protocol == "ss":
        settings = {"servers": [{"address": addr, "port": node.port,
                                 "method": p["method"], "password": node.secret}]}
        proto = "shadowsocks"
    else:
        raise ValueError("неизвестный протокол")
    out = {"tag": "proxy", "protocol": proto, "settings": settings}
    if node.protocol != "ss":
        out["streamSettings"] = _stream(node)
    return out


def build_xray_config(node, socks_port, user, password, address=None, loglevel="warning"):
    """Конфиг: локальный SOCKS5 (с паролем) на 127.0.0.1 -> выбранный узел."""
    return {
        "log": {"loglevel": loglevel},
        "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
        "inbounds": [{
            "tag": "in", "listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
            "settings": {"auth": "password", "accounts": [{"user": user, "pass": password}], "udp": False},
        }],
        "outbounds": [_outbound(node, address or node.host), {"tag": "direct", "protocol": "freedom"}],
    }
