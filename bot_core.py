"""Логика Telegram-бота для заметок.

Работает на чистой стандартной библиотеке (urllib + sqlite3) — без внешних
зависимостей, кроме certifi (корневые сертификаты для HTTPS на Android).
Получает сообщения через long polling (getUpdates).

Доступ по коду: пока не введён код дня, бот ничего не принимает.
Код:  G = 2 * (d + m + y)
  d — число месяца, m — номер месяца, y — год.
После ввода кода открывается сессия. По кнопке «Закрыть» (или автоматически,
если от пользователя не было сообщений 15 минут) все сообщения чата стираются.
Сами заметки в базе при этом остаются.
"""
import datetime
import json
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.request

API_URL = "https://api.telegram.org/bot{token}/{method}"
LIST_LIMIT = 20
PREVIEW_LEN = 150

SESSION_TTL = 15 * 60          # через сколько секунд без сообщений сессия закрывается
MAX_FAILS = 5                  # неверных попыток подряд до блокировки
BLOCK_TIME = 10 * 60           # на сколько блокируем, сек
MSG_KEEP = 47 * 3600           # Telegram не даёт удалять сообщения старше 48 ч

CLOSE_BTN = "🔒 Закрыть"
CLOSE_KB = {
    "keyboard": [[{"text": CLOSE_BTN}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
REMOVE_KB = {"remove_keyboard": True}

HELP_TEXT = (
    "📝 Я бот для заметок.\n\n"
    "Просто отправь мне любое сообщение — я сохраню его как заметку.\n\n"
    "Команды:\n"
    "/add <текст> — добавить заметку\n"
    "/list — последние заметки\n"
    "/view <id> — показать заметку целиком\n"
    "/search <слово> — поиск по заметкам\n"
    "/edit <id> <новый текст> — изменить заметку\n"
    "/delete <id> — удалить заметку\n"
    "/close — закрыть сессию и стереть переписку\n"
    "/help — эта справка"
)


def access_code(today=None) -> int:
    """Код доступа на сегодня: G = 2 * (d + m + y)."""
    today = today or datetime.date.today()  # локальная дата телефона
    return 2 * (today.day + today.month + today.year)


class ApiError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"Telegram API {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class Fatal(Exception):
    """Ошибка, после которой нет смысла повторять попытки (например, неверный токен)."""


def make_ssl_context():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def preview(text: str) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= PREVIEW_LEN else text[:PREVIEW_LEN] + "…"


class NotesBot:
    def __init__(self, token, db_path, log, should_stop=lambda: False,
                 on_status=lambda state, **extra: None):
        self.token = token
        self._log = log
        self.should_stop = should_stop
        self.on_status = on_status
        self.ctx = make_ssl_context()
        self.fails = {}  # user_id -> [неверных попыток, заблокирован до]
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS notes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   text TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        # открытые сессии (хранятся в БД, чтобы пережить перезапуск сервиса)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   chat_id INTEGER PRIMARY KEY,
                   user_id INTEGER NOT NULL,
                   opened_at REAL NOT NULL
               )"""
        )
        # id всех сообщений чата — чтобы потом их стереть
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS chat_msgs (
                   chat_id INTEGER NOT NULL,
                   message_id INTEGER NOT NULL,
                   ts REAL NOT NULL
               )"""
        )
        self.db.commit()

    # ---------- журнал без утечки токена ----------
    def log(self, msg):
        self._log(str(msg).replace(self.token, "<токен>"))

    # ---------- Telegram API ----------
    def api(self, method, _timeout=15, **params):
        params = {k: v for k, v in params.items() if v is not None}
        req = urllib.request.Request(
            API_URL.format(token=self.token, method=method),
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_timeout, context=self.ctx) as r:
                body = r.read()
        except urllib.error.HTTPError as e:  # у Telegram в теле ошибки лежит JSON
            body = e.read()
        try:
            data = json.loads(body)
        except ValueError:
            raise RuntimeError("Некорректный ответ сервера Telegram")
        if not data.get("ok"):
            raise ApiError(
                data.get("error_code"),
                data.get("description", ""),
                (data.get("parameters") or {}).get("retry_after"),
            )
        return data["result"]

    def send(self, chat_id, text, markup=None, track=True):
        res = self.api("sendMessage", chat_id=chat_id, text=text[:4096], reply_markup=markup)
        if track and isinstance(res, dict) and "message_id" in res:
            self.track(chat_id, res["message_id"])
        return res

    # ---------- сессии и стирание переписки ----------
    def track(self, chat_id, message_id):
        self.db.execute(
            "INSERT INTO chat_msgs (chat_id, message_id, ts) VALUES (?, ?, ?)",
            (chat_id, message_id, time.time()),
        )
        self.db.commit()

    def is_open(self, chat_id):
        row = self.db.execute(
            "SELECT opened_at FROM sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return bool(row) and time.time() - row[0] < SESSION_TTL

    def touch(self, chat_id):
        """Отмечаем активность пользователя: таймер 15 минут начинается заново."""
        self.db.execute(
            "UPDATE sessions SET opened_at = ? WHERE chat_id = ?", (time.time(), chat_id)
        )
        self.db.commit()

    def open_session(self, chat_id, uid):
        self.db.execute(
            "INSERT OR REPLACE INTO sessions (chat_id, user_id, opened_at) VALUES (?, ?, ?)",
            (chat_id, uid, time.time()),
        )
        self.db.commit()

    def _delete_messages(self, chat_id, ids):
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                self.api("deleteMessages", chat_id=chat_id, message_ids=chunk)
            except ApiError:
                for mid in chunk:  # запасной путь: по одному, ошибки игнорируем
                    try:
                        self.api("deleteMessage", chat_id=chat_id, message_id=mid)
                    except ApiError:
                        pass

    def close_session(self, chat_id, reason):
        """Стирает все сообщения чата и закрывает сессию. Заметки не трогает."""
        ids = [r[0] for r in self.db.execute(
            "SELECT message_id FROM chat_msgs WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        )]
        self._delete_messages(chat_id, ids)  # при сетевой ошибке — исключение, повторим позже
        self.db.execute("DELETE FROM chat_msgs WHERE chat_id = ?", (chat_id,))
        self.db.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        self.db.commit()
        try:  # итоговое сообщение не отслеживаем — оно остаётся в чате
            self.send(chat_id, f"🔒 {reason}\nЧтобы открыть бота, отправь код доступа.",
                      REMOVE_KB, track=False)
        except Exception as e:
            self.log(f"Не удалось отправить итог закрытия: {e}")

    def expire_sessions(self):
        """Закрывает просроченные сессии и чистит старые записи об id сообщений."""
        limit = time.time() - SESSION_TTL
        rows = self.db.execute(
            "SELECT chat_id FROM sessions WHERE opened_at < ?", (limit,)
        ).fetchall()
        for (chat_id,) in rows:
            self.close_session(chat_id, "15 минут без сообщений — сессия закрыта, переписка стёрта.")
        # сообщения старше ~48 ч Telegram всё равно не даст удалить
        self.db.execute("DELETE FROM chat_msgs WHERE ts < ?", (time.time() - MSG_KEEP,))
        self.db.commit()

    # ---------- проверка кода ----------
    def locked(self, chat, uid, text):
        now = time.time()
        count, until = self.fails.get(uid, [0, 0])
        if until > now:
            left = int((until - now) // 60) + 1
            return self.send(chat, f"⛔ Слишком много неверных попыток. Подожди ~{left} мин.")
        if text.strip() == str(access_code()):
            self.fails.pop(uid, None)
            self.open_session(chat, uid)
            return self.send(
                chat,
                "🔓 Доступ открыт. Если 15 минут ничего не писать или нажать «Закрыть» — переписка сотрётся.\n\n"
                + HELP_TEXT,
                CLOSE_KB,
            )
        count += 1
        if count >= MAX_FAILS:
            self.fails[uid] = [0, now + BLOCK_TIME]
            return self.send(chat, "⛔ Неверный код. Доступ заблокирован на 10 минут.")
        self.fails[uid] = [count, 0]
        self.send(chat, "🔒 Бот закрыт. Отправь код доступа.")

    # ---------- заметки (БД) ----------
    def add_note(self, uid, text):
        cur = self.db.execute("INSERT INTO notes (user_id, text) VALUES (?, ?)", (uid, text))
        self.db.commit()
        return cur.lastrowid

    def get_notes(self, uid, query=None):
        rows = self.db.execute(
            "SELECT id, text, created_at FROM notes WHERE user_id = ? ORDER BY id DESC", (uid,)
        ).fetchall()
        if query:  # поиск в Python: SQLite не умеет lower() для кириллицы
            q = query.lower()
            rows = [r for r in rows if q in r[1].lower()]
        return rows[:LIST_LIMIT]

    def get_note(self, uid, note_id):
        return self.db.execute(
            "SELECT id, text, created_at FROM notes WHERE id = ? AND user_id = ?", (note_id, uid)
        ).fetchone()

    def update_note(self, uid, note_id, text):
        cur = self.db.execute(
            "UPDATE notes SET text = ? WHERE id = ? AND user_id = ?", (text, note_id, uid)
        )
        self.db.commit()
        return cur.rowcount > 0

    def delete_note(self, uid, note_id):
        cur = self.db.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, uid))
        self.db.commit()
        return cur.rowcount > 0

    # ---------- оформление списка ----------
    @staticmethod
    def render_list(notes, title):
        lines, buttons = [title, ""], []
        for note_id, text, created in notes:
            lines.append(f"#{note_id} · {created[:16]}\n{preview(text)}\n")
            buttons.append({"text": f"🗑 #{note_id}", "callback_data": f"del:{note_id}"})
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        return "\n".join(lines), {"inline_keyboard": rows}

    # ---------- обработка обновлений ----------
    def handle(self, update):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        msg = update.get("message")
        if not msg or not msg.get("text"):
            return
        chat, uid = msg["chat"]["id"], msg["from"]["id"]
        text = msg["text"].strip()
        self.track(chat, msg["message_id"])
        self.expire_sessions()

        if not self.is_open(chat):
            return self.locked(chat, uid, text)

        self.touch(chat)
        if text == CLOSE_BTN or text.split("@")[0].lower() == "/close":
            return self.close_session(chat, "Сессия закрыта, переписка стёрта.")
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            self.command(chat, uid, cmd[1:].split("@")[0].lower(), arg.strip())
        else:
            note_id = self.add_note(uid, text)
            self.send(chat, f"✅ Заметка #{note_id} сохранена.")

    def command(self, chat, uid, cmd, arg):
        if cmd in ("start", "help"):
            self.send(chat, HELP_TEXT)
        elif cmd == "add":
            if not arg:
                return self.send(chat, "Использование: /add <текст заметки>")
            self.send(chat, f"✅ Заметка #{self.add_note(uid, arg)} сохранена.")
        elif cmd == "list":
            notes = self.get_notes(uid)
            if not notes:
                return self.send(chat, "Заметок пока нет. Отправь мне любой текст!")
            text, kb = self.render_list(notes, f"📚 Последние заметки (до {LIST_LIMIT}):")
            self.send(chat, text, kb)
        elif cmd == "search":
            if not arg:
                return self.send(chat, "Использование: /search <слово>")
            notes = self.get_notes(uid, arg)
            if not notes:
                return self.send(chat, "Ничего не найдено.")
            text, kb = self.render_list(notes, f"🔎 Результаты по «{arg}»:")
            self.send(chat, text, kb)
        elif cmd == "view":
            if not arg.isdigit():
                return self.send(chat, "Использование: /view <id>")
            note = self.get_note(uid, int(arg))
            if not note:
                return self.send(chat, "Заметка не найдена.")
            self.send(chat, f"#{note[0]} · {note[2][:16]}\n\n{note[1]}")
        elif cmd == "edit":
            parts = arg.split(None, 1)
            if len(parts) < 2 or not parts[0].isdigit():
                return self.send(chat, "Использование: /edit <id> <новый текст>")
            ok = self.update_note(uid, int(parts[0]), parts[1].strip())
            self.send(chat, f"✏️ Заметка #{parts[0]} обновлена." if ok else "Заметка не найдена.")
        elif cmd == "delete":
            if not arg.isdigit():
                return self.send(chat, "Использование: /delete <id>")
            ok = self.delete_note(uid, int(arg))
            self.send(chat, f"🗑 Заметка #{arg} удалена." if ok else "Заметка не найдена.")
        else:
            self.send(chat, "Не знаю такую команду. Список команд: /help")

    def on_callback(self, cq):
        data = cq.get("data", "")
        uid = cq["from"]["id"]
        message = cq.get("message")
        chat = message["chat"]["id"] if message else uid  # в личке id чата = id пользователя
        self.expire_sessions()
        if not self.is_open(chat):
            return self.api(
                "answerCallbackQuery", callback_query_id=cq["id"],
                text="Сессия закрыта. Отправь код доступа.", show_alert=True,
            )
        self.touch(chat)
        if not data.startswith("del:") or not data[4:].isdigit():
            return self.api("answerCallbackQuery", callback_query_id=cq["id"])
        deleted = self.delete_note(uid, int(data[4:]))
        self.api(
            "answerCallbackQuery",
            callback_query_id=cq["id"],
            text="Заметка удалена" if deleted else "Заметка уже удалена",
        )
        if not message:
            return
        notes = self.get_notes(uid)
        if notes:
            text, kb = self.render_list(notes, f"📚 Последние заметки (до {LIST_LIMIT}):")
        else:
            text, kb = "Заметок больше нет.", None
        self.api(
            "editMessageText",
            chat_id=message["chat"]["id"],
            message_id=message["message_id"],
            text=text[:4096],
            reply_markup=kb,
        )

    # ---------- основной цикл ----------
    def run(self):
        username, offset = None, None
        while not self.should_stop():
            try:
                if username is None:
                    username = self.api("getMe")["username"]
                    self.api("deleteWebhook")  # иначе getUpdates вернёт ошибку 409
                    self.log(f"Подключён как @{username}")
                self.on_status("running", bot=username)
                self.expire_sessions()  # автозакрытие по таймеру (точность ≈ 25 с)
                updates = self.api(
                    "getUpdates", _timeout=40, offset=offset, timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for upd in updates:
                    offset = upd["update_id"] + 1
                    try:
                        self.handle(upd)
                    except Exception as e:  # одно кривое сообщение не должно ронять бота
                        self.log(f"Ошибка обработки сообщения: {e}")
            except ApiError as e:
                if e.code == 401:
                    raise Fatal("Неверный токен бота (Telegram ответил 401).")
                if e.code == 409:
                    self.log("Конфликт: этот бот уже запущен где-то ещё (ПК, сервер?). Жду…")
                    self.on_status("running", bot=username, msg="конфликт: бот запущен ещё где-то")
                    time.sleep(10)
                elif e.code == 429:
                    time.sleep((e.retry_after or 5) + 1)
                else:
                    self.log(str(e))
                    time.sleep(5)
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                if isinstance(e, socket.timeout):
                    continue  # обычный таймаут long polling
                self.log(f"Нет связи с Telegram: {e}. Повтор через 5 с…")
                self.on_status("running", bot=username, msg="нет сети, пробую снова…")
                time.sleep(5)
