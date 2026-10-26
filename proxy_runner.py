"""Запуск локального MTProto-прокси (код из проекта tg-ws-proxy) в отдельном
потоке сервиса — рядом с ботом для заметок.

Telegram на этом же телефоне подключается к 127.0.0.1:<порт>, а прокси
пересылает трафик через WebSocket-соединения.
"""
import asyncio
import errno
import logging
import threading

DC_IP = ["2:149.154.167.220", "4:149.154.167.220"]


class _LogHandler(logging.Handler):
    """Перенаправляет журнал прокси в общий журнал приложения."""

    def __init__(self, fn):
        super().__init__(logging.INFO)
        self.fn = fn
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            msg = self.format(record)
            if not msg.strip() or set(msg.strip()) == {"="}:
                return  # декоративные линии из баннера
            self.fn("[прокси] " + msg)
        except Exception:
            pass


class ProxyRunner:
    def __init__(self, log):
        self._log = log
        self.thread = None
        self.error = None
        self._mod = None
        self._loop = None
        self._stop_ev = None
        self._want_stop = False
        self._handler = None
        self._port = None

    @property
    def alive(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def listening(self):
        return bool(self._mod and getattr(self._mod, "_server_instance", None) is not None)

    # ---------- запуск ----------
    def start(self, port, secret):
        self.error = None
        self._want_stop = False
        self._port = port
        try:
            from proxy import parse_dc_ip_list, proxy_config
            from proxy import tg_ws_proxy as mod

            # заранее проверяем, что AES доступен (иначе прокси не заработает)
            from proxy._aes import Cipher, algorithms, modes
            Cipher(algorithms.AES(bytes(32)), modes.CTR(bytes(16))).encryptor().update(b"x")
        except Exception as e:
            self.error = f"Прокси не запустился: {e}"
            self._log(f"[прокси] {self.error}")
            return False
        self._mod = mod

        proxy_config.host = "127.0.0.1"
        proxy_config.port = int(port)
        proxy_config.secret = secret
        proxy_config.dc_redirects = parse_dc_ip_list(DC_IP)
        proxy_config.buffer_size = 256 * 1024
        proxy_config.pool_size = 4
        proxy_config.fallback_cfproxy = True
        proxy_config.cfproxy_user_domains = []
        proxy_config.cfproxy_worker_domains = []
        proxy_config.cfproxy_h2_media = False  # HTTP/2 (httpx) в Android-версии не ставим
        proxy_config.disable_secure = False
        proxy_config.fake_tls_domain = ""
        proxy_config.proxy_protocol = False
        proxy_config.force_test_dc = False

        self._attach_log()
        self.thread = threading.Thread(target=self._work, daemon=True, name="proxy")
        self.thread.start()
        return True

    def _attach_log(self):
        lg = logging.getLogger("tg-mtproto-proxy")
        lg.setLevel(logging.INFO)
        if self._handler is None:
            self._handler = _LogHandler(self._log)
            lg.addHandler(self._handler)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    def _detach_log(self):
        if self._handler is not None:
            logging.getLogger("tg-mtproto-proxy").removeHandler(self._handler)
            self._handler = None

    def _work(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_ev = asyncio.Event()
        self._loop, self._stop_ev = loop, stop_ev
        if self._want_stop:
            stop_ev.set()
        try:
            loop.run_until_complete(self._mod._run(stop_event=stop_ev))
        except Exception as e:
            if isinstance(e, OSError) and e.errno == errno.EADDRINUSE:
                self.error = f"Порт {self._port} занят другим приложением"
            else:
                self.error = f"Ошибка прокси: {e}"
            self._log(f"[прокси] {self.error}")
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = self._stop_ev = None

    # ---------- остановка ----------
    def stop(self):
        self._want_stop = True
        loop, ev = self._loop, self._stop_ev
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=8)
            if self.thread.is_alive():
                self._log("[прокси] Поток не остановился за 8 с — порт может быть занят")
            else:
                self._log("[прокси] Остановлен")
        self.thread = None
        self._detach_log()
