"""Фоновый сервис: здесь живут бот для заметок и локальный прокси для Telegram.
Запускается из main.py как Android-сервис (или как обычный процесс при проверке
на компьютере).

Сервис следит за config.json и сам запускает/останавливает:
  • бота      — когда "enabled": true и задан токен;
  • прокси    — когда "proxy_enabled": true.
Когда не нужно ни то ни другое, сервис завершается.
"""
import os
import threading
import time
import traceback

from bot_core import Fatal, NotesBot
from common import (
    data_dir,
    file_path,
    load_config,
    log,
    save_config,
    write_proxy_status,
    write_status,
)
from proxy_runner import ProxyRunner

LOOP_PERIOD = 2        # как часто проверяем настройки, сек
BOT_RESTART_GAP = 10   # не перезапускать упавший поток бота чаще, сек
LOCK_NAME = "service.lock"


def acquire_wakelock():
    """Не даём процессору уснуть вместе с экраном (только Android)."""
    try:
        from jnius import autoclass

        PythonService = autoclass("org.kivy.android.PythonService")
        Context = autoclass("android.content.Context")
        pm = PythonService.mService.getSystemService(Context.POWER_SERVICE)
        wl = pm.newWakeLock(1, "tgnotesbot:polling")  # 1 = PARTIAL_WAKE_LOCK
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
        with open(file_path(LOCK_NAME), encoding="utf-8") as f:
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
        with open(file_path(LOCK_NAME), "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {time.time()}")
    except OSError:
        pass


def _remove_lock():
    try:
        os.remove(file_path(LOCK_NAME))
    except OSError:
        pass


# ---------- бот ----------
class BotRunner:
    def __init__(self):
        self.thread = None
        self.last_start = 0.0

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, token):
        self.last_start = time.time()
        self.thread = threading.Thread(
            target=self._work, args=(token,), daemon=True, name="bot"
        )
        self.thread.start()

    @staticmethod
    def _work(token):
        def should_stop():
            return not load_config().get("enabled")

        write_status("starting")
        log("— Бот запущен —")
        try:
            bot = NotesBot(
                token,
                os.path.join(data_dir(), "notes.db"),
                log,
                should_stop=should_stop,
                on_status=write_status,
            )
            while not should_stop():
                try:
                    bot.run()
                except Fatal as e:
                    log(f"Ошибка: {e}")
                    write_status("error", msg=str(e))
                    save_config({**load_config(), "enabled": False})
                    break
                except Exception:
                    bot.log(traceback.format_exc())
                    time.sleep(10)
        except Exception:
            log(traceback.format_exc())
        finally:
            log("— Бот остановлен —")


# ---------- главный цикл ----------
def main():
    cfg = load_config()
    bot_wanted = bool(cfg.get("enabled") and cfg.get("token", "").strip())
    if not bot_wanted and not cfg.get("proxy_enabled"):
        return  # сервис перезапущен системой, но пользователь ничего не включал
    if _another_instance_alive():
        return  # уже работает другой экземпляр сервиса
    _touch_lock()

    wakelock = acquire_wakelock()
    log("— Сервис запущен —")
    bot = BotRunner()
    proxy = ProxyRunner(log)
    proxy_started_at = 0.0

    try:
        while True:
            _touch_lock()
            cfg = load_config()
            token = cfg.get("token", "").strip()
            want_bot = bool(cfg.get("enabled") and token)
            want_proxy = bool(cfg.get("proxy_enabled"))

            if not want_bot and not want_proxy and not bot.alive and not proxy.alive:
                break

            # --- бот ---
            if want_bot and not bot.alive and time.time() - bot.last_start >= BOT_RESTART_GAP:
                bot.start(token)

            # --- прокси ---
            if want_proxy and not proxy.alive and proxy.error is None:
                try:
                    port = int(cfg.get("proxy_port", 1443))
                    secret = str(cfg.get("proxy_secret", ""))
                    if proxy.start(port, secret):
                        proxy_started_at = time.time()
                        write_proxy_status("starting")
                except Exception:
                    proxy.error = "Прокси не запустился, подробности в журнале"
                    log(traceback.format_exc())
            if not want_proxy:
                if proxy.alive:
                    proxy.stop()
                proxy.error = None  # при следующем включении пробуем заново

            if want_proxy:
                if proxy.error:
                    # крах прокси: фиксируем ошибку и выключаем его в настройках
                    write_proxy_status("error", msg=proxy.error)
                    save_config({**load_config(), "proxy_enabled": False})
                elif proxy.alive and proxy.listening:
                    write_proxy_status("running", port=cfg.get("proxy_port"))
                elif proxy.alive and time.time() - proxy_started_at > 20:
                    write_proxy_status("starting", msg="жду открытия порта…")
                elif not proxy.alive:
                    # поток завершился без ошибки (неожиданно) — перезапустим
                    proxy.error = None
                    proxy.thread = None

            time.sleep(LOOP_PERIOD)
    finally:
        if proxy.alive:
            proxy.stop()
        log("— Сервис остановлен —")
        if wakelock:
            try:
                wakelock.release()
            except Exception:
                pass
        _remove_lock()
        stop_self()


main()
