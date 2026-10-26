"""Общие функции для интерфейса (main.py) и фонового сервиса (service.py).

Интерфейс и сервис работают в РАЗНЫХ процессах и общаются через файлы
в приватной папке приложения: config.json, status.json, bot.log.
"""
import json
import os
import time


def data_dir() -> str:
    d = os.environ.get("ANDROID_PRIVATE")  # на Android — внутренняя папка приложения
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".tgnotesbot")  # на ПК — для тестов
    os.makedirs(d, exist_ok=True)
    return d


def file_path(name: str) -> str:
    return _path(name)


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def _write_json(name: str, obj: dict) -> None:
    path = _path(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(name: str) -> dict:
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- настройки: {"token": "...", "enabled": true/false,
#                  "proxy_enabled": true/false, "proxy_port": 1443, "proxy_secret": "32 hex"} ---
def load_config() -> dict:
    return _read_json("config.json")


def save_config(cfg: dict) -> None:
    _write_json("config.json", cfg)


# --- локальный MTProto-прокси для Telegram ---
PROXY_HOST = "127.0.0.1"
PROXY_DEFAULT_PORT = 1443


def ensure_proxy_config() -> dict:
    """Гарантирует, что в настройках есть порт и секрет прокси (создаёт при первом запуске)."""
    cfg = load_config()
    changed = False
    secret = str(cfg.get("proxy_secret", ""))
    try:
        bytes.fromhex(secret)
        valid = len(secret) == 32
    except ValueError:
        valid = False
    if not valid:
        cfg["proxy_secret"] = os.urandom(16).hex()
        changed = True
    try:
        port = int(cfg.get("proxy_port", 0))
    except (TypeError, ValueError):
        port = 0
    if not 1024 <= port <= 65535:
        cfg["proxy_port"] = PROXY_DEFAULT_PORT
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def proxy_link(cfg: dict) -> str:
    """Ссылка для Telegram: tg://proxy?... (секрет с префиксом dd)."""
    return (f"tg://proxy?server={PROXY_HOST}&port={cfg['proxy_port']}"
            f"&secret=dd{cfg['proxy_secret']}")


# --- статус прокси: {"state": "running|starting|error", "msg": "...", "ts": ...} ---
def write_proxy_status(state: str, **extra) -> None:
    _write_json("proxy_status.json", {"state": state, "ts": time.time(), **extra})


def read_proxy_status() -> dict:
    return _read_json("proxy_status.json")


def clear_proxy_status() -> None:
    try:
        os.remove(_path("proxy_status.json"))
    except OSError:
        pass


# --- статус сервиса: {"state": "running|starting|error", "bot": "...", "msg": "...", "ts": ...} ---
def write_status(state: str, **extra) -> None:
    _write_json("status.json", {"state": state, "ts": time.time(), **extra})


def read_status() -> dict:
    return _read_json("status.json")


def clear_status() -> None:
    try:
        os.remove(_path("status.json"))
    except OSError:
        pass


# --- журнал ---
LOG_MAX_BYTES = 200_000


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    path = _path("bot.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-50_000, 2)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def tail_log(max_lines: int = 60) -> str:
    try:
        with open(_path("bot.log"), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8000))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except OSError:
        return ""
