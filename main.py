import os
import json
import time
import threading
import traceback
from pathlib import Path
from typing import Optional

import telebot
from groq import Groq

import file_parser


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY не задан в переменных окружения")


# ============================================================
# CLIENTS
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# SETTINGS
# ============================================================

# Через сколько сообщений пользователя запускаем сжатие.
COMPRESSION_EVERY_USER_MESSAGES = 12

# Сколько последних сообщений оставить после сжатия.
KEEP_RECENT_MESSAGES = 8

# Максимальное количество символов истории.
MAX_HISTORY_CHARS = 30000

# Максимальный размер текста одного пользовательского сообщения,
# который отправляем в модель.
MAX_USER_TEXT_CHARS = 20000

# Максимальный ответ модели.
MAX_OUTPUT_TOKENS = 2000

# Модели идут в порядке приоритета.
#
# Мы НЕ предполагаем, что каждая из них обязательно доступна.
# Перед использованием бот получает список моделей Groq.
MODEL_PRIORITY = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]

# Если сжатие не удалось, оставляем последние сообщения.
FALLBACK_RECENT_MESSAGES = 8


# ============================================================
# LOCKS
# ============================================================

history_lock = threading.RLock()


# ============================================================
# IN-MEMORY HISTORY
# ============================================================

conversation_histories = {}


# ============================================================
# LOAD / SAVE HISTORY
# ============================================================

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

        print(
            f"История загружена: "
            f"{len(conversation_histories)} чатов"
        )

    except Exception as e:
        print("ОШИБКА ЗАГРУЗКИ ИСТОРИИ:")
        print(e)
        traceback.print_exc()
        conversation_histories = {}


def save_histories():
    temp_file = HISTORY_FILE.with_suffix(".tmp")

    try:
        with history_lock:

            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(
                    conversation_histories,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            os.replace(temp_file, HISTORY_FILE)

    except Exception as e:
        print("ОШИБКА СОХРАНЕНИЯ ИСТОРИИ:")
        print(e)
        traceback.print_exc()


# ============================================================
# HISTORY HELPERS
# ============================================================

def get_history(chat_id):
    chat_id = str(chat_id)

    with history_lock:
        if chat_id not in conversation_histories:
            conversation_histories[chat_id] = {
                "summary": "",
                "messages": [],
                "user_messages_since_compression": 0
            }

        return conversation_histories[chat_id]


def add_message(chat_id, role, content):
    chat_id = str(chat_id)

    with history_lock:
        history = get_history(chat_id)

        history["messages"].append({
            "role": role,
            "content": content
        })

        if role == "user":
            history["user_messages_since_compression"] += 1

        save_histories()


def total_history_chars(history):
    total = len(history.get("summary", ""))

    for message in history.get("messages", []):
        total += len(str(message.get("content", "")))

    return total


# ============================================================
# MODEL DISCOVERY
# ============================================================

def get_available_models():
    """
    Получает список реально доступных моделей Groq.

    Если API списка моделей временно недоступен,
    возвращаем MODEL_PRIORITY как fallback.
    """

    try:
        models_response = groq_client.models.list()

        available = set()

        for model in models_response.data:
            model_id = getattr(model, "id", None)

            if model_id:
                available.add(model_id)

        print(
            f"Groq: найдено доступных моделей: "
            f"{len(available)}"
        )

        selected = [
            model
            for model in MODEL_PRIORITY
            if model in available
        ]

        if selected:
            print(
                "Порядок моделей: "
                + " -> ".join(selected)
            )
            return selected

        print(
            "ВНИМАНИЕ: ни одна модель из MODEL_PRIORITY "
            "не найдена среди доступных."
        )

        return MODEL_PRIORITY.copy()

    except Exception as e:
        print("ОШИБКА ПОЛУЧЕНИЯ СПИСКА МОДЕЛЕЙ:")
        print(e)
        print("Использую локальный список моделей.")

        return MODEL_PRIORITY.copy()


AVAILABLE_MODELS = get_available_models()


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
Ты — Fast Answer, интеллектуальный Telegram-ассистент.

Главные правила:

1. Всегда отвечай пользователю на русском языке,
   если пользователь явно не попросил другой язык.

2. Отвечай понятно, точно и по существу.

3. Не выдумывай факты.
   Если не уверен — прямо скажи об этом.

4. Учитывай предыдущую историю диалога.

5. Если пользователь ссылается на предыдущие сообщения,
   используй сохранённую историю.

6. Не говори пользователю о внутренних моделях,
   API, fallback-механизме или технической реализации,
   если это не является предметом вопроса.

7. Если пользователь прислал текст документа,
   анализируй именно этот документ.

8. Не повторяй вопрос пользователя без необходимости.

9. Если задача требует пошагового решения,
   давай решение последовательно.

10. Форматируй ответ так, чтобы его было удобно читать
    в Telegram.
"""


# ============================================================
# BUILD MODEL MESSAGES
# ============================================================

def build_messages(chat_id):
    history = get_history(chat_id)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    summary = history.get("summary", "").strip()

    if summary:
        messages.append({
            "role": "system",
            "content": (
                "Краткое содержание предыдущей части диалога:\n"
                + summary
            )
        })

    for message in history.get("messages", []):
        role = message.get("role")
        content = message.get("content", "")

        if role not in ("user", "assistant"):
            continue

        messages.append({
            "role": role,
            "content": content
        })

    return messages


# ============================================================
# HISTORY COMPRESSION
# ============================================================

def compress_history(chat_id):
    """
    Сжимает историю отдельным запросом к модели.

    ВАЖНО:
    Это отдельный API-запрос.
    Он не использует обычный запрос пользователя.
    """

    chat_id = str(chat_id)
    history = get_history(chat_id)

    messages = history.get("messages", [])

    if not messages:
        return True

    print(
        f"[HISTORY] Запускаю сжатие истории "
        f"для chat_id={chat_id}"
    )

    dialog_parts = []

    for message in messages:
        role = message.get("role")

        if role == "user":
            role_name = "Пользователь"
        else:
            role_name = "Ассистент"

        dialog_parts.append(
            f"{role_name}: {message.get('content', '')}"
        )

    dialog_text = "\n\n".join(dialog_parts)

    old_summary = history.get("summary", "").strip()

    if old_summary:
        compression_input = (
            "Предыдущее резюме:\n"
            f"{old_summary}\n\n"
            "Новые сообщения диалога:\n"
            f"{dialog_text}"
        )
    else:
        compression_input = (
            "Сообщения диалога:\n"
            f"{dialog_text}"
        )

    compression_prompt = """
Сожми историю разговора.

Твоя задача — создать компактное, но информативное резюме,
которое позволит другому ИИ продолжить разговор так,
будто он видел всю предыдущую переписку.

Обязательно сохрани:

- главные темы;
- цели пользователя;
- важные факты;
- конкретные числа;
- названия;
- решения, которые уже были приняты;
- предпочтения пользователя, если они важны;
- незавершённые задачи;
- контекст последних вопросов.

Не придумывай ничего нового.

Пиши резюме на русском языке.

Не пиши вступление вроде "Вот краткое содержание".
Сразу дай содержание.
"""

    compression_messages = [
        {
            "role": "system",
            "content": compression_prompt
        },
        {
            "role": "user",
            "content": compression_input
        }
    ]

    last_error = None

    for model in AVAILABLE_MODELS:

        try:
            print(
                f"[HISTORY] Сжатие: пробую модель {model}"
            )

            completion = groq_client.chat.completions.create(
                model=model,
                messages=compression_messages,
                max_tokens=1200,
                temperature=0.2
            )

            summary = (
                completion.choices[0]
                .message
                .content
                .strip()
            )

            if not summary:
                raise RuntimeError(
                    "Модель вернула пустое резюме."
                )

            with history_lock:

                # Сохраняем только последние сообщения.
                recent = messages[-KEEP_RECENT_MESSAGES:]

                history["summary"] = summary
                history["messages"] = recent
                history["user_messages_since_compression"] = 0

                save_histories()

            print(
                f"[HISTORY] Сжатие успешно через {model}. "
                f"Новый размер summary: {len(summary)} символов."
            )

            return True

        except Exception as e:

            last_error = e

            print(
                f"[HISTORY] ОШИБКА МОДЕЛИ {model}:"
            )
            print(repr(e))
            traceback.print_exc()

            continue

    print(
        "[HISTORY] Не удалось сжать историю."
    )

    if last_error:
        print(
            "[HISTORY] Последняя ошибка:",
            repr(last_error)
        )

    # Безопасный fallback:
    # удаляем старые сообщения, но сохраняем последние.
    with history_lock:

        history["messages"] = messages[-FALLBACK_RECENT_MESSAGES:]
        history["user_messages_since_compression"] = 0

        save_histories()

    return False


# ============================================================
# SHOULD COMPRESS
# ============================================================

def should_compress(chat_id):
    history = get_history(chat_id)

    user_count = history.get(
        "user_messages_since_compression",
        0
    )

    chars = total_history_chars(history)

    return (
        user_count >= COMPRESSION_EVERY_USER_MESSAGES
        or chars >= MAX_HISTORY_CHARS
    )


# ============================================================
# GENERATE RESPONSE
# ============================================================

def generate_response(chat_id, user_message):

    chat_id = str(chat_id)

    user_message = user_message.strip()

    if len(user_message) > MAX_USER_TEXT_CHARS:
        user_message = user_message[:MAX_USER_TEXT_CHARS] + (
            "\n\n[Текст был автоматически сокращён из-за "
            "ограничения размера.]"
        )

    # --------------------------------------------------------
    # Добавляем сообщение пользователя ОДИН раз.
    # --------------------------------------------------------

    add_message(
        chat_id,
        "user",
        user_message
    )

    # --------------------------------------------------------
    # Сжимаем историю ПОСЛЕ добавления сообщения.
    # --------------------------------------------------------

    if should_compress(chat_id):

        try:
            compress_history(chat_id)

        except Exception as e:
            print(
                "[HISTORY] Критическая ошибка сжатия:"
            )
            print(repr(e))
            traceback.print_exc()

    # --------------------------------------------------------
    # Формируем запрос.
    # --------------------------------------------------------

    messages = build_messages(chat_id)

    last_error = None

    # --------------------------------------------------------
    # Fallback моделей.
    # --------------------------------------------------------

    for index, model in enumerate(AVAILABLE_MODELS):

        try:

            print(
                f"[AI] chat_id={chat_id} "
                f"модель={model}"
            )

            completion = groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.7
            )

            answer = (
                completion.choices[0]
                .message
                .content
                .strip()
            )

            if not answer:
                raise RuntimeError(
                    "Модель вернула пустой ответ."
                )

            # Сохраняем ответ.
            add_message(
                chat_id,
                "assistant",
                answer
            )

            print(
                f"[AI] Успешно: {model}"
            )

            return answer

        except Exception as e:

            last_error = e

            # ОБЯЗАТЕЛЬНО пишем подробность в консоль.
            print()
            print("=" * 70)
            print(
                f"[AI] ОШИБКА МОДЕЛИ: {model}"
            )
            print(
                f"[AI] Ошибка: {repr(e)}"
            )
            print("=" * 70)
            traceback.print_exc()
            print()

            # Если есть следующая модель,
            # пытаемся использовать её.
            if index + 1 < len(AVAILABLE_MODELS):

                next_model = AVAILABLE_MODELS[index + 1]

                print(
                    f"[AI] Переключаюсь: "
                    f"{model} -> {next_model}"
                )

                continue

    # --------------------------------------------------------
    # Все модели не сработали.
    # --------------------------------------------------------

    print(
        "[AI] ВСЕ МОДЕЛИ НЕ СРАБОТАЛИ."
    )

    if last_error:
        print(
            "[AI] Последняя ошибка:",
            repr(last_error)
        )

    return (
        "⚠️ Сейчас не удалось получить ответ от ИИ. "
        "Я попробовал несколько доступных моделей. "
        "Попробуй отправить запрос ещё раз немного позже."
    )


# ============================================================
# TELEGRAM MESSAGE SPLITTER
# ============================================================

def split_text(text, max_length=4000):

    if len(text) <= max_length:
        return [text]

    parts = []

    while len(text) > max_length:

        split_at = text.rfind(
            "\n",
            0,
            max_length
        )

        if split_at < max_length // 2:
            split_at = text.rfind(
                " ",
                0,
                max_length
            )

        if split_at <= 0:
            split_at = max_length

        parts.append(text[:split_at].strip())

        text = text[split_at:].strip()

    if text:
        parts.append(text)

    return parts


def send_long_message(chat_id, text, reply_to_message_id=None):

    parts = split_text(text)

    for index, part in enumerate(parts):

        if index == 0 and reply_to_message_id:

            bot.send_message(
                chat_id,
                part,
                reply_to_message_id=reply_to_message_id
            )

        else:

            bot.send_message(
                chat_id,
                part
            )


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):

    get_history(message.chat.id)

    bot.send_message(
        message.chat.id,
        "🚀 Привет! Я Fast Answer.\n\n"
        "Я умею:\n"
        "• 💬 вести диалог с сохранением истории;\n"
        "• 🧠 автоматически сжимать длинную историю;\n"
        "• 📄 анализировать документы;\n"
        "• 📸 работать с изображениями, если доступна подходящая модель;\n"
        "• 🔄 автоматически переключаться на другую модель "
        "при ошибке.\n\n"
        "Можешь просто написать мне сообщение."
    )


# ============================================================
# TEXT
# ============================================================

@bot.message_handler(content_types=["text"])
def handle_text(message):

    user_text = (message.text or "").strip()

    if not user_text:
        return

    if user_text.startswith("/"):
        return

    chat_id = message.chat.id

    try:

        bot.send_chat_action(
            chat_id,
            "typing"
        )

        answer = generate_response(
            chat_id,
            user_text
        )

        send_long_message(
            chat_id,
            answer,
            reply_to_message_id=message.message_id
        )

    except Exception as e:

        print("КРИТИЧЕСКАЯ ОШИБКА TEXT HANDLER:")
        print(repr(e))
        traceback.print_exc()

        bot.reply_to(
            message,
            "⚠️ Произошла внутренняя ошибка. "
            "Попробуй повторить запрос."
        )


# ============================================================
# DOCUMENTS
# ============================================================

@bot.message_handler(content_types=["document"])
def handle_document(message):

    chat_id = message.chat.id

    try:

        bot.send_chat_action(
            chat_id,
            "typing"
        )

        document = message.document

        file_info = bot.get_file(
            document.file_id
        )

        downloaded = bot.download_file(
            file_info.file_path
        )

        suffix = Path(
            document.file_name or ""
        ).suffix

        temp_path = None

        try:

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=suffix
            ) as tmp:

                tmp.write(downloaded)
                temp_path = tmp.name

            extracted_text = file_parser.parse_file(
                temp_path,
                document.file_name or ""
            )

            if not extracted_text.strip():

                bot.reply_to(
                    message,
                    "⚠️ Не удалось извлечь текст из документа."
                )

                return

            prompt = (
                "Пользователь отправил документ.\n\n"
                "Имя файла: "
                f"{document.file_name}\n\n"
                "Текст документа:\n"
                f"{extracted_text}\n\n"
                "Проанализируй документ и ответь на запрос "
                "пользователя, если он был указан в подписи."
            )

            # Если в caption есть запрос пользователя,
            # используем его.
            if message.caption:
                prompt = (
                    f"Пользователь прислал файл "
                    f"{document.file_name}.\n\n"
                    f"Запрос пользователя:\n"
                    f"{message.caption}\n\n"
                    f"Содержимое файла:\n"
                    f"{extracted_text}"
                )

            answer = generate_response(
                chat_id,
                prompt
            )

            send_long_message(
                chat_id,
                answer,
                reply_to_message_id=message.message_id
            )

        finally:

            if temp_path:

                try:
                    os.remove(temp_path)

                except Exception:
                    pass

    except Exception as e:

        print("ОШИБКА ОБРАБОТКИ ДОКУМЕНТА:")
        print(repr(e))
        traceback.print_exc()

        bot.reply_to(
            message,
            "⚠️ Не удалось обработать этот документ."
        )


# ============================================================
# PHOTO
# ============================================================

def get_vision_models():

    # Список потенциальных vision-моделей.
    # Проверяем фактическое наличие через API.

    candidates = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
    ]

    try:

        models_response = groq_client.models.list()

        available = {
            getattr(model, "id", "")
            for model in models_response.data
        }

        return [
            model
            for model in candidates
            if model in available
        ]

    except Exception as e:

        print(
            "[VISION] Не удалось получить список моделей:"
        )
        print(repr(e))

        return []


@bot.message_handler(content_types=["photo"])
def handle_photo(message):

    chat_id = message.chat.id

    try:

        bot.send_chat_action(
            chat_id,
            "typing"
        )

        vision_models = get_vision_models()

        if not vision_models:

            bot.reply_to(
                message,
                "📸 Сейчас у доступных моделей Groq "
                "нет подходящей модели для анализа изображения."
            )

            return

        largest_photo = message.photo[-1]

        file_info = bot.get_file(
            largest_photo.file_id
        )

        image_bytes = bot.download_file(
            file_info.file_path
        )

        import base64

        encoded_image = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        caption = (
            message.caption.strip()
            if message.caption
            else "Опиши подробно, что изображено на фотографии."
        )

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": caption
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url":
                            f"data:image/jpeg;base64,{encoded_image}"
                        }
                    }
                ]
            }
        ]

        last_error = None

        for model in vision_models:

            try:

                print(
                    f"[VISION] Пробую модель {model}"
                )

                completion = groq_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0.5
                )

                answer = (
                    completion.choices[0]
                    .message
                    .content
                    .strip()
                )

                if not answer:
                    raise RuntimeError(
                        "Vision-модель вернула пустой ответ."
                    )

                send_long_message(
                    chat_id,
                    answer,
                    reply_to_message_id=message.message_id
                )

                print(
                    f"[VISION] Успешно: {model}"
                )

                return

            except Exception as e:

                last_error = e

                print(
                    f"[VISION] ОШИБКА МОДЕЛИ {model}:"
                )
                print(repr(e))
                traceback.print_exc()

        print(
            "[VISION] Все vision-модели завершились ошибкой."
        )

        if last_error:
            print(repr(last_error))

        bot.reply_to(
            message,
            "⚠️ Не удалось проанализировать изображение."
        )

    except Exception as e:

        print("КРИТИЧЕСКАЯ ОШИБКА PHOTO HANDLER:")
        print(repr(e))
        traceback.print_exc()

        bot.reply_to(
            message,
            "⚠️ Произошла ошибка при обработке изображения."
        )


# ============================================================
# AUDIO
# ============================================================

@bot.message_handler(content_types=["voice", "audio"])
def handle_audio(message):

    bot.reply_to(
        message,
        "🎤 Обработка аудио пока не подключена."
    )


# ============================================================
# OTHER
# ============================================================

@bot.message_handler(
    content_types=[
        "video",
        "sticker",
        "contact",
        "location",
        "venue",
        "animation",
        "video_note"
    ]
)
def handle_other(message):

    bot.reply_to(
        message,
        "⚠️ Этот тип контента пока не поддерживается."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    load_histories()

    print()
    print("=" * 60)
    print("🚀 Fast Answer запущен")
    print("📱 Telegram: Long Polling")
    print("🧠 История: сохраняется на диск")
    print(
        "🔄 Сжатие каждые "
        f"{COMPRESSION_EVERY_USER_MESSAGES} "
        "сообщений пользователя"
    )
    print(
        "🤖 Модели: "
        + " -> ".join(AVAILABLE_MODELS)
    )
    print("=" * 60)
    print()

    bot.remove_webhook()

    # Небольшая задержка после удаления webhook,
    # чтобы Telegram успел обработать изменение.
    time.sleep(1)

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )


if __name__ == "__main__":
    main()
