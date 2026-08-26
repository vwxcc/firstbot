import os
import json
import time
import threading
import traceback
import tempfile
import subprocess
import base64
import re

from pathlib import Path
from typing import Optional

import telebot
from groq import Groq

import file_parser

# ============================================================
# CONFIG / CLIENTS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY не задан в переменных окружения")

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

# ============================================================
# PATHS / STORAGE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_FILE = DATA_DIR / "history.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# SETTINGS
# ============================================================

COMPRESSION_EVERY_USER_MESSAGES = 12
KEEP_RECENT_MESSAGES = 8
MAX_HISTORY_CHARS = 30000
MAX_USER_TEXT_CHARS = 20000
MAX_OUTPUT_TOKENS = 2000
MODEL_PRIORITY = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]
FALLBACK_RECENT_MESSAGES = 8

# Locks and in-memory history
history_lock = threading.RLock()
conversation_histories = {}

# ============================================================
# HELPERS: load/save history
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
        print(f"История загружена: {len(conversation_histories)} чатов")
    except Exception as e:
        print("ОШИБКА ЗАГРУЗКИ ИСТОРИИ:")
        print(repr(e))
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
        print("ОШИБКА СОХРАНЕНИЯ ИСТОРИИ:")
        print(repr(e))
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

# ============================================================
# MODEL DISCOVERY
# ============================================================

def get_available_models():
    try:
        models_response = groq_client.models.list()
        available = set()
        for model in models_response.data:
            model_id = getattr(model, "id", None)
            if model_id:
                available.add(model_id)
        selected = [m for m in MODEL_PRIORITY if m in available]
        return selected if selected else MODEL_PRIORITY.copy()
    except Exception as e:
        print("ОШИБКА ПОЛУЧЕНИЯ СПИСКА МОДЕЛЕЙ:", repr(e))
        traceback.print_exc()
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
# BUILD MESSAGES
# ============================================================

def build_messages(chat_id):
    history = get_history(chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    summary = history.get("summary", "").strip()
    if summary:
        messages.append({"role": "system", "content": "Краткое содержание предыдущей части диалога:\n" + summary})
    for message in history.get("messages", []):
        role = message.get("role")
        content = message.get("content", "")
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": content})
    return messages

# ============================================================
# HISTORY COMPRESSION
# ============================================================

def compress_history(chat_id):
    chat_id = str(chat_id)
    history = get_history(chat_id)
    messages = history.get("messages", [])
    if not messages:
        return True
    dialog_parts = []
    for message in messages:
        role = message.get("role")
        role_name = "Пользователь" if role == "user" else "Ассистент"
        dialog_parts.append(f"{role_name}: {message.get('content', '')}")
    dialog_text = "\n\n".join(dialog_parts)
    old_summary = history.get("summary", "").strip()
    if old_summary:
        compression_input = f"Предыдущее резюме:\n{old_summary}\n\nНовые сообщения диалога:\n{dialog_text}"
    else:
        compression_input = f"Сообщения диалога:\n{dialog_text}"
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
    compression_messages = [{"role": "system", "content": compression_prompt}, {"role": "user", "content": compression_input}]
    last_error = None
    for model in AVAILABLE_MODELS:
        try:
            completion = groq_client.chat.completions.create(model=model, messages=compression_messages, max_tokens=1200, temperature=0.2)
            summary = completion.choices[0].message.content.strip()
            if not summary:
                raise RuntimeError("Модель вернула пустое резюме.")
            with history_lock:
                recent = messages[-KEEP_RECENT_MESSAGES:]
                history["summary"] = summary
                history["messages"] = recent
                history["user_messages_since_compression"] = 0
                save_histories()
            return True
        except Exception as e:
            last_error = e
            print(f"[HISTORY] ОШИБКА МОДЕЛИ {model}:")
            print(repr(e))
            traceback.print_exc()
            continue
    # fallback
    with history_lock:
        history["messages"] = messages[-FALLBACK_RECENT_MESSAGES:]
        history["user_messages_since_compression"] = 0
        save_histories()
    return False


def should_compress(chat_id):
    history = get_history(chat_id)
    user_count = history.get("user_messages_since_compression", 0)
    chars = total_history_chars(history)
    return user_count >= COMPRESSION_EVERY_USER_MESSAGES or chars >= MAX_HISTORY_CHARS

# ============================================================
# CLEAN ANSWER (remove excessive Markdown)
# ============================================================

def clean_answer(text: str) -> str:
    """
    Убирает из ответа чрезмерное Markdown-форматирование.
    """
    if not text:
        return text
    text = text.replace("***", "")
    text = text.replace("**", "")
    text = re.sub(r"(?<!\w)__([^_\n]+)__", r"\1", text)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^\s*\*+\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ============================================================
# GENERATE RESPONSE (uses Groq) — now cleans the answer
# ============================================================

def generate_response(chat_id, user_message):
    chat_id = str(chat_id)
    user_message = user_message.strip()
    if len(user_message) > MAX_USER_TEXT_CHARS:
        user_message = user_message[:MAX_USER_TEXT_CHARS] + "\n\n[Текст был автоматически сокращён из-за ограничения размера.]"
    add_message(chat_id, "user", user_message)
    if should_compress(chat_id):
        try:
            compress_history(chat_id)
        except Exception as e:
            print("[HISTORY] Критическая ошибка сжатия:", repr(e))
            traceback.print_exc()
    messages = build_messages(chat_id)
    last_error = None
    for index, model in enumerate(AVAILABLE_MODELS):
        try:
            completion = groq_client.chat.completions.create(model=model, messages=messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.7)
            answer = completion.choices[0].message.content.strip()
            if not answer:
                raise RuntimeError("Модель вернула пустой ответ.")
            answer = clean_answer(answer)
            add_message(chat_id, "assistant", answer)
            return answer
        except Exception as e:
            last_error = e
            print("\n" + "=" * 70)
            print(f"[AI] ОШИБКА МОДЕЛИ: {model}")
            print(f"[AI] Ошибка: {repr(e)}")
            print("=" * 70)
            traceback.print_exc()
            if index + 1 < len(AVAILABLE_MODELS):
                continue
    print("[AI] ВСЕ МОДЕЛИ НЕ СРАБОТАЛИ.")
    if last_error:
        print("[AI] Последняя ошибка:", repr(last_error))
    return ("⚠️ Сейчас не удалось получить ответ от ИИ. Я попробовал несколько доступных моделей. Попробуй отправить запрос ещё раз немного позже.")

# ============================================================
# TELEGRAM SPLIT / SEND
# ============================================================

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
    for index, part in enumerate(parts):
        if index == 0 and reply_to_message_id:
            bot.send_message(chat_id, part, reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, part)

# ============================================================
# /start and text handlers
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):
    get_history(message.chat.id)
    bot.send_message(message.chat.id, "🚀 Привет! Я Fast Answer. Отправь текст, фото, аудио или документ.")

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
        print("КРИТИЧЕСКАЯ ОШИБКА TEXT HANDLER:", repr(e))
        traceback.print_exc()
        bot.reply_to(message, "⚠️ Произошла внутренняя ошибка. Попробуй повторить запрос.")

# ============================================================
# VISION MODELS helper
# ============================================================

def get_vision_models():
    env = os.environ.get("VISION_MODELS")
    if env:
        return [m.strip() for m in env.split(",") if m.strip()]
    candidates = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
    ]
    try:
        models_response = groq_client.models.list()
        available = {getattr(model, "id", "") for model in models_response.data}
        return [m for m in candidates if m in available]
    except Exception as e:
        print("[VISION] Не удалось получить список моделей:", repr(e))
        return []

# ============================================================
# PHOTO handler (replaced)
# ============================================================

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
        vision_models = get_vision_models()
        if not vision_models:
            bot.reply_to(message, "⚠️ Сейчас нет доступной модели для анализа изображений.")
            return
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        image_bytes = bot.download_file(file_info.file_path)
        encoded_image = base64.b64encode(image_bytes).decode("utf-8")
        user_request = (message.caption.strip() if message.caption else "Подробно проанализируй это изображение и ответь на русском языке.")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": user_request},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded_image}}
            ]}
        ]
        last_error = None
        for index, model in enumerate(vision_models):
            try:
                print(f"[VISION] Пробую модель: {model}", flush=True)
                completion = groq_client.chat.completions.create(model=model, messages=messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.5)
                answer = completion.choices[0].message.content.strip()
                answer = clean_answer(answer)
                if not answer:
                    raise RuntimeError("Vision-модель вернула пустой ответ.")
                try:
                    add_message(chat_id, "user", "[Пользователь отправил изображение]\n" + user_request)
                    add_message(chat_id, "assistant", answer)
                except Exception:
                    pass
                try:
                    send_long_message(chat_id, answer, reply_to_message_id=message.message_id)
                except Exception:
                    bot.reply_to(message, answer)
                print(f"[VISION] Успешно: {model}", flush=True)
                return
            except Exception as e:
                last_error = e
                print(f"[VISION] ОШИБКА МОДЕЛИ {model}", flush=True)
                print(repr(e), flush=True)
                traceback.print_exc()
                if index + 1 < len(vision_models):
                    print("[VISION] Переключаюсь на следующую модель...", flush=True)
        print("[VISION] Все vision-модели завершились ошибкой.", flush=True)
        if last_error:
            print(repr(last_error), flush=True)
        bot.reply_to(message, "⚠️ Не получилось проанализировать изображение. Попробуй отправить его ещё раз.")
    except Exception as e:
        print("[PHOTO] КРИТИЧЕСКАЯ ОШИБКА:", repr(e), flush=True)
        traceback.print_exc()
        bot.reply_to(message, "⚠️ Произошла ошибка при обработке фотографии.")

# ============================================================
# HEIC conversion
# ============================================================

def convert_heic_to_jpeg(input_path: str, output_path: str) -> str:
    try:
        import pyheif
        from PIL import Image
        heif_file = pyheif.read(input_path)
        image = Image.frombytes(heif_file.mode, heif_file.size, heif_file.data, "raw", heif_file.mode, heif_file.stride)
        image.save(output_path, "JPEG", quality=90)
        return output_path
    except Exception as e:
        print("[HEIC] Ошибка конвертации:", repr(e))
        raise

# ============================================================
# DOCUMENT handler (replaced) — handles HEIC and video docs too
# ============================================================

@bot.message_handler(content_types=["document"])
def handle_document(message):
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
        document = message.document
        filename = document.file_name or "file"
        extension = Path(filename).suffix.lower()
        print(f"[FILE] Получен файл: {filename}")
        file_info = bot.get_file(document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = Path(temp_dir) / filename
            with open(original_path, "wb") as f:
                f.write(file_bytes)
            # HEIC / HEIF
            if extension in (".heic", ".heif"):
                jpeg_path = Path(temp_dir) / "converted.jpg"
                convert_heic_to_jpeg(str(original_path), str(jpeg_path))
                with open(jpeg_path, "rb") as f:
                    image_bytes = f.read()
                encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                caption = (message.caption.strip() if message.caption else "Проанализируй изображение и подробно ответь на русском языке.")
                vision_models = get_vision_models()
                if not vision_models:
                    bot.reply_to(message, "⚠️ Нет доступной модели для анализа HEIC.")
                    return
                vision_messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": caption},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded_image}}
                    ]}
                ]
                for model in vision_models:
                    try:
                        print(f"[HEIC] Пробую {model}")
                        completion = groq_client.chat.completions.create(model=model, messages=vision_messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.5)
                        answer = completion.choices[0].message.content.strip()
                        answer = clean_answer(answer)
                        try:
                            add_message(chat_id, "user", "[Пользователь отправил HEIC]\n" + caption)
                            add_message(chat_id, "assistant", answer)
                        except Exception:
                            pass
                        send_long_message(chat_id, answer, message.message_id)
                        return
                    except Exception as e:
                        print(f"[HEIC] Ошибка {model}: {repr(e)}")
                        traceback.print_exc()
                bot.reply_to(message, "⚠️ Не удалось проанализировать HEIC.")
                return
            # Видео как документы
            if extension in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg"):
                process_video_message(message, document.file_id, message.caption)
                return
            # Обычные документы
            extracted_text = file_parser.parse_file(str(original_path), extension.lstrip('.'))
            if not extracted_text.strip():
                bot.reply_to(message, "⚠️ Не удалось извлечь содержимое файла.")
                return
            max_document_chars = 50000
            if len(extracted_text) > max_document_chars:
                extracted_text = extracted_text[:max_document_chars] + "\n\n[Документ был сокращён из-за ограничения размера.]"
            if message.caption:
                prompt = (f"Пользователь отправил файл {filename}.\n\nЗапрос пользователя:\n{message.caption}\n\nСодержимое файла:\n{extracted_text}")
            else:
                prompt = (f"Пользователь отправил файл {filename}.\n\nПроанализируй его содержимое. Если это документ с текстом, сделай содержательный анализ. Отвечай на русском языке.\n\nСодержимое:\n{extracted_text}")
            answer = generate_response(chat_id, prompt)
            try:
                send_long_message(chat_id, answer, message.message_id)
            except Exception:
                bot.reply_to(message, answer)
    except Exception as e:
        print("[FILE] ОШИБКА:", repr(e))
        traceback.print_exc()
        bot.reply_to(message, "⚠️ Не удалось обработать этот файл.")

# ============================================================
# VIDEO processing (ffmpeg)
# ============================================================

def extract_video_frames(video_path: str, output_dir: str, max_frames: int = 6):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path
    ], capture_output=True, text=True)
    if probe.returncode != 0:
        raise RuntimeError("Не удалось определить длительность видео.")
    try:
        duration = float(probe.stdout.strip())
    except Exception:
        duration = 0
    if duration <= 0:
        raise RuntimeError("Некорректная длительность видео.")
    frame_count = min(max_frames, max(1, int(duration)))
    frames = []
    for i in range(frame_count):
        timestamp = 0 if frame_count == 1 else (duration * i / (frame_count - 1))
        output_file = output_dir / f"frame_{i}.jpg"
        result = subprocess.run([
            "ffmpeg", "-y", "-ss", str(timestamp), "-i", video_path, "-frames:v", "1", "-q:v", "2", str(output_file)
        ], capture_output=True)
        if result.returncode == 0 and output_file.exists():
            frames.append(str(output_file))
    return frames


def process_video_message(message, file_id, caption):
    chat_id = message.chat.id
    try:
        bot.send_chat_action(chat_id, "typing")
        print("[VIDEO] Начинаю обработку видео...")
        file_info = bot.get_file(file_id)
        video_bytes = bot.download_file(file_info.file_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "video"
            with open(video_path, "wb") as f:
                f.write(video_bytes)
            frames_dir = Path(temp_dir) / "frames"
            frames = extract_video_frames(str(video_path), str(frames_dir), max_frames=6)
            if not frames:
                bot.reply_to(message, "⚠️ Не удалось получить кадры видео.")
                return
            vision_models = get_vision_models()
            if not vision_models:
                bot.reply_to(message, "⚠️ Нет доступной модели для анализа видео.")
                return
            request = (caption.strip() if caption else "Проанализируй видео по представленным кадрам. Опиши, что происходит, важные события, объекты и действия. Отвечай на русском языке.")
            content = [{"type": "text", "text": request}]
            for frame_path in frames:
                with open(frame_path, "rb") as f:
                    frame_bytes = f.read()
                encoded = base64.b64encode(frame_bytes).decode("utf-8")
                content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}})
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]
            for model in vision_models:
                try:
                    print(f"[VIDEO] Пробую модель {model}")
                    completion = groq_client.chat.completions.create(model=model, messages=messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.5)
                    answer = completion.choices[0].message.content.strip()
                    answer = clean_answer(answer)
                    try:
                        add_message(chat_id, "user", "[Пользователь отправил видео]\n" + request)
                        add_message(chat_id, "assistant", answer)
                    except Exception:
                        pass
                    send_long_message(chat_id, answer, message.message_id)
                    print(f"[VIDEO] Успешно: {model}")
                    return
                except Exception as e:
                    print(f"[VIDEO] ОШИБКА {model}: {repr(e)}")
                    traceback.print_exc()
            bot.reply_to(message, "⚠️ Не удалось проанализировать видео.")
    except Exception as e:
        print("[VIDEO] КРИТИЧЕСКАЯ ОШИБКА:", repr(e))
        traceback.print_exc()
        bot.reply_to(message, "⚠️ Произошла ошибка при обработке видео.")

@bot.message_handler(content_types=["video"])
def handle_video(message):
    process_video_message(message, message.video.file_id, message.caption)

# ============================================================
# AUDIO / OTHER handlers (placeholders)
# ============================================================

@bot.message_handler(content_types=["voice", "audio"]) 
def handle_audio(message):
    bot.reply_to(message, "🎤 Обработка аудио пока не подключена.")

@bot.message_handler(content_types=["sticker", "contact", "location", "venue", "animation", "video_note"]) 
def handle_other(message):
    bot.reply_to(message, "⚠️ Этот тип контента пока не поддерживается.")

# ============================================================
# MAIN
# ============================================================

def main():
    load_histories()
    print("🚀 Fast Answer запущен (Long Polling)")
    bot.remove_webhook()
    time.sleep(1)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)

if __name__ == "__main__":
    main()
