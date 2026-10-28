"""Фоновый сервис: здесь живут бот и VPN (Xray).

Запускается из main.py как Android-сервис (или как обычный процесс при
проверке на компьютере).

Сервис следит за config.json и сам запускает/останавливает:
  • VPN (Xray) — когда "vpn_enabled": true и задан "vpn_links";
  • бота       — когда "enabled": true и задан токен.
Бот ходит в Telegram через SOCKS5, который поднимает Xray; если VPN
выключен, бот идёт напрямую. Когда не нужно ни то ни другое — сервис
завершается.
"""
import os
import threading
import time
import traceback

from bot_core import Fatal, NotesBot
from common import (
    clear_vpn_status,
    data_dir,
    load_config,
    log,
    save_config,
    write_status,
    write_vpn_status,
)
from vpn_runner import VpnRunner

LOOP_PERIOD = 2        # как часто проверяем настройки, сек
BOT_RESTART_GAP = 10   # не перезапускать упавший поток бота чаще, сек
LOCK_NAME = "service.lock"


def _lock_path():
    return os.path.join(data_dir(), LOCK_NAME)


def acquire_wakelock():
    """Не даём процессору уснуть вместе с экраном (только Android)."""
    try:
        from jnius import autoclass

        PythonService = autoclass("org.kivy.android.PythonService")
        Context = autoclass("android.content.Context")
        pm = PythonService.mService.getSystemService(Context.POWER_SERVICE)
        wl = pm.newWakeLock(1, "tgnotesbot:polling")  # PARTIAL_WAKE_LOCK
        wl.acquire()
        return wl
    except Exception:
        return None


def stop_self():
    try:
        from jnius import autoclass

        autoclass("org.kivy.android.PythonService").mService.stopSelf()
    except Exception:
        pass


# ---------- защита от двойного запуска ----------
def _another_instance_alive():
    try:
        with open(_lock_path(), encoding="utf-8") as f:
            pid_s, ts_s = f.read().split()
        pid, ts = int(pid_s), float(ts_s)
    except (OSError, ValueError):
        return False
    if time.time() - ts > 3 * LOOP_PERIOD + 4:
        return False  # «пульс» давно не обновлялся — прежний экземпляр умер
    if pid == os.getpid():
        return True   # повторный запуск в этом же процессе
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _touch_lock():
    try:
        with open(_lock_path(), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {time.time()}")
    except OSError:
        pass


def _remove_lock():
    try:
        os.remove(_lock_path())
    except OSError:
        pass


# ---------- бот ----------
class BotRunner:
    def __init__(self):
        self.thread = None
        self._stop = threading.Event()
        self.proxy_used = ""
        self.last_start = 0.0

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, token, db_path, proxy_url):
        self.proxy_used = proxy_url or ""
        self.last_start = time.time()
        self._stop.clear()

        # Прокси передаём боту напрямую (не через config.json): так нет гонки
        # с интерфейсом и пароль SOCKS не лежит на диске.
        self.thread = threading.Thread(
            target=self._work, args=(token, db_path, self._stop, self.proxy_used),
            daemon=True, name="bot")
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=45)
            self.thread = None

    @staticmethod
    def _work(token, db_path, stop_ev, proxy_url):
        def should_stop():
            return stop_ev.is_set() or not load_config().get("enabled")

        write_status("starting")
        log("— Бот запущен —")
        try:
            bot = NotesBot(
                token, db_path, log,
                should_stop=should_stop, on_status=write_status,
                proxy=proxy_url)
        except Fatal as e:
            log(f"Ошибка: {e}")
            write_status("error", msg=str(e))
            save_config({**load_config(), "enabled": False})
            return
        except Exception:
            log(traceback.format_exc())
            write_status("error", msg="См. журнал")
            save_config({**load_config(), "enabled": False})
            return

        try:
            bot.run()
        except Fatal as e:
            log(f"Ошибка: {e}")
            write_status("error", msg=str(e))
            save_config({**load_config(), "enabled": False})
        except Exception:
            bot.log(traceback.format_exc())
        finally:
            log("— Бот остановлен —")


# ---------- главный цикл ----------
def main():
    cfg = load_config()
    want_bot_start = bool(cfg.get("enabled") and cfg.get("token", "").strip())
    want_vpn_start = bool(cfg.get("vpn_enabled") and cfg.get("vpn_links", "").strip())
    if not want_bot_start and not want_vpn_start:
        return
    if _another_instance_alive():
        return
    _touch_lock()

    wakelock = acquire_wakelock()
    log("— Сервис запущен —")
    bot = BotRunner()
    vpn = VpnRunner(log)

    try:
        while True:
            _touch_lock()
            cfg = load_config()
            token = cfg.get("token", "").strip()
            vpn_links = cfg.get("vpn_links", "").strip()
            want_bot = bool(cfg.get("enabled") and token)
            want_vpn = bool(cfg.get("vpn_enabled") and vpn_links)

            if not want_bot and not want_vpn and not bot.alive and not vpn.alive:
                break

            # --- VPN ---
            if want_vpn and not vpn.alive and vpn.error is None:
                write_vpn_status("starting")
                if vpn.start(vpn_links):
                    write_vpn_status("running", node=vpn.node_name)
                else:
                    write_vpn_status("error", msg=vpn.error or "не запустился")
                    save_config({**load_config(), "vpn_enabled": False})
            elif not want_vpn:
                if vpn.alive:
                    vpn.stop()
                vpn.error = None
                clear_vpn_status()
            elif vpn.alive:
                write_vpn_status("running", node=vpn.node_name)

            # --- актуальный прокси для бота ---
            desired_proxy = vpn.proxy_url if vpn.alive else ""

            # Если бот уже работает с другим прокси (включили/выключили VPN) —
            # перезапустим его, чтобы он подхватил новый SOCKS5.
            if bot.alive and bot.proxy_used != desired_proxy:
                bot.stop()

            # --- бот ---
            if want_bot and not bot.alive and time.time() - bot.last_start >= BOT_RESTART_GAP:
                if want_vpn and not vpn.alive and not vpn.error:
                    write_status("starting", msg="жду VPN…")
                else:
                    bot.start(
                        token,
                        os.path.join(data_dir(), "notes.db"),
                        desired_proxy,
                    )

            time.sleep(LOOP_PERIOD)
    finally:
        if bot.alive:
            bot.stop()
        if vpn.alive:
            vpn.stop()
        log("— Сервис остановлен —")
        if wakelock:
            try:
                wakelock.release()
            except Exception:
                pass
        _remove_lock()
        stop_self()


main()