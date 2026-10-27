"""Общие функции для интерфейса (main.py) и фонового сервиса (service.py).

Интерфейс и сервис работают в РАЗНЫХ процессах и общаются через файлы
в приватной папке приложения: config.json, status.json, vpn_status.json,
vpn_cache.json, bot.log.
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


# --- настройки ---
def load_config() -> dict:
    return _read_json("config.json")


def save_config(cfg: dict) -> None:
    _write_json("config.json", cfg)


# --- статус сервиса (бота) ---
def write_status(state: str, **extra) -> None:
    _write_json("status.json", {"state": state, "ts": time.time(), **extra})


def read_status() -> dict:
    return _read_json("status.json")


def clear_status() -> None:
    try:
        os.remove(_path("status.json"))
    except OSError:
        pass


# --- статус VPN ---
def write_vpn_status(state: str, **extra) -> None:
    _write_json("vpn_status.json", {"state": state, "ts": time.time(), **extra})


def read_vpn_status() -> dict:
    return _read_json("vpn_status.json")


def clear_vpn_status() -> None:
    try:
        os.remove(_path("vpn_status.json"))
    except OSError:
        pass


# --- кеш узлов из подписок ---
VPN_CACHE_NAME = "vpn_cache.json"


def load_vpn_cache() -> dict:
    return _read_json(VPN_CACHE_NAME)


def save_vpn_cache(cache: dict) -> None:
    _write_json(VPN_CACHE_NAME, cache)


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