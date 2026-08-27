import os
import json
import time
import threading
import traceback
import requests
from pathlib import Path
import tempfile
import re

import telebot
import file_parser

# ------------------------------------------------------------
# NOTE:
# Эта версия бота упрощена под единственный "cloud" API.
# - Все внешние провайдеры и fallback закомментированы/удалены.
# - Требуется: переменные окружения CLAUDE2MLN1 (ключ) и CLOUD_API_URL (endpoint).
# - Запросы к моделям отправляются только на CLOUD_API_URL с этим ключом.
# - Остальные обработчики (photo/document/audio/video) отключены — бот отвечает только на текст.
# ------------------------------------------------------------

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
CLAUDE2MLN1 = os.getenv("CLAUDE2MLN1")  # ключ для cloud API (по требованию пользователя)
CLOUD_API_URL = os.getenv("CLOUD_API_URL")  # полный URL endpoint, например https://api.vyce.ai/v1/chat/completions
CLOUD_MODEL = os.getenv("CLOUD_MODEL", "claude-2.1")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
if not CLAUDE2MLN1:
    raise RuntimeError("CLAUDE2MLN1 не задан в переменных окружения")
if not CLOUD_API_URL:
    raise RuntimeError("CLOUD_API_URL не задан в переменных окружения")

# Инициализация Telegram
bot = telebot.TeleBot(BOT_TOKEN)

# Параметры истории и сжатия (редко)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

history_lock = threading.RLock()
conversation_histories = {}

COMPRESSION_EVERY_USER_MESSAGES = int(os.getenv("COMPRESSION_EVERY_USER_MESSAGES", "50"))
KEEP_RECENT_MESSAGES = int(os.getenv("KEEP_RECENT_MESSAGES", "8"))
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "60000"))
MAX_USER_TEXT_CHARS = int(os.getenv("MAX_USER_TEXT_CHARS", "20000"))
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "2000"))

# ----------------- Helpers: load/save history -----------------

def load_histories():
    global conversation_histories
    if not HISTORY_FILE.exists():
        conversation_histories = {}
        return
    try:
        with history_lock:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                conversation_histories = data
            else:
                conversation_histories = {}
        print(f"История загружена: {len(conversation_histories)} чатов")
    except Exception as e:
        print("ОШИБКА ЗАГРУЗКИ ИСТОРИИ:", repr(e))
        traceback.print_exc()
        conversation_histories = {}


def save_histories():
    temp_file = HISTORY_FILE.with_suffix(".tmp")
    try:
        with history_lock:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(conversation_histories, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, HISTORY_FILE)
    except Exception as e:
        print("ОШИБКА СОХРАНЕНИЯ ИСТОРИИ:", repr(e))
        traceback.print_exc()

# ----------------- History helpers -----------------

def get_history(chat_id):
    chat_id = str(chat_id)
    with history_lock:
        if chat_id not in conversation_histories:
            conversation_histories[chat_id] = {
                "summary": "",
                "messages": [],
                "user_messages_since_compression": 0,
            }
        return conversation_histories[chat_id]


def add_message(chat_id, role, content):
    chat_id = str(chat_id)
    with history_lock:
        history = get_history(chat_id)
        history["messages"].append({"role": role, "content": content})
        if role == "user":
            history["user_messages_since_compression"] += 1
        save_histories()


def total_history_chars(history):
    total = len(history.get("summary", ""))
    for message in history.get("messages", []):
        total += len(str(message.get("content", "")))
    return total

# ----------------- Clean formatting -----------------

def clean_answer(text: str) -> str:
    if not text:
        return text
    text = text.replace("***", "")
    text = text.replace("**", "")
    text = re.sub(r"(?<!\\w)__([^_\\n]+)__", r"\1", text)
    text = re.sub(r"(?<!\\w)\*([^*\\n]+)\*", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*\*+\s$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ----------------- Cloud API call -----------------

def call_cloud_chat(messages, model=None, max_tokens=None, timeout=30):
    """Отправляет chat-like запрос на CLOUD_API_URL и возвращает текст-ответ.
    Поддерживает несколько форматов ответа (choices[].message.content, generated_text, output).
    """
    payload = {
        "model": model or CLOUD_MODEL,
        "messages": messages,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    headers = {
        "Authorization": f"Bearer {CLAUDE2MLN1}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(CLOUD_API_URL, headers=headers, json=payload, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"Ошибка запроса к cloud API: {e}")

    if resp.status_code >= 400:
        raise RuntimeError(f"cloud API error {resp.status_code}: {resp.text}")

    try:
        data = resp.json()
    except Exception:
        return resp.text

    # Попытки извлечения текста в разных форматах
    if isinstance(data, dict):
        if "choices" in data and isinstance(data["choices"], list) and len(data["choices"])>0:
            ch = data["choices"][0]
            # openai-like
            msg = ch.get("message") if isinstance(ch, dict) else None
            if isinstance(msg, dict) and msg.get("content"):
                return msg.get("content")
            if ch.get("text"):
                return ch.get("text")
        for key in ("generated_text", "text", "output", "result"):
            if key in data and isinstance(data[key], str):
                return data[key]
    # fallback: return full JSON as string
    return json.dumps(data, ensure_ascii=False)

# ----------------- Compression (rare) -----------------

def compress_history(chat_id):
    chat_id = str(chat_id)
    history = get_history(chat_id)
    messages = history.get("messages", [])
    if not messages:
        return True
    # соберём диалог для сжатия
    dialog_parts = []
    for message in messages:
        role = message.get("role")
        role_name = "Пользователь" if role == "user" else "Ассистент"
        dialog_parts.append(f"{role_name}: {message.get('content', '')}")
    dialog_text = "\n\n".join(dialog_parts)
    compression_prompt = (
        "Сожми историю разговора в понятное резюме. Дай только содержание, без вступлений."
    )
    system = {"role": "system", "content": compression_prompt}
    user = {"role": "user", "content": dialog_text}
    try:
        summary = call_cloud_chat([system, user], max_tokens=1200)
    except Exception as e:
        print("[HISTORY] Ошибка сжатия:", repr(e))
        traceback.print_exc()
        # fallback: оставляем последние
        with history_lock:
            history["messages"] = messages[-KEEP_RECENT_MESSAGES:]
            history["user_messages_since_compression"] = 0
            save_histories()
        return False

    if not summary or not str(summary).strip():
        # fallback
        with history_lock:
            history["messages"] = messages[-KEEP_RECENT_MESSAGES:]
            history["user_messages_since_compression"] = 0
            save_histories()
        return False

    with history_lock:
        recent = messages[-KEEP_RECENT_MESSAGES:]
        history["summary"] = summary
        history["messages"] = recent
        history["user_messages_since_compression"] = 0
        save_histories()
    return True


def should_compress(chat_id):
    history = get_history(chat_id)
    user_count = history.get("user_messages_since_compression", 0)
    chars = total_history_chars(history)
    return user_count >= COMPRESSION_EVERY_USER_MESSAGES or chars >= MAX_HISTORY_CHARS

# ----------------- Build messages -----------------

def build_messages(chat_id):
    history = get_history(chat_id)
    messages = []
    # system prompt short
    system_prompt = (
        "Ты — Fast Answer, отвечай кратко и по существу на русском языке."
    )
    messages.append({"role": "system", "content": system_prompt})
    summary = history.get("summary", "").strip()
    if summary:
        messages.append({"role": "system", "content": "Краткое содержание предыдущей части диалога:\n" + summary})
    for m in history.get("messages", []):
        if m.get("role") in ("user", "assistant"):
            messages.append({"role": m.get("role"), "content": m.get("content", "")})
    return messages

# ----------------- Generate response -----------------

def generate_response(chat_id, user_message):
    chat_id = str(chat_id)
    user_message = user_message.strip()
    if len(user_message) > MAX_USER_TEXT_CHARS:
        user_message = user_message[:MAX_USER_TEXT_CHARS] + "\n\n[Текст был автоматически сокращён.]"
    add_message(chat_id, "user", user_message)
    if should_compress(chat_id):
        try:
            compress_history(chat_id)
        except Exception as e:
            print("[HISTORY] Ошибка при сжатии:", repr(e))
    messages = build_messages(chat_id)
    # добавим текущее сообщение
    messages.append({"role": "user", "content": user_message})
    try:
        raw = call_cloud_chat(messages, max_tokens=MAX_OUTPUT_TOKENS)
    except Exception as e:
        print("[AI] Ошибка cloud API:", repr(e))
        traceback.print_exc()
        return "⚠️ Ошибка обращения к AI-сервису. Попробуйте позже."
    answer = clean_answer(str(raw))
    add_message(chat_id, "assistant", answer)
    return answer

# ----------------- Telegram helpers -----------------

def split_text(text, max_length=4000):
    if len(text) <= max_length:
        return [text]
    parts = []
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at < max_length // 2:
            split_at = text.rfind(" ", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        parts.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        parts.append(text)
    return parts


def send_long_message(chat_id, text, reply_to_message_id=None):
    parts = split_text(text)
    for i, part in enumerate(parts):
        if i == 0 and reply_to_message_id:
            bot.send_message(chat_id, part, reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, part)

# ----------------- Handlers -----------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    get_history(message.chat.id)
    # show info and available model (single cloud model)
    bot.send_message(message.chat.id, f"Привет! Бот работает через облачный API. Модель: {CLOUD_MODEL}\nОтправь текстовое сообщение.")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    user_text = (message.text or "").strip()
    if not user_text:
        return
    if user_text.startswith("/"):
        return
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
        answer = generate_response(chat_id, user_text)
        send_long_message(chat_id, answer, reply_to_message_id=message.message_id)
    except Exception as e:
        print("TEXT HANDLER ERROR:", repr(e))
        traceback.print_exc()
        bot.reply_to(message, "⚠️ Внутренняя ошибка. Попробуйте позже.")

# Отключаем обработку фото/документов/аудио/видео — закомментированы
# (Если нужно — можно восстановить по примеру предыдущих версий)

# @bot.message_handler(content_types=['photo'])
# def handle_photo(message):
#     bot.reply_to(message, "📸 Обработка фото временно отключена.")

# @bot.message_handler(content_types=['document'])
# def handle_document(message):
#     bot.reply_to(message, "📄 Обработка документов временно отключена.")

# @bot.message_handler(content_types=['voice','audio'])
# def handle_audio(message):
#     bot.reply_to(message, "🎤 Обработка аудио временно отключена.")

# @bot.message_handler(content_types=['video'])
# def handle_video(message):
#     bot.reply_to(message, "📹 Обработка видео временно отключена.")

# ----------------- Run -----------------

def main():
    load_histories()
    print("Бот запущен с использованием CLOUD_API_URL:", CLOUD_API_URL)
    bot.remove_webhook()
    time.sleep(1)
    bot.infinity_polling(skip_pending=True)

if __name__ == '__main__':
    main()
