"""Фоновый сервис: здесь живёт бот. Запускается из main.py как Android-сервис
(или как обычный процесс при проверке на компьютере)."""
import os
import time
import traceback

from bot_core import Fatal, NotesBot
from common import data_dir, load_config, log, save_config, write_status


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


def main():
    cfg = load_config()
    token = cfg.get("token", "").strip()
    if not cfg.get("enabled") or not token:
        return  # сервис перезапущен системой, но пользователь бота не включал

    write_status("starting")
    wakelock = acquire_wakelock()
    log("— Сервис запущен —")

    def should_stop():
        return not load_config().get("enabled")

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
    finally:
        log("— Сервис остановлен —")
        if wakelock:
            try:
                wakelock.release()
            except Exception:
                pass
        stop_self()


main()
