import os
import json
import time
import re
import traceback
from pathlib import Path

import requests
import telebot


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CVC_API_KEY = os.getenv("CVC_API_KEY")

# Gateway из инструкции OpenCode
BASE_URL = "https://ai.starimg.ru/v1"

# Переменная с IP/адресом, если она нужна
# для твоей инфраструктуры.
CLAUDE2MLN1 = os.getenv("CLAUDE2MLN1")

# Основная модель.
MODEL = os.getenv(
    "MODEL",
    "cheapvibecode/claude-sonnet-4-6"
)

# Модель для сжатия истории.
SUMMARY_MODEL = os.getenv(
    "SUMMARY_MODEL",
    "cheapvibecode/claude-haiku-4-5"
)

# После какого количества сообщений
# запускать сжатие.
COMPRESS_EVERY = 12

# Сколько последних сообщений оставить
# после сжатия.
KEEP_LAST_MESSAGES = 8

# Сколько символов максимум отправлять
# Telegram одним сообщением.
TELEGRAM_LIMIT = 4000

REQUEST_TIMEOUT = 180


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN"
    )

if not CVC_API_KEY:
    raise RuntimeError(
        "Не задан CVC_API_KEY"
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# ХРАНЕНИЕ ИСТОРИИ
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

    history["messages"].append(
        {
            "role": role,
            "content": content
        }
    )

    if role == "user":

        history[
            "user_message_count"
        ] += 1

    save_histories()


# ============================================================
# ОЧИСТКА ОТ ЛИШНИХ ЗВЁЗДОЧЕК
# ============================================================

def clean_answer(text):

    if not text:
        return ""

    text = text.strip()

    # Убираем Markdown-заголовки
    text = re.sub(
        r"(?m)^\s*#{1,6}\s+",
        "",
        text
    )

    # Убираем декоративные строки
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

    # Не оставляем одиночные декоративные *
    text = re.sub(
        r"(?m)^\s*\*\s*$",
        "",
        text
    )

    # Тройные и более переносы
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# ЗАПРОС К GATEWAY
# ============================================================

def claude_request(
    messages,
    model,
    temperature=0.5
):

    url = (
        BASE_URL.rstrip("/")
        + "/chat/completions"
    )

    headers = {
        "Authorization":
            f"Bearer {CVC_API_KEY}",

        "Content-Type":
            "application/json",

        "Accept":
            "application/json"
    }

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature
    }

    print(
        f"[AI] Запрос модели: {model}",
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
            f"[AI] HTTP ERROR "
            f"{model}: {repr(e)}",
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
            f"[AI] MODEL ERROR: {model}",
            flush=True
        )

        print(
            response.text[:3000],
            flush=True
        )

        raise RuntimeError(
            f"HTTP {response.status_code}"
        )

    try:

        data = response.json()

    except Exception:

        print(
            "[AI] Gateway вернул не JSON:",
            response.text[:3000],
            flush=True
        )

        raise RuntimeError(
            "Некорректный ответ gateway"
        )

    try:

        content = (
            data["choices"][0]
            ["message"]
            ["content"]
        )

    except Exception:

        print(
            "[AI] Неизвестный формат ответа:",
            json.dumps(
                data,
                ensure_ascii=False
            )[:5000],
            flush=True
        )

        raise RuntimeError(
            "Не найден content в ответе"
        )

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
# СИСТЕМНЫЙ ПРОМПТ
# ============================================================

SYSTEM_PROMPT = """
Ты AI-ассистент Telegram-бота.

Отвечай преимущественно на русском языке.

Отвечай непосредственно на вопрос пользователя.

Не используй лишнее форматирование Markdown.

Не ставь вокруг обычного текста много звёздочек.

Не добавляй ненужные вступления.

Если пользователь явно попросил другой язык,
отвечай на этом языке.

Учитывай предыдущий контекст разговора.
""".strip()


# ============================================================
# ФОРМИРОВАНИЕ КОНТЕКСТА
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

        messages.append(
            {
                "role": "system",
                "content":
                    "Память предыдущего "
                    "диалога:\n\n"
                    + summary
            }
        )

    messages.extend(
        history.get(
            "messages",
            []
        )
    )

    return messages


# ============================================================
# СЖАТИЕ ИСТОРИИ
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

        role = message["role"]

        if role == "user":

            name = "Пользователь"

        else:

            name = "Ассистент"

        old_parts.append(
            f"{name}: "
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
Создай компактную память предыдущего диалога.

Сохрани только информацию, которая действительно
может понадобиться в следующих сообщениях.

Обязательно сохрани:

- цели пользователя;
- важные факты;
- важные числа;
- названия;
- решения;
- предпочтения, если они важны;
- текущие задачи;
- незавершённые задачи;
- важный технический контекст.

Не пересказывай каждую реплику.

Не добавляй информацию,
которой не было в разговоре.

Предыдущее резюме:

{previous_summary}

Старая часть диалога:

{old_text}

Верни только новое компактное резюме
на русском языке.
""".strip()

    print(
        f"[HISTORY] Начинаю сжатие "
        f"чата {chat_id}",
        flush=True
    )

    try:

        summary = claude_request(
            [
                {
                    "role": "system",
                    "content":
                        "Ты модуль памяти AI-ассистента. "
                        "Создавай только краткие "
                        "и точные резюме."
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
                "Пустое резюме"
            )

        # Только после успешного
        # получения summary меняем историю.

        history["summary"] = summary

        history["messages"] = (
            recent_messages
        )

        save_histories()

        print(
            f"[HISTORY] Чат {chat_id} "
            f"успешно сжат",
            flush=True
        )

    except Exception as e:

        print(
            f"[HISTORY] Ошибка сжатия "
            f"чата {chat_id}:",
            repr(e),
            flush=True
        )

        traceback.print_exc()

        # Старые сообщения НЕ удаляем,
        # если сжатие не удалось.


# ============================================================
# AI ОТВЕТ
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

    # Сжимаем каждые N сообщений.

    if (
        count > 0
        and
        count % COMPRESS_EVERY == 0
    ):

        compress_chat(
            chat_id
        )

    messages = build_messages(
        chat_id
    )

    answer = claude_request(
        messages,
        model=MODEL,
        temperature=0.5
    )

    answer = clean_answer(
        answer
    )

    add_message(
        chat_id,
        "assistant",
        answer
    )

    return answer


# ============================================================
# ДЛИННЫЕ ОТВЕТЫ TELEGRAM
# ============================================================

def send_long_message(
    chat_id,
    text,
    reply_to=None
):

    text = text.strip()

    if not text:

        text = "Не удалось получить ответ."

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
# START
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
# CLEAR
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
        f"{message.text[:200]}",
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
            "[TEXT] ERROR:",
            repr(e),
            flush=True
        )

        traceback.print_exc()

        bot.send_message(
            chat_id,
            "Модель временно не ответила. "
            "Попробуй ещё раз немного позже."
        )


# ============================================================
# TELEGRAM START
# ============================================================

def main():

    print(
        "==========================================",
        flush=True
    )

    print(
        "🚀 Fast Answer / Claude Gateway",
        flush=True
    )

    print(
        f"🌐 Base URL: {BASE_URL}",
        flush=True
    )

    print(
        f"🤖 Model: {MODEL}",
        flush=True
    )

    print(
        f"🧠 Summary model: {SUMMARY_MODEL}",
        flush=True
    )

    print(
        f"📦 Сжатие каждые "
        f"{COMPRESS_EVERY} сообщений",
        flush=True
    )

    if CLAUDE2MLN1:

        print(
            "🌐 CLAUDE2MLN1: задан",
            flush=True
        )

    else:

        print(
            "ℹ️ CLAUDE2MLN1: не задан",
            flush=True
        )

    print(
        "==========================================",
        flush=True
    )

    load_histories()

    # Проверяем Telegram.

    try:

        me = bot.get_me()

        print(
            f"✅ Telegram: @{me.username}",
            flush=True
        )

    except Exception as e:

        print(
            "❌ Telegram ERROR:",
            repr(e),
            flush=True
        )

        raise

    # Удаляем webhook.

    try:

        bot.remove_webhook(
            drop_pending_updates=True
        )

        print(
            "✅ Webhook удалён",
            flush=True
        )

    except Exception as e:

        print(
            "⚠️ Webhook:",
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
