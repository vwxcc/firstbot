import os
import re
import io
import json
import time
import base64
import sqlite3
import logging
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

import httpx
import telebot
from telebot import types

import file_parser

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CVC_API_KEY = os.getenv("CVC_API_KEY")

API_BASE = os.getenv(
    "CVC_BASE_URL",
    "https://ai.starimg.ru/v1"
).rstrip("/")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "bot.db")

DEFAULT_USER_LIMIT = int(os.getenv("USER_LIMIT", "20000"))
DEFAULT_SUB_LIMIT = int(os.getenv("SUB_LIMIT", "200000"))

HISTORY_MAX_MESSAGES = int(
    os.getenv("HISTORY_MAX_MESSAGES", "20")
)

HISTORY_MAX_CHARS = int(
    os.getenv("HISTORY_MAX_CHARS", "30000")
)

SUMMARY_TRIGGER_MESSAGES = int(
    os.getenv("SUMMARY_TRIGGER_MESSAGES", "16")
)

MAX_OUTPUT_TOKENS = int(
    os.getenv("MAX_OUTPUT_TOKENS", "4096")
)


# Only Claude Sonnet 5 is used
SINGLE_MODEL = "cheapvibecode/claude-sonnet-5"
SINGLE_MODEL_NAME = "Claude Sonnet 5"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("fast-answer")


# ============================================================
# CHECK ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан в Environment Variables"
    )

if not CVC_API_KEY:
    raise RuntimeError(
        "CVC_API_KEY не задан в Environment Variables"
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            model TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            user_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER PRIMARY KEY,
            period_start INTEGER NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            expires_at INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES ('user_limit', ?)
    """, (str(DEFAULT_USER_LIMIT),))

    conn.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES ('sub_limit', ?)
    """, (str(DEFAULT_SUB_LIMIT),))

    conn.commit()
    conn.close()


init_db()


# ============================================================
# SETTINGS
# ============================================================

def get_setting(name, default):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (name,)
    ).fetchone()
    conn.close()

    if not row:
        return default

    try:
        return int(row["value"])
    except Exception:
        return default


def set_setting(name, value):
    conn = db()

    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
    """, (name, str(value)))

    conn.commit()
    conn.close()


# ============================================================
# USERS
# ============================================================

def ensure_user(message):
    user = message.from_user
    now = int(time.time())

    conn = db()

    conn.execute("""
        INSERT INTO users(
            user_id,
            username,
            first_name,
            model,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            updated_at=excluded.updated_at
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        SINGLE_MODEL,
        now,
        now
    ))

    conn.commit()
    conn.close()


def get_model(user_id):
    # Always return the single Sonnet 5 model
    return SINGLE_MODEL


def set_model(user_id, model):
    # Ignore model selection - always use Sonnet 5
    pass


# ============================================================
# 6-HOUR PERIODS
# ============================================================

def current_period_start():
    """
    Фиксированный общий период.
    00:00-06:00
    06:00-12:00
    12:00-18:00
    18:00-00:00 UTC
    """

    now = int(time.time())

    period = 6 * 60 * 60

    return (now // period) * period


def seconds_to_reset():
    now = int(time.time())
    period = 6 * 60 * 60
    next_period = ((now // period) + 1) * period
    return max(0, next_period - now)


# ============================================================
# SUBSCRIPTION
# ============================================================

def has_subscription(user_id):
    conn = db()

    row = conn.execute("""
        SELECT expires_at
        FROM subscriptions
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    if not row:
        return False

    return row["expires_at"] > int(time.time())


def subscription_expires(user_id):
    conn = db()

    row = conn.execute("""
        SELECT expires_at
        FROM subscriptions
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    if not row:
        return None

    if row["expires_at"] <= int(time.time()):
        return None

    return row["expires_at"]


def grant_subscription(user_id, days=1):
    expires = int(time.time()) + days * 86400

    conn = db()

    conn.execute("""
        INSERT INTO subscriptions(user_id, expires_at)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET expires_at=excluded.expires_at
    """, (user_id, expires))

    conn.commit()
    conn.close()

    return expires


# ============================================================
# TOKEN LIMITS
# ============================================================

def get_limit(user_id):
    if has_subscription(user_id):
        return get_setting(
            "sub_limit",
            DEFAULT_SUB_LIMIT
        )

    return get_setting(
        "user_limit",
        DEFAULT_USER_LIMIT
    )


def get_usage(user_id):
    period = current_period_start()

    conn = db()

    row = conn.execute("""
        SELECT period_start, tokens
        FROM usage
        WHERE user_id=?
    """, (user_id,)).fetchone()

    if not row:
        conn.execute("""
            INSERT INTO usage(user_id, period_start, tokens)
            VALUES (?, ?, 0)
        """, (user_id, period))

        conn.commit()
        conn.close()

        return 0

    if row["period_start"] != period:
        conn.execute("""
            UPDATE usage
            SET period_start=?, tokens=0
            WHERE user_id=?
        """, (period, user_id))

        conn.commit()
        conn.close()

        return 0

    tokens = row["tokens"]

    conn.close()

    return tokens


def add_usage(user_id, tokens):
    if tokens <= 0:
        return

    period = current_period_start()

    conn = db()

    conn.execute("""
        INSERT INTO usage(user_id, period_start, tokens)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            period_start=excluded.period_start,
            tokens=
                CASE
                    WHEN usage.period_start != excluded.period_start
                    THEN excluded.tokens
                    ELSE usage.tokens + excluded.tokens
                END
    """, (user_id, period, tokens))

    conn.commit()
    conn.close()


def can_use(user_id):
    used = get_usage(user_id)
    limit = get_limit(user_id)

    return used < limit


# ============================================================
# HISTORY
# ============================================================

def add_message(user_id, role, content):
    conn = db()

    conn.execute("""
        INSERT INTO messages(
            user_id,
            role,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        role,
        content,
        int(time.time())
    ))

    conn.commit()
    conn.close()


def get_history(user_id):
    conn = db()

    rows = conn.execute("""
        SELECT role, content
        FROM messages
        WHERE user_id=?
        ORDER BY id ASC
    """, (user_id,)).fetchall()

    summary = conn.execute("""
        SELECT content
        FROM summaries
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    result = []

    if summary:
        result.append({
            "role": "user",
            "content": (
                "Краткое содержание предыдущего диалога:\n"
                + summary["content"]
            )
        })

    for row in rows:
        result.append({
            "role": row["role"],
            "content": row["content"]
        })

    return result


def history_stats(user_id):
    conn = db()

    row = conn.execute("""
        SELECT COUNT(*) AS count,
               COALESCE(SUM(LENGTH(content)), 0) AS chars
        FROM messages
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    return row["count"], row["chars"]


# ============================================================
# API
# ============================================================

SYSTEM_PROMPT = """
Ты — Fast Answer, русскоязычный AI-ассистент в Telegram.

Отвечай преимущественно на русском языке.

Отвечай понятно, конкретно и без лишнего оформления.

Не используй огромное количество Markdown-звёздочек.
Не превращай обычный ответ в жирный текст.
Используй заголовки только когда они действительно нужны.

Если пользователь просит создать файл, действительно подготовь данные
в требуемом формате, а не говори, что это невозможно.

Если пользователь прислал документ, анализируй его содержимое.

Если информации недостаточно, прямо скажи, чего не хватает.

Не утверждай, что создал файл, если файл фактически не был создан.
"""


def clean_answer(text):
    if not text:
        return "Не удалось получить ответ."

    text = str(text)

    text = text.replace("###", "")
    text = text.replace("##", "")
    text = text.replace("**", "")
    text = text.replace("__", "")

    text = re.sub(
        r"\n{4,}",
        "\n\n",
        text
    )

    text = re.sub(
        r"[ \t]{2,}",
        " ",
        text
    )

    return text.strip()


def estimate_tokens(text):
    if not text:
        return 0

    # Грубая оценка, используется только до запроса.
    return max(
        1,
        len(text) // 4
    )


async def api_request(
    model,
    messages,
    max_tokens=MAX_OUTPUT_TOKENS
):
    # Always use Sonnet 5 regardless of model parameter
    url = f"{API_BASE}/chat/completions"

    headers = {
        "Authorization": f"Bearer {CVC_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "model": SINGLE_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    timeout = httpx.Timeout(
        connect=30,
        read=180,
        write=60,
        pool=30
    )

    async with httpx.AsyncClient(
        timeout=timeout
    ) as client:

        response = await client.post(
            url,
            headers=headers,
            json=payload
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code}: "
                f"{response.text[:1000]}"
            )

        data = response.json()

    if not data.get("choices"):
        raise RuntimeError(
            f"API не вернул choices: {data}"
        )

    choice = data["choices"][0]

    message = choice.get("message", {})

    content = message.get("content")

    if isinstance(content, list):
        parts = []

        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))

        content = "\n".join(parts)

    if not content:
        content = ""

    usage = data.get("usage") or {}

    total_tokens = usage.get("total_tokens")

    if not isinstance(total_tokens, int):
        total_tokens = (
            usage.get("prompt_tokens", 0)
            + usage.get("completion_tokens", 0)
        )

    return str(content), int(total_tokens or 0)


def run_api(model, messages, max_tokens=MAX_OUTPUT_TOKENS):
    import asyncio

    return asyncio.run(
        api_request(
            model,
            messages,
            max_tokens
        )
    )


# ============================================================
# HISTORY COMPRESSION
# ============================================================

def compress_history(user_id):
    history = get_history(user_id)

    if len(history) < 2:
        return

    dialog = []

    for message in history:
        role = message["role"]
        content = message["content"]

        dialog.append(
            f"{role}: {content}"
        )

    dialog_text = "\n\n".join(dialog)

    summary_prompt = """
Сожми историю диалога.

Сохрани:
- факты, которые сообщил пользователь;
- его цели;
- важные решения;
- предпочтения;
- незавершённые задачи;
- важный контекст;
- результаты предыдущих действий.

Удали:
- повторы;
- приветствия;
- неважные детали;
- лишнее оформление.

Сделай компактное содержание на русском языке.
"""

    messages = [
        {
            "role": "system",
            "content": summary_prompt
        },
        {
            "role": "user",
            "content": dialog_text
        }
    ]

    try:
        summary, tokens = run_api(
            SINGLE_MODEL,
            messages,
            max_tokens=1500
        )

        add_usage(
            user_id,
            tokens
        )

        conn = db()

        conn.execute("""
            INSERT INTO summaries(
                user_id,
                content,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                content=excluded.content,
                updated_at=excluded.updated_at
        """, (
            user_id,
            summary,
            int(time.time())
        ))

        conn.execute(
            "DELETE FROM messages WHERE user_id=?",
            (user_id,)
        )

        conn.commit()
        conn.close()

        logger.info(
            "History compressed: user=%s tokens=%s",
            user_id,
            tokens
        )

    except Exception as e:
        logger.error(
            "Ошибка сжатия истории: %s",
            e
        )


def maybe_compress(user_id):
    count, chars = history_stats(user_id)

    if (
        count >= SUMMARY_TRIGGER_MESSAGES
        or chars >= HISTORY_MAX_CHARS
    ):
        compress_history(user_id)


# ============================================================
# GENERATE ANSWER (No fallback, only Sonnet 5)
# ============================================================

def generate_answer(user_id, user_text, attachments=None):
    attachments = attachments or []

    if not can_use(user_id):
        used = get_usage(user_id)
        limit = get_limit(user_id)
        reset = seconds_to_reset()

        hours = reset // 3600
        minutes = (reset % 3600) // 60

        return (
            f"Лимит на текущие 6 часов исчерпан.\n\n"
            f"Использовано: {used:,} / {limit:,} токенов.\n"
            f"Следующий сброс через: {hours} ч. {minutes} мин."
        ), None

    history = get_history(user_id)

    user_content = user_text

    if attachments:
        user_content += "\n\nПрикреплённые материалы:\n"

        for attachment in attachments:
            user_content += (
                f"\n[{attachment.get('name', 'файл')}]\n"
                f"{attachment.get('text', '')[:50000]}"
            )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_content
    })

    try:
        logger.info(
            "Запрос: user=%s model=%s",
            user_id,
            SINGLE_MODEL
        )

        answer, tokens = run_api(
            SINGLE_MODEL,
            messages
        )

        if not tokens:
            tokens = (
                estimate_tokens(user_content)
                + estimate_tokens(answer)
            )

        add_usage(
            user_id,
            tokens
        )

        add_message(
            user_id,
            "user",
            user_content
        )

        add_message(
            user_id,
            "assistant",
            answer
        )

        maybe_compress(user_id)

        return clean_answer(answer), SINGLE_MODEL

    except Exception as e:
        error_text = str(e)

        logger.error(
            "Ошибка запроса для user=%s: %s",
            user_id,
            error_text
        )

        return (
            "Небольшая техническая проблема: сейчас не удалось "
            "получить ответ. Попробуй отправить запрос ещё раз через "
            "несколько секунд.",
            None
        )


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def safe_send(chat_id, text):
    text = clean_answer(text)

    # Telegram message limit
    limit = 4000

    if len(text) <= limit:
        return bot.send_message(
            chat_id,
            text
        )

    parts = []

    while text:
        parts.append(text[:limit])
        text = text[limit:]

    for part in parts:
        bot.send_message(
            chat_id,
            part
        )


def model_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    
    keyboard.add(
        types.InlineKeyboardButton(
            SINGLE_MODEL_NAME,
            callback_data="model:only"
        )
    )

    return keyboard


def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    keyboard.row(
        types.KeyboardButton("📊 Лимит")
    )

    keyboard.row(
        types.KeyboardButton("ℹ️ Помощь")
    )

    return keyboard


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message)

    text = (
        "🚀 Fast Answer\n\n"
        "Я AI-бот с поддержкой Claude Sonnet 5.\n\n"
        f"Модель: {SINGLE_MODEL_NAME}\n\n"
        "Можно отправлять:\n"
        "• текст\n"
        "• фото\n"
        "• PDF\n"
        "• DOCX\n"
        "• XLSX\n"
        "• PPTX\n"
        "• CSV\n"
        "• TXT\n"
        "• HEIC/HEIF\n"
        "• видео и MOV\n\n"
        "Основные команды:\n"
        "/limit — посмотреть лимит\n"
        "/new — начать новый диалог\n"
        "/help — помощь"
    )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=main_keyboard()
    )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_command(message):
    ensure_user(message)

    text = (
        "📖 Возможности\n\n"
        "/limit — состояние лимита\n"
        "/new — очистить текущую историю\n"
        "/help — список команд\n\n"
        "Файлы:\n"
        "PDF, DOCX, XLSX, PPTX, CSV, TXT, HEIC, изображения,\n"
        "видео и MOV можно отправлять прямо в чат.\n\n"
        "Также можно попросить создать Excel, Word, PowerPoint "
        "или другой файл."
    )

    bot.send_message(
        message.chat.id,
        text
    )


# ============================================================
# /LIMIT
# ============================================================

@bot.message_handler(commands=["limit"])
def limit_command(message):
    ensure_user(message)

    user_id = message.from_user.id

    used = get_usage(user_id)
    limit = get_limit(user_id)

    subscription = has_subscription(user_id)

    reset = seconds_to_reset()

    hours = reset // 3600
    minutes = (reset % 3600) // 60

    text = (
        "📊 Лимит\n\n"
        f"Использовано: {used:,} токенов\n"
        f"Доступно: {limit:,} токенов\n"
        f"Осталось: {max(0, limit - used):,}\n\n"
        f"Подписка: {'активна ⭐' if subscription else 'нет'}\n"
        f"Сброс через: {hours} ч. {minutes} мин."
    )

    bot.send_message(
        message.chat.id,
        text
    )


# ============================================================
# /NEW
# ============================================================

@bot.message_handler(commands=["new"])
def new_command(message):
    ensure_user(message)

    user_id = message.from_user.id

    conn = db()

    conn.execute(
        "DELETE FROM messages WHERE user_id=?",
        (user_id,)
    )

    conn.execute(
        "DELETE FROM summaries WHERE user_id=?",
        (user_id,)
    )

    conn.commit()
    conn.close()

    bot.send_message(
        message.chat.id,
        "🧹 История текущего диалога очищена."
    )


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Нет доступа."
        )
        return

    user_limit = get_setting(
        "user_limit",
        DEFAULT_USER_LIMIT
    )

    sub_limit = get_setting(
        "sub_limit",
        DEFAULT_SUB_LIMIT
    )

    keyboard = types.InlineKeyboardMarkup()

    keyboard.row(
        types.InlineKeyboardButton(
            "Изменить обычный лимит",
            callback_data="admin:user_limit"
        )
    )

    keyboard.row(
        types.InlineKeyboardButton(
            "Изменить лимит подписки",
            callback_data="admin:sub_limit"
        )
    )

    bot.send_message(
        message.chat.id,
        "🔧 Админка\n\n"
        f"Обычный лимит: {user_limit:,}\n"
        f"Лимит подписки: {sub_limit:,}\n\n"
        "Для выдачи подписки:\n"
        "/grant ID\n"
        "или\n"
        "/grant ID ДНИ",
        reply_markup=keyboard
    )


admin_waiting = {}


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("admin:")
)
def admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "Нет доступа"
        )
        return

    setting = call.data.split(
        "admin:",
        1
    )[1]

    if setting == "user_limit":
        admin_waiting[call.from_user.id] = "user_limit"

        bot.send_message(
            call.message.chat.id,
            "Введи новый лимит обычного пользователя:"
        )

    elif setting == "sub_limit":
        admin_waiting[call.from_user.id] = "sub_limit"

        bot.send_message(
            call.message.chat.id,
            "Введи новый лимит подписчика:"
        )

    bot.answer_callback_query(call.id)


# ============================================================
# ADMIN LIMIT MESSAGE
# ============================================================

@bot.message_handler(
    func=lambda message:
        message.from_user.id in admin_waiting
)
def admin_limit_input(message):
    if not is_admin(message.from_user.id):
        return

    setting = admin_waiting.pop(
        message.from_user.id
    )

    try:
        value = int(
            message.text.replace(
                " ",
                ""
            )
        )

        if value <= 0:
            raise ValueError

    except Exception:
        bot.send_message(
            message.chat.id,
            "Нужно указать положительное целое число."
        )
        return

    set_setting(
        setting,
        value
    )

    bot.send_message(
        message.chat.id,
        f"✅ Лимит изменён: {value:,} токенов."
    )


# ============================================================
# GRANT SUBSCRIPTION
# ============================================================

@bot.message_handler(commands=["grant"])
def grant_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Нет доступа."
        )
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Использование:\n"
            "/grant TELEGRAM_ID\n"
            "/grant TELEGRAM_ID ДНИ"
        )
        return

    try:
        user_id = int(parts[1])

        days = 1

        if len(parts) >= 3:
            days = int(parts[2])

        if days <= 0:
            raise ValueError

    except Exception:
        bot.send_message(
            message.chat.id,
            "Неверный ID или количество дней."
        )
        return

    expires = grant_subscription(
        user_id,
        days
    )

    date = datetime.fromtimestamp(
        expires,
        tz=timezone.utc
    ).strftime(
        "%d.%m.%Y %H:%M UTC"
    )

    bot.send_message(
        message.chat.id,
        f"✅ Подписка выдана.\n"
        f"ID: {user_id}\n"
        f"Срок: {days} дн.\n"
        f"До: {date}"
    )

    try:
        bot.send_message(
            user_id,
            "⭐ Тебе выдана подписка.\n\n"
            f"Лимит теперь: "
            f"{get_setting('sub_limit', DEFAULT_SUB_LIMIT):,} "
            "токенов / 6 часов.\n"
            f"Подписка действует до: {date}"
        )
    except Exception as e:
        logger.warning(
            "Не удалось уведомить пользователя %s: %s",
            user_id,
            e
        )


# ============================================================
# HELPERS FOR FILES
# ============================================================

def download_telegram_file(file_id, suffix=""):
    file_info = bot.get_file(file_id)

    data = bot.download_file(
        file_info.file_path
    )

    fd, path = tempfile.mkstemp(
        suffix=suffix
    )

    os.close(fd)

    with open(path, "wb") as f:
        f.write(data)

    return path


def process_file(
    file_path,
    extension,
    filename
):
    extension = extension.lower().lstrip(".")

    try:
        result = file_parser.parse_file(
            file_path,
            extension
        )

        if isinstance(result, dict):
            return result

        return {
            "type": "text",
            "text": result or "",
            "name": filename
        }

    except Exception as e:
        logger.error(
            "Ошибка обработки %s: %s",
            filename,
            e
        )

        return {
            "type": "error",
            "text": (
                f"Не удалось прочитать файл {filename}.\n"
                f"Причина: {e}"
            ),
            "name": filename
        }


# ============================================================
# DOCUMENTS
# ============================================================

@bot.message_handler(
    content_types=["document"]
)
def handle_document(message):
    ensure_user(message)

    document = message.document

    filename = document.file_name or "file"

    extension = os.path.splitext(
        filename
    )[1]

    try:
        path = download_telegram_file(
            document.file_id,
            extension
        )

        parsed = process_file(
            path,
            extension,
            filename
        )

        try:
            os.remove(path)
        except Exception:
            pass

        if parsed["type"] == "error":
            bot.reply_to(
                message,
                parsed["text"]
            )
            return

        user_text = (
            message.caption
            or
            f"Проанализируй файл {filename}."
        )

        answer, model = generate_answer(
            message.from_user.id,
            user_text,
            [parsed]
        )

        bot.send_chat_action(
            message.chat.id,
            "typing"
        )

        safe_send(
            message.chat.id,
            answer
        )

    except Exception as e:
        logger.error(
            "Document error: %s\n%s",
            e,
            traceback.format_exc()
        )

        bot.reply_to(
            message,
            "Не удалось обработать файл."
        )


# ============================================================
# PHOTO
# ============================================================

@bot.message_handler(
    content_types=["photo"]
)
def handle_photo(message):
    ensure_user(message)

    try:
        photo = message.photo[-1]

        path = download_telegram_file(
            photo.file_id,
            ".jpg"
        )

        with open(path, "rb") as f:
            encoded = base64.b64encode(
                f.read()
            ).decode()

        try:
            os.remove(path)
        except Exception:
            pass

        user_text = (
            message.caption
            or
            "Проанализируй это изображение."
        )

        # Для API, поддерживающего OpenAI-style vision.
        history = get_history(
            message.from_user.id
        )

        content = [
            {
                "type": "text",
                "text": user_text
            },
            {
                "type": "image_url",
                "image_url": {
                    "url":
                        "data:image/jpeg;base64,"
                        + encoded
                }
            }
        ]

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }
        ]

        messages.extend(history)

        messages.append({
            "role": "user",
            "content": content
        })

        try:
            answer, tokens = run_api(
                SINGLE_MODEL,
                messages
            )

            if not tokens:
                tokens = estimate_tokens(
                    user_text
                ) + estimate_tokens(answer)

            add_usage(
                message.from_user.id,
                tokens
            )

            add_message(
                message.from_user.id,
                "user",
                user_text + "\n[Изображение]"
            )

            add_message(
                message.from_user.id,
                "assistant",
                answer
            )

            maybe_compress(
                message.from_user.id
            )

            safe_send(
                message.chat.id,
                answer
            )

            return

        except Exception as e:
            logger.error(
                "Ошибка vision модели: %s",
                e
            )

        bot.reply_to(
            message,
            "Не удалось обработать изображение. "
            "Попробуй ещё раз."
        )

    except Exception as e:
        logger.error(
            "Photo error: %s\n%s",
            e,
            traceback.format_exc()
        )

        bot.reply_to(
            message,
            "Не удалось обработать изображение."
        )


# ============================================================
# VIDEO / MOV
# ============================================================

@bot.message_handler(
    content_types=["video"]
)
def handle_video(message):
    ensure_user(message)

    bot.reply_to(
        message,
        "🎬 Видео получено.\n\n"
        "Сейчас я могу принять видеофайл, но для полноценного "
        "анализа видео API должно поддерживать video input. "
        "Если оно не поддерживается, отправь отдельные кадры "
        "или аудиодорожку."
    )


# ============================================================
# AUDIO
# ============================================================

@bot.message_handler(
    content_types=["voice", "audio"]
)
def handle_audio(message):
    ensure_user(message)

    bot.reply_to(
        message,
        "🎧 Аудио получено.\n\n"
        "Для расшифровки аудио нужен отдельный speech-to-text "
        "endpoint. Текущий Claude endpoint используется для текста "
        "и мультимодального анализа."
    )


# ============================================================
# TEXT
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text(message):
    ensure_user(message)

    text = message.text.strip()

    if not text:
        return

    if text.startswith("/"):
        return

    if text == "📊 Лимит":
        limit_command(message)
        return

    if text == "ℹ️ Помощь":
        help_command(message)
        return

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    answer, model = generate_answer(
        message.from_user.id,
        text
    )

    safe_send(
        message.chat.id,
        answer
    )


# ============================================================
# OTHER
# ============================================================

@bot.message_handler(
    content_types=[
        "animation",
        "sticker",
        "contact",
        "location",
        "venue",
        "video_note"
    ]
)
def handle_other(message):
    ensure_user(message)

    bot.reply_to(
        message,
        "Этот тип сообщения пока не поддерживается напрямую."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

class BotExceptionHandler(
    telebot.ExceptionHandler
):
    def handle(self, exception):
        logger.error(
            "Telegram error: %s\n%s",
            exception,
            traceback.format_exc()
        )

        return True


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "🚀 Fast Answer запущен"
    )

    logger.info(
        "API: %s",
        API_BASE
    )

    logger.info(
        "Модель: %s",
        SINGLE_MODEL
    )

    logger.info(
        "User limit: %s",
        get_setting(
            "user_limit",
            DEFAULT_USER_LIMIT
        )
    )

    logger.info(
        "Subscription limit: %s",
        get_setting(
            "sub_limit",
            DEFAULT_SUB_LIMIT
        )
    )

    bot.remove_webhook()

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )


if __name__ == "__main__":
    main()
