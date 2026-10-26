"""Notes Bot Launcher — Android-приложение, которое запускает Telegram-бота
для заметок прямо на телефоне (фоновый сервис + пастельно-розовый интерфейс).

Проверка на компьютере:  pip install kivy certifi && python main.py
"""
import os
import re
import subprocess
import sys
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (
    BooleanProperty,
    ColorProperty,
    NumericProperty,
    StringProperty,
)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.utils import platform

from common import (
    clear_status,
    load_config,
    read_status,
    save_config,
    tail_log,
)

BG = (1, 0.94, 0.96, 1)
ACCENT = (0.96, 0.66, 0.76, 1)
SOFT = (1, 0.86, 0.91, 1)
DARK_TEXT = (0.6, 0.25, 0.4, 1)

DOT_COLORS = {
    "stopped": (0.86, 0.72, 0.78, 1),
    "starting": (1, 0.8, 0.5, 1),
    "running": (0.5, 0.8, 0.62, 1),
    "noresp": (1, 0.8, 0.5, 1),
    "error": (0.93, 0.45, 0.5, 1),
}
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
STALE_AFTER = 75  # секунд без «пульса» от сервиса — считаем, что он не отвечает

KV = """
#:import dp kivy.metrics.dp
#:set BG (1, 0.94, 0.96, 1)
#:set ACCENT (0.96, 0.66, 0.76, 1)
#:set SOFT (1, 0.86, 0.91, 1)
#:set INK (0.45, 0.2, 0.32, 1)
#:set MUTED (0.72, 0.5, 0.6, 1)
#:set DARK_TEXT (0.6, 0.25, 0.4, 1)

<PinkButton>:
    size_hint_y: None
    height: dp(48)
    font_size: '15sp'
    bold: True
    color: 1, 1, 1, 1
    canvas.before:
        Color:
            rgba: (0.85, 0.5, 0.62, 0.3) if self.shadow else (0, 0, 0, 0)
        RoundedRectangle:
            pos: self.x, self.y - dp(3)
            size: self.size
            radius: [self.radius]
        Color:
            rgba: (self.bg[0] - .06, self.bg[1] - .06, self.bg[2] - .06, 1) if self.state == 'down' else self.bg
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.radius]

<InputBox>:
    padding: dp(18), dp(4)
    opacity: .55 if self.disabled else 1
    canvas.before:
        Color:
            rgba: 1, 1, 1, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(20)]
    TextInput:
        id: field
        hint_text: root.hint_text
        password: root.password
        multiline: False
        background_normal: ''
        background_active: ''
        background_color: 0, 0, 0, 0
        foreground_color: INK
        hint_text_color: 0.72, 0.5, 0.6, 0.8
        cursor_color: ACCENT
        selection_color: 0.96, 0.66, 0.76, 0.35
        font_size: '16sp'
        padding: 0, dp(11)
        write_tab: False

<Root>:
    canvas.before:
        Color:
            rgba: BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: dp(18), dp(14), dp(18), dp(18)
        spacing: dp(12)

        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: dp(72)
            Label:
                text: 'Notes Bot'
                color: INK
                bold: True
                font_size: '30sp'
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                size_hint_y: .62
            Label:
                text: 'Telegram-бот для заметок на этом телефоне'
                color: MUTED
                font_size: '14sp'
                halign: 'left'
                valign: 'top'
                text_size: self.size
                size_hint_y: .38

        BoxLayout:
            size_hint_y: None
            height: dp(84)
            padding: dp(20), dp(14)
            spacing: dp(16)
            canvas.before:
                Color:
                    rgba: 0.9, 0.6, 0.72, 0.2
                RoundedRectangle:
                    pos: self.x, self.y - dp(2)
                    size: self.size
                    radius: [dp(24)]
                Color:
                    rgba: 1, 1, 1, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
                    radius: [dp(24)]
            Widget:
                size_hint: None, None
                size: dp(18), dp(18)
                pos_hint: {'center_y': .5}
                canvas:
                    Color:
                        rgba: root.dot_color
                    Ellipse:
                        pos: self.pos
                        size: self.size
            BoxLayout:
                orientation: 'vertical'
                Label:
                    text: root.status_title
                    color: INK
                    bold: True
                    font_size: '18sp'
                    halign: 'left'
                    valign: 'bottom'
                    text_size: self.size
                Label:
                    text: root.status_detail
                    color: MUTED
                    font_size: '13sp'
                    halign: 'left'
                    valign: 'top'
                    max_lines: 2
                    text_size: self.size

        InputBox:
            id: token
            hint_text: 'Токен бота (от @BotFather)'
            password: True
            size_hint_y: None
            height: dp(52)
            disabled: root.running

        PinkButton:
            text: root.button_text
            bg: root.toggle_bg
            color: root.toggle_color
            height: dp(56)
            font_size: '17sp'
            radius: dp(28)
            shadow: True
            on_release: app.toggle()

        BoxLayout:
            size_hint_y: None
            height: dp(44)
            spacing: dp(10)
            PinkButton:
                text: 'Показать токен' if token.password else 'Скрыть токен'
                font_size: '13sp'
                height: dp(44)
                bg: SOFT
                color: DARK_TEXT
                on_release: token.password = not token.password
            PinkButton:
                text: 'Не засыпать (батарея)'
                font_size: '13sp'
                height: dp(44)
                bg: SOFT
                color: DARK_TEXT
                on_release: app.open_battery_settings()

        Label:
            text: root.message
            color: 0.8, 0.35, 0.45, 1
            font_size: '13sp'
            size_hint_y: None
            height: dp(18) if root.message else 0
            text_size: self.width, None
            halign: 'center'

        BoxLayout:
            orientation: 'vertical'
            padding: dp(16)
            spacing: dp(6)
            canvas.before:
                Color:
                    rgba: 0.9, 0.6, 0.72, 0.2
                RoundedRectangle:
                    pos: self.x, self.y - dp(2)
                    size: self.size
                    radius: [dp(24)]
                Color:
                    rgba: 1, 1, 1, 1
                RoundedRectangle:
                    pos: self.pos
                    size: self.size
                    radius: [dp(24)]
            Label:
                text: 'Журнал'
                color: MUTED
                bold: True
                font_size: '13sp'
                halign: 'left'
                text_size: self.size
                size_hint_y: None
                height: dp(20)
            ScrollView:
                id: log_scroll
                bar_width: dp(3)
                bar_color: ACCENT
                Label:
                    id: log_label
                    text: root.log_text
                    color: INK
                    font_size: '12sp'
                    halign: 'left'
                    valign: 'top'
                    text_size: self.width, None
                    size_hint_y: None
                    height: max(self.texture_size[1], log_scroll.height)
"""


class PinkButton(ButtonBehavior, Label):
    bg = ColorProperty(ACCENT)
    radius = NumericProperty(dp(24))
    shadow = BooleanProperty(False)


class InputBox(BoxLayout):
    hint_text = StringProperty("")
    password = BooleanProperty(False)


class Root(FloatLayout):
    status_title = StringProperty("")
    status_detail = StringProperty("")
    dot_color = ColorProperty(DOT_COLORS["stopped"])
    running = BooleanProperty(False)
    button_text = StringProperty("Запустить бота")
    toggle_bg = ColorProperty(ACCENT)
    toggle_color = ColorProperty((1, 1, 1, 1))
    log_text = StringProperty("")
    message = StringProperty("")


class BotLauncherApp(App):
    title = "Notes Bot"

    def build(self):
        Builder.load_string(KV)
        Window.clearcolor = BG
        if platform == "android":
            Window.softinput_mode = "below_target"
        self.proc = None  # процесс сервиса при проверке на ПК
        self.requested_at = 0.0
        return Root()

    def on_start(self):
        cfg = load_config()
        self.root.ids.token.ids.field.text = cfg.get("token", "")
        if cfg.get("enabled"):
            self.requested_at = time.time()
        if platform == "android":
            self._request_notification_permission()
        Clock.schedule_interval(self.tick, 1.5)
        self.tick()

    def on_pause(self):
        return True  # сервис продолжает работать, пока приложение свёрнуто

    # ---------- кнопки ----------
    def toggle(self):
        if load_config().get("enabled"):
            self.stop_bot()
        else:
            self.start_bot()

    def start_bot(self):
        token = self.root.ids.token.ids.field.text.strip()
        if not TOKEN_RE.match(token):
            return self.flash("Токен выглядит неверно. Он вида 123456:ABC-DEF…")
        clear_status()
        save_config({"token": token, "enabled": True})
        self.requested_at = time.time()
        # сначала останавливаем возможный «хвост» старого запуска, потом стартуем
        self._launch_stop()
        Clock.schedule_once(lambda dt: self._launch_start(), 0.7)
        self.tick()

    def stop_bot(self):
        cfg = load_config()
        save_config({**cfg, "enabled": False})
        clear_status()
        self._launch_stop()
        self.tick()

    def open_battery_settings(self):
        if platform != "android":
            return self.flash("Эта настройка есть только на Android")
        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            activity.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            self.flash("Найди Notes Bot и выбери «Не ограничивать»")
        except Exception as e:
            self.flash(f"Не удалось открыть настройки: {e}")

    # ---------- запуск/остановка сервиса ----------
    def _launch_start(self):
        if not load_config().get("enabled"):
            return
        try:
            if platform == "android":
                from jnius import autoclass

                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                service = autoclass(activity.getPackageName() + ".ServiceBotservice")
                try:
                    service.start(activity, "")
                except Exception:  # сигнатура старых версий python-for-android
                    service.start(activity, "", "Notes Bot", "Бот работает", "")
            else:
                here = os.path.dirname(os.path.abspath(__file__))
                self.proc = subprocess.Popen([sys.executable, "service.py"], cwd=here)
        except Exception as e:
            save_config({**load_config(), "enabled": False})
            self.flash(f"Не удалось запустить сервис: {e}")

    def _launch_stop(self):
        try:
            if platform == "android":
                from jnius import autoclass

                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                service = autoclass(activity.getPackageName() + ".ServiceBotservice")
                service.stop(activity)
            elif self.proc:
                self.proc.terminate()
                self.proc = None
        except Exception:
            pass

    def _request_notification_permission(self):
        try:
            from android.permissions import request_permissions

            request_permissions(["android.permission.POST_NOTIFICATIONS"])
        except Exception:
            pass

    # ---------- обновление интерфейса ----------
    def compute_state(self):
        cfg = load_config()
        st = read_status()
        now = time.time()
        if not cfg.get("enabled"):
            if st.get("state") == "error":
                return "error", "Ошибка", st.get("msg", "")
            return "stopped", "Бот остановлен", "Вставь токен и нажми «Запустить»"
        if st.get("state") == "running" and now - st.get("ts", 0) < STALE_AFTER:
            detail = f"@{st['bot']}" if st.get("bot") else ""
            if st.get("msg"):
                detail = f"{detail} · {st['msg']}" if detail else st["msg"]
            return "running", "Бот работает", detail
        if now - self.requested_at < 30:
            return "starting", "Запускаю…", "Подключаюсь к Telegram"
        return "noresp", "Сервис не отвечает", "Нажми «Остановить» и запусти снова"

    def tick(self, *_):
        root = self.root
        state, title, detail = self.compute_state()
        active = state in ("running", "starting", "noresp")
        root.status_title = title
        root.status_detail = detail[:90]
        root.dot_color = DOT_COLORS[state]
        root.running = active
        root.button_text = "Остановить бота" if active else "Запустить бота"
        root.toggle_bg = SOFT if active else ACCENT
        root.toggle_color = DARK_TEXT if active else (1, 1, 1, 1)

        text = tail_log()
        if text != root.log_text:
            root.log_text = text
            Clock.schedule_once(lambda dt: setattr(root.ids.log_scroll, "scroll_y", 0), 0.05)

    def flash(self, text):
        self.root.message = text
        Clock.unschedule(self._clear_flash)
        Clock.schedule_once(self._clear_flash, 5)

    def _clear_flash(self, *_):
        self.root.message = ""


if __name__ == "__main__":
    BotLauncherApp().run()
