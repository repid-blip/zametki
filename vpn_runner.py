"""Запуск Xray с выбранным узлом из VPN-ключа (vless / vmess / trojan / ss).

На Android бинарники xray упаковываются в APK как .so-библиотеки (android.add_libs_*).
Android автоматически извлекает их в nativeLibraryDir, откуда их можно запускать
(в отличие от приватной папки приложения, где стоит запрет на выполнение).
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import time

import vpnlink
from common import data_dir, load_vpn_cache, save_vpn_cache


def _get_native_lib_dir():
    """Получает путь к папке nativeLibraryDir на Android (где лежат .so)."""
    try:
        from jnius import autoclass
        try:
            PythonService = autoclass('org.kivy.android.PythonService')
            return PythonService.mService.getApplicationInfo().nativeLibraryDir
        except Exception:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            return PythonActivity.mActivity.getApplicationInfo().nativeLibraryDir
    except Exception:
        return None


class VpnRunner:
    def __init__(self, log):
        self._log = log
        self.proc = None
        self.error = None
        self.node_name = ""
        self.socks_port = 0
        self.socks_user = ""
        self.socks_password = ""
        self._config_path = ""

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def proxy_url(self):
        if not self.alive or not self.socks_port:
            return ""
        return (f"socks5://{self.socks_user}:{self.socks_password}"
                f"@127.0.0.1:{self.socks_port}")

    @staticmethod
    def find_xray():
        # 1. Сначала ищем в PATH (обычно для ПК: Linux/macOS/Windows)
        names = ("xray.exe", "xray") if os.name == "nt" else ("xray",)
        for name in names:
            exe = shutil.which(name)
            if exe:
                return exe

        # 2. Ищем рядом с кодом проекта (для ПК)
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = []
        for name in names:
            candidates += [
                os.path.join(here, name),
                os.path.join(here, "bin", name),
            ]

        for p in candidates:
            if p and os.path.isfile(p):
                return p

        # 3. Android: ищем в nativeLibraryDir (куда попадают .so из APK)
        native_dir = _get_native_lib_dir()
        if native_dir:
            arch = platform.machine().lower()
            if arch in ("aarch64", "arm64", "arm64-v8a"):
                lib_name = "libxray_arm64.so"
            else:
                lib_name = "libxray_arm32.so"
            
            for name in (lib_name, "libxray.so"):
                p = os.path.join(native_dir, name)
                if os.path.isfile(p):
                    return p

        # 4. Fallback: ANDROID_PRIVATE (вдруг кто-то положил туда вручную)
        priv = os.environ.get("ANDROID_PRIVATE")
        if priv:
            for name in names:
                p = os.path.join(priv, name)
                if p and os.path.isfile(p):
                    return p
                    
        return None

    def start(self, vpn_text):
        self.error = None
        if self.alive:
            return True

        exe = self.find_xray()
        if not exe:
            self.error = ("xray не найден. Проверьте, что APK собран с "
                          "android.add_libs_* или установите xray в PATH.")
            self._log(f"[vpn] {self.error}")
            return False

        cache = load_vpn_cache()
        try:
            nodes = vpnlink.load_nodes(
                vpn_text,
                log=lambda m: self._log(f"[vpn] {m}"),
                cache=cache,
            )
        except Exception as e:
            self.error = f"Ошибка разбора ключа: {e}"
            self._log(f"[vpn] {self.error}")
            return False
        finally:
            try:
                save_vpn_cache(cache)
            except Exception:
                pass

        if not nodes:
            self.error = "Нет рабочих узлов в ключе/подписке"
            self._log(f"[vpn] {self.error}")
            return False

        node = nodes[0]
        self.node_name = node.name
        self.socks_port = self._free_port()
        self.socks_user = "u" + os.urandom(3).hex()
        self.socks_password = os.urandom(8).hex()

        try:
            cfg = vpnlink.build_xray_config(
                node, self.socks_port,
                self.socks_user, self.socks_password)
        except Exception as e:
            self.error = f"Конфиг Xray не собрался: {e}"
            self._log(f"[vpn] {self.error}")
            return False

        path = os.path.join(data_dir(), "xray-config.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False)
            self._config_path = path
        except OSError as e:
            self.error = f"Не удалось записать конфиг Xray: {e}"
            self._log(f"[vpn] {self.error}")
            return False

        self._log(f"[vpn] Узел «{node.name}» — "
                  f"{node.protocol} {node.host}:{node.port}")
        try:
            self.proc = subprocess.Popen(
                [exe, "run", "-c", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=data_dir(),
            )
        except Exception as e:
            self.error = f"xray не запустился: {e}"
            self._log(f"[vpn] {self.error}")
            self._cleanup()
            return False

        deadline = time.time() + 12
        while time.time() < deadline:
            if not self.alive:
                tail = b""
                try:
                    tail = self.proc.stderr.read(4096) or b""
                except Exception:
                    pass
                msg = tail.decode("utf-8", "replace").strip()
                self.error = (f"xray завершился (код {self.proc.returncode})"
                              + (f": {msg[:200]}" if msg else ""))
                self._log(f"[vpn] {self.error}")
                self._cleanup()
                return False
            if self._port_open():
                self._log(f"[vpn] SOCKS5 готов: 127.0.0.1:{self.socks_port}")
                return True
            time.sleep(0.2)

        self.error = "xray не открыл SOCKS5 за 12 секунд"
        self._log(f"[vpn] {self.error}")
        self.stop()
        return False

    def _port_open(self):
        try:
            with socket.create_connection(
                    ("127.0.0.1", self.socks_port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def stop(self):
        proc = self.proc
        self.proc = None
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
            self._log("[vpn] Остановлен")
        self._cleanup()

    def _cleanup(self):
        if self._config_path:
            try:
                os.remove(self._config_path)
            except OSError:
                pass
            self._config_path = ""
        self.socks_port = 0
        self.node_name = ""
