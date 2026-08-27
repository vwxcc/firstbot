import os
import json
import re
import traceback
from pathlib import Path

import requests
import telebot


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CVC_API_KEY = os.getenv("CVC_API_KEY")

# OpenAI-compatible API сайта
BASE_URL = "https://ai.starimg.ru/v1"

# Основная модель
MODEL = os.getenv(
    "MODEL",
    "cheapvibecode/claude-sonnet-4-6"
)

# Модель для сжатия истории
SUMMARY_MODEL = os.getenv(
    "SUMMARY_MODEL",
    "cheapvibecode/claude-haiku-4-5"
)

# После такого количества сообщений пользователя
# запускается сжатие истории.
COMPRESS_EVERY = 12

# После сжатия сохраняем последние сообщения.
KEEP_LAST_MESSAGES = 8

# Лимит Telegram
TELEGRAM_LIMIT = 4000

# Таймаут запроса к AI
REQUEST_TIMEOUT = 180


# ============================================================
# CHECK ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задана переменная BOT_TOKEN"
    )

if not CVC_API_KEY:
    raise RuntimeError(
        "Не задана переменная CVC_API_KEY"
    )

if not CVC_API_KEY.startswith("sk-"):
    print(
        "[WARNING] CVC_API_KEY обычно должен "
        "начинаться с sk-",
        flush=True
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# HISTORY
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

HISTORY_FILE = DATA_DIR / "history.json"

histories = {}


def load_histories():
    global histories

    if not HISTORY_FILE.exists():
        histories = {}
        return

    try:
        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            histories = json.load(f)

        print(
            f"[HISTORY] Загружено чатов: "
            f"{len(histories)}",
            flush=True
        )

    except Exception as e:
        print(
            "[HISTORY] Ошибка загрузки:",
            repr(e),
            flush=True
        )

        histories = {}


def save_histories():
    temp_file = HISTORY_FILE.with_suffix(
        ".tmp"
    )

    try:
        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                histories,
                f,
                ensure_ascii=False,
                indent=2
            )

        temp_file.replace(
            HISTORY_FILE
        )

    except Exception as e:
        print(
            "[HISTORY] Ошибка сохранения:",
            repr(e),
            flush=True
        )


def get_history(chat_id):
    chat_id = str(chat_id)

    if chat_id not in histories:
        histories[chat_id] = {
            "summary": "",
            "messages": [],
            "user_message_count": 0
        }

    return histories[chat_id]


def add_message(
    chat_id,
    role,
    content
):
    history = get_history(chat_id)

    history["messages"].append({
        "role": role,
        "content": content
    })

    if role == "user":
        history["user_message_count"] += 1

    save_histories()


# ============================================================
# CLEAN ANSWER
# ============================================================

def clean_answer(text):
    if not text:
        return ""

    text = str(text).strip()

    # Убираем Markdown-заголовки:
    # ### Заголовок -> Заголовок
    text = re.sub(
        r"(?m)^\s*#{1,6}\s+",
        "",
        text
    )

    # Убираем строки из одних звездочек
    text = re.sub(
        r"(?m)^\s*\*{2,}\s*$",
        "",
        text
    )

    # ***текст*** -> текст
    text = re.sub(
        r"\*\*\*(.*?)\*\*\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    # **текст** -> текст
    text = re.sub(
        r"\*\*(.*?)\*\*",
        r"\1",
        text,
        flags=re.DOTALL
    )

    # __текст__ -> текст
    text = re.sub(
        r"__(.*?)__",
        r"\1",
        text,
        flags=re.DOTALL
    )

    # Убираем отдельные декоративные *
    text = re.sub(
        r"(?m)^\s*\*\s*$",
        "",
        text
    )

    # Не допускаем огромное количество пустых строк
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# AI REQUEST
# ============================================================

def ai_request(
    messages,
    model,
    temperature=0.5
):
    url = (
        BASE_URL.rstrip("/")
        + "/chat/completions"
    )

    headers = {
        "Authorization": f"Bearer {CVC_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature
    }

    print(
        f"[AI] Модель: {model}",
        flush=True
    )

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT
        )

    except requests.RequestException as e:
        print(
            f"[AI ERROR] Ошибка соединения "
            f"с моделью {model}:",
            repr(e),
            flush=True
        )

        raise

    print(
        f"[AI] HTTP {response.status_code} "
        f"← {model}",
        flush=True
    )

    if response.status_code >= 400:

        print(
            f"[MODEL ERROR] {model}",
            flush=True
        )

        print(
            response.text[:5000],
            flush=True
        )

        raise RuntimeError(
            f"Модель {model} вернула "
            f"HTTP {response.status_code}"
        )

    try:
        data = response.json()

    except Exception:
        print(
            "[AI ERROR] API вернул не JSON:",
            response.text[:5000],
            flush=True
        )

        raise RuntimeError(
            "API вернул некорректный ответ"
        )

    try:
        content = (
            data["choices"][0]
            ["message"]
            ["content"]
        )

    except Exception:
        print(
            "[AI ERROR] Не удалось найти "
            "choices[0].message.content",
            flush=True
        )

        print(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            )[:5000],
            flush=True
        )

        raise RuntimeError(
            "Неизвестный формат ответа API"
        )

    # Некоторые API могут вернуть список
    # частей вместо обычной строки.
    if isinstance(content, list):

        parts = []

        for item in content:

            if isinstance(item, dict):

                if item.get("type") == "text":

                    parts.append(
                        item.get(
                            "text",
                            ""
                        )
                    )

        content = "\n".join(parts)

    return str(content).strip()


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты AI-ассистент Telegram-бота.

Отвечай на русском языке, если пользователь
не попросил другой язык.

Отвечай непосредственно на вопрос.

Не используй лишнее форматирование.

Не окружай обычные предложения большим
количеством звёздочек.

Не добавляй ненужные вступления.

Используй контекст предыдущего разговора.

Если информации недостаточно, честно скажи об этом.
Не выдумывай факты.
""".strip()


# ============================================================
# BUILD CONTEXT
# ============================================================

def build_messages(chat_id):

    history = get_history(chat_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    summary = history.get(
        "summary",
        ""
    )

    if summary:

        messages.append({
            "role": "system",
            "content":
                "Краткая память предыдущей "
                "части разговора:\n\n"
                + summary
        })

    messages.extend(
        history.get(
            "messages",
            []
        )
    )

    return messages


# ============================================================
# COMPRESS HISTORY
# ============================================================

def compress_chat(chat_id):

    history = get_history(chat_id)

    messages = history.get(
        "messages",
        []
    )

    if len(messages) <= KEEP_LAST_MESSAGES:
        return

    old_messages = messages[
        :-KEEP_LAST_MESSAGES
    ]

    recent_messages = messages[
        -KEEP_LAST_MESSAGES:
    ]

    old_parts = []

    for message in old_messages:

        if message["role"] == "user":
            role = "Пользователь"
        else:
            role = "Ассистент"

        old_parts.append(
            f"{role}: "
            f"{message['content']}"
        )

    old_text = "\n\n".join(
        old_parts
    )

    previous_summary = history.get(
        "summary",
        ""
    )

    prompt = f"""
Сделай компактное и точное резюме
предыдущей части разговора.

Предыдущее резюме:
{previous_summary}

Предыдущая часть диалога:
{old_text}

Сохрани только действительно важную
информацию, которая может понадобиться
для продолжения разговора:

- цели пользователя;
- важные факты;
- числа;
- названия;
- принятые решения;
- текущие задачи;
- незавершённые задачи;
- технический контекст;
- важные предпочтения.

Не выдумывай информацию.

Не пересказывай каждое сообщение.

Верни только компактную память
на русском языке.
""".strip()

    print(
        f"[HISTORY] Сжимаю историю "
        f"чата {chat_id}",
        flush=True
    )

    try:

        summary = ai_request(
            [
                {
                    "role": "system",
                    "content":
                        "Ты модуль памяти "
                        "AI-ассистента. "
                        "Создавай только точные "
                        "и компактные резюме."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            model=SUMMARY_MODEL,
            temperature=0.2
        )

        summary = clean_answer(
            summary
        )

        if not summary:
            raise RuntimeError(
                "Модель вернула пустое резюме"
            )

        # Удаляем старую часть только
        # после успешного сжатия.

        history["summary"] = summary

        history["messages"] = (
            recent_messages
        )

        save_histories()

        print(
            f"[HISTORY] История чата "
            f"{chat_id} сжата успешно",
            flush=True
        )

    except Exception as e:

        print(
            f"[HISTORY ERROR] Чат "
            f"{chat_id}:",
            repr(e),
            flush=True
        )

        traceback.print_exc()

        # При ошибке ничего не удаляем.


# ============================================================
# GENERATE RESPONSE
# ============================================================

def generate_response(
    chat_id,
    user_text
):

    add_message(
        chat_id,
        "user",
        user_text
    )

    history = get_history(
        chat_id
    )

    count = history.get(
        "user_message_count",
        0
    )

    # Каждые 12 сообщений
    if (
        count > 0
        and count % COMPRESS_EVERY == 0
    ):
        compress_chat(
            chat_id
        )

    messages = build_messages(
        chat_id
    )

    answer = ai_request(
        messages,
        model=MODEL,
        temperature=0.5
    )

    answer = clean_answer(
        answer
    )

    if not answer:
        raise RuntimeError(
            "Модель вернула пустой ответ"
        )

    add_message(
        chat_id,
        "assistant",
        answer
    )

    return answer


# ============================================================
# SEND LONG MESSAGE
# ============================================================

def send_long_message(
    chat_id,
    text,
    reply_to=None
):

    text = text.strip()

    if not text:
        text = "Модель не вернула ответ."

    first = True

    while text:

        part = text[
            :TELEGRAM_LIMIT
        ]

        if len(text) > TELEGRAM_LIMIT:

            split_pos = part.rfind(
                "\n"
            )

            if split_pos > 1000:
                part = part[
                    :split_pos
                ]

        kwargs = {}

        if first and reply_to:

            kwargs[
                "reply_to_message_id"
            ] = reply_to

        bot.send_message(
            chat_id,
            part,
            **kwargs
        )

        first = False

        text = text[
            len(part):
        ]


# ============================================================
# /START
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def start_handler(message):

    bot.send_message(
        message.chat.id,
        "Привет! Я готов отвечать на вопросы."
    )


# ============================================================
# /CLEAR
# ============================================================

@bot.message_handler(
    commands=["clear"]
)
def clear_handler(message):

    chat_id = str(
        message.chat.id
    )

    histories.pop(
        chat_id,
        None
    )

    save_histories()

    bot.send_message(
        message.chat.id,
        "История этого чата очищена."
    )


# ============================================================
# TEXT
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def text_handler(message):

    if message.text.startswith("/"):
        return

    chat_id = message.chat.id

    print(
        f"[TEXT] chat={chat_id}: "
        f"{message.text[:300]}",
        flush=True
    )

    try:

        bot.send_chat_action(
            chat_id,
            "typing"
        )

        answer = generate_response(
            chat_id,
            message.text
        )

        send_long_message(
            chat_id,
            answer,
            message.message_id
        )

    except Exception as e:

        print(
            "[TEXT ERROR]",
            repr(e),
            flush=True
        )

        traceback.print_exc()

        bot.send_message(
            chat_id,
            "Не получилось получить ответ "
            "от модели. Попробуй ещё раз немного позже."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "==========================================",
        flush=True
    )

    print(
        "🚀 Fast Answer",
        flush=True
    )

    print(
        "🌐 API: https://ai.starimg.ru/v1",
        flush=True
    )

    print(
        f"🤖 Main model: {MODEL}",
        flush=True
    )

    print(
        f"🧠 Summary model: {SUMMARY_MODEL}",
        flush=True
    )

    print(
        f"📦 Compression: every "
        f"{COMPRESS_EVERY} user messages",
        flush=True
    )

    print(
        "🔑 CVC_API_KEY: задан",
        flush=True
    )

    # ВАЖНО:
    # CLAUDE2MLN1 здесь вообще не используется.

    print(
        "==========================================",
        flush=True
    )

    load_histories()

    # Проверяем Telegram

    try:

        me = bot.get_me()

        print(
            f"✅ Telegram подключён: "
            f"@{me.username}",
            flush=True
        )

    except Exception as e:

        print(
            "❌ Telegram ERROR:",
            repr(e),
            flush=True
        )

        raise

    # Убираем webhook,
    # чтобы Long Polling работал корректно.

    try:

        bot.remove_webhook(
            drop_pending_updates=True
        )

        print(
            "✅ Webhook отключён",
            flush=True
        )

    except Exception as e:

        print(
            "⚠️ Не удалось отключить webhook:",
            repr(e),
            flush=True
        )

    print(
        "📡 Long Polling запущен",
        flush=True
    )

    bot.infinity_polling(
        skip_pending=False,
        timeout=30,
        long_polling_timeout=30
    )


if __name__ == "__main__":
    main()
