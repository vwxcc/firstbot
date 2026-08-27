import os
import json
import time
import base64
import tempfile
import subprocess
import re
from typing import List, Dict, Optional

import telebot
from telebot.apihelper import ApiTelegramException
from groq import Groq
import httpx

# Нативная интеграция поддержки HEIC/HEIF в Pillow
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()

from file_parser import parse_file

# ==========================================
# 1. КОНФИГУРАЦИЯ И СИСТЕМНЫЕ ЗАВИСИМОСТИ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN or not GROQ_API_KEY:
    print("[FATAL] BOT_TOKEN или GROQ_API_KEY отсутствуют в системном окружении. Остановка приложения.")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

# Конфигурация постоянного хранилища (Persistent Storage на Render)
HISTORY_FILE = os.getenv("HISTORY_FILE_PATH", "/data/history.json")
if not os.path.exists(os.path.dirname(HISTORY_FILE)) and os.path.dirname(HISTORY_FILE) != "":
    # Fallback для локального тестирования вне среды Render
    HISTORY_FILE = "local_history.json"

COMPRESSION_EVERY = 12
MAX_CHARS_BEFORE_COMPRESSION = 2000

# Иерархия моделей для обеспечения отказоустойчивости (Fallback)
TEXT_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it"
]

VISION_MODELS = [
    "llama-3.2-90b-vision-preview",
    "llama-3.2-11b-vision-preview"
]

AUDIO_MODELS = [
    "whisper-large-v3-turbo",
    "whisper-large-v3"
]

SYSTEM_PROMPT = """Ты — полезный AI-ассистент профессионального уровня.
СТРОГИЕ ПРАВИЛА:
1. Отвечай ИСКЛЮЧИТЕЛЬНО на русском языке, независимо от языка запроса (если пользователь явно не попросил перевести).
2. Формулируй мысли четко, без лишних вводных слов и рассуждений.
3. Отвечай непосредственно на поставленный вопрос.
4. Обязательно учитывай контекст предыдущего диалога.
5. Не используй избыточное форматирование Markdown (ограничь использование жирного текста и заголовков)."""

# Оперативная память для кэширования истории (RAM Storage)
conversation_histories = {}

# ==========================================
# 2. ПОДСИСТЕМА УПРАВЛЕНИЯ ПАМЯТЬЮ И СЖАТИЯ
# ==========================================
def load_histories():
    """Загрузка персистентной истории из JSON файла в RAM."""
    global conversation_histories
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                conversation_histories = json.load(f)
            print(f"[HISTORY] База данных диалогов успешно загружена из {HISTORY_FILE}")
        except Exception as e:
            print(f"[HISTORY] Критическая ошибка десериализации {HISTORY_FILE}: {e}")
            conversation_histories = {}

def save_histories():
    """Атомарная транзакция сохранения для предотвращения повреждения файла при SIGKILL."""
    temp_file = HISTORY_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(conversation_histories, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, HISTORY_FILE)
    except Exception as e:
        print(f"[HISTORY] Сбой фиксации транзакции истории: {e}")

def add_message(chat_id: int, role: str, content: str):
    """Добавление сообщения в контекст и триггер фонового сжатия."""
    chat_id_str = str(chat_id)
    if chat_id_str not in conversation_histories:
        conversation_histories[chat_id_str] = []
    
    conversation_histories[chat_id_str].append({"role": role, "content": content})
    
    # Эвристика необходимости сжатия контекста
    user_msgs = sum(1 for m in conversation_histories[chat_id_str] if m['role'] == 'user')
    total_chars = sum(len(m['content']) for m in conversation_histories[chat_id_str])
    
    if user_msgs >= COMPRESSION_EVERY or total_chars > MAX_CHARS_BEFORE_COMPRESSION:
        compress_history(chat_id_str)
    else:
        save_histories()

def compress_history(chat_id: str):
    """Асинхронная суммаризация устаревшего контекста для высвобождения токенов."""
    print(f"[HISTORY] Инициирована процедура сжатия для сессии {chat_id}")
    history = conversation_histories[chat_id]
    
    # Сохраняем "горячий" контекст (последние 4 итерации)
    to_compress = history[:-4]
    to_keep = history[-4:]
    
    if not to_compress:
        return

    dialogue_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in to_compress])
    
    prompt = (
        "Сделай максимально краткое и информативное резюме следующего диалога. "
        "Выдели основные обсуждаемые темы, запросы пользователя и твои ключевые ответы. "
        "Пиши только факты на русском языке.\n\n"
        f"ДИАЛОГ:\n{dialogue_text}"
    )

    try:
        response = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=TEXT_MODELS[0],
            temperature=0.2,
            max_tokens=700,
        )
        summary = response.choices[0].message.content
        
        # Реконструкция памяти: системное саммари + горячий контекст
        new_history = [{"role": "assistant", "content": f"[Архивное резюме контекста]: {summary}"}]
        new_history.extend(to_keep)
        
        conversation_histories[chat_id] = new_history
        save_histories()
        print(f"[HISTORY] Процедура сжатия успешно завершена для {chat_id}")
    except Exception as e:
        print(f"[HISTORY] Ошибка нейросетевого сжатия: {e}. Контекст оставлен без изменений.")

# ==========================================
# 3. МАРШРУТИЗАЦИЯ И ОБРАБОТКА LLM (FALLBACK)
# ==========================================
def generate_response(chat_id: int) -> str:
    """Текстовая генерация с каскадным переключением моделей."""
    chat_id_str = str(chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_histories.get(chat_id_str, []))

    for model_name in TEXT_MODELS:
        try:
            print(f"[AI] Инициализация инференса. Модель: {model_name}")
            response = groq_client.chat.completions.create(
                messages=messages,
                model=model_name,
                temperature=0.6,
                max_tokens=2500,
                timeout=25.0
            )
            answer = response.choices[0].message.content
            print(f"[AI] Успешная генерация: {model_name}")
            return clean_answer(answer)
        except Exception as e:
            print(f"[AI] Ошибка модели {model_name}")
            print(f"[AI] Трассировка сбоя: {e}")
            print(f"[AI] Перенаправление на резервную модель...")
            
            if model_name != TEXT_MODELS[-1]:
                try:
                    bot.send_message(chat_id, "Модель временно не ответила, пробую другую. Ещё немного...")
                except Exception:
                    pass
    
    return "К сожалению, в данный момент вычислительные мощности недоступны. Пожалуйста, повторите запрос позже."

def generate_vision_response(chat_id: int, prompt_text: str, base64_images: List[str]) -> str:
    """Генерация ответов на основе мультимодального анализа (Vision)."""
    content = [{"type": "text", "text": prompt_text}]
    for b64 in base64_images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    messages = [{"role": "user", "content": content}]

    for model_name in VISION_MODELS:
        try:
            print(f"[VISION] Инициализация анализа изображений. Модель: {model_name}")
            response = groq_client.chat.completions.create(
                messages=messages,
                model=model_name,
                temperature=0.4,
                max_tokens=1500,
                timeout=35.0
            )
            answer = response.choices[0].message.content
            print(f"[VISION] Визуальный анализ завершен: {model_name}")
            return clean_answer(answer)
        except Exception as e:
            print(f"[VISION] Сбой Vision-модели {model_name}: {e}")
            if model_name != VISION_MODELS[-1]:
                try:
                    bot.send_message(chat_id, "Оптический модуль временно занят, переключаюсь на резервный...")
                except Exception:
                    pass
    
    return "Произошел сбой при визуальном анализе файла. Возможно, изображение повреждено или сервис перегружен."

def transcribe_audio(file_path: str) -> Optional[str]:
    """Транскрибация аудио с использованием Whisper API."""
    for model_name in AUDIO_MODELS:
        try:
            print(f"[AUDIO] Запуск транскрибации. Модель: {model_name}")
            with open(file_path, "rb") as file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(file_path, file.read()),
                    model=model_name,
                    prompt="Распознай русскую речь.",
                    response_format="text",
                    language="ru"
                )
            print(f"[AUDIO] Транскрибация успешна: {model_name}")
            return transcription
        except Exception as e:
            print(f"[AUDIO] Ошибка распознавания речи {model_name}: {e}")
    return None

def clean_answer(text: str) -> str:
    """Санитаризация вывода LLM от избыточных символов Markdown."""
    # Удаление тройных и двойных декоративных звездочек, используемых моделями для выделения
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    # Очистка от избыточных Markdown-заголовков (###), оставляя текст
    text = re.sub(r'^###\s+', '', text, flags=re.MULTILINE)
    # Удаление горизонтальных линий и прочего спама
    text = re.sub(r'\*{3,}', '', text)
    return text.strip()

def send_long_message(chat_id: int, text: str):
    """Фрагментация ответа для соблюдения ограничений API Telegram (4096 символов)."""
    max_length = 4000
    for i in range(0, len(text), max_length):
        bot.send_message(chat_id, text[i:i+max_length])

# ==========================================
# 4. КОНВЕЙЕРЫ ОБРАБОТКИ МУЛЬТИМЕДИА
# ==========================================
def process_image(file_info, downloaded_file, file_extension: str) -> Optional[str]:
    """Декодирование HEIC/JPEG/PNG, нормализация и кодирование в Base64."""
    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, f"input{file_extension}")
        with open(input_path, 'wb') as f:
            f.write(downloaded_file)
            
        try:
            img = Image.open(input_path)
            # Конвертация цветовых пространств с альфа-каналом (RGBA/CMYK) в стандартный RGB
            if img.mode != 'RGB':
                img = img.convert('RGB')
                
            # Жесткое лимитирование разрешения для Llama 3.2 Vision (max 1120x1120)
            img.thumbnail((1120, 1120))
            
            output_path = os.path.join(temp_dir, "optimized.jpg")
            img.save(output_path, "JPEG", quality=85)
            
            with open(output_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            print(f"[IMAGE_PROCESSOR] Ошибка растеризации изображения: {e}")
            return None

def extract_video_frames(video_path: str, num_frames=6) -> List[str]:
    """Многопоточное извлечение репрезентативных кадров видеопотока через FFmpeg."""
    frames_b64 = []
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Получение точного хронометража медиаконтейнера
            probe_cmd = [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path
            ]
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            duration = float(result.stdout.strip())
            
            if duration <= 0:
                raise ValueError("Сбой парсинга хронометража.")

            # Расчет временных меток для равномерного покрытия
            intervals = [duration * i / (num_frames + 1) for i in range(1, num_frames + 1)]
            
            for i, ts in enumerate(intervals):
                frame_path = os.path.join(temp_dir, f"frame_{i}.jpg")
                # Извлечение одного кадра, понижение качества (q:v 2) и масштабирование по высоте до 720p
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                    "-vframes", "1", "-q:v", "2", "-vf", "scale=-1:720", frame_path
                ]
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(frame_path):
                    with open(frame_path, "rb") as f:
                        frames_b64.append(base64.b64encode(f.read()).decode('utf-8'))
                        
        except Exception as e:
            print(f"[FFMPEG] Ошибка конвейера извлечения кадров: {e}")
            
    return frames_b64

# ==========================================
# 5. ИНТЕРФЕЙС ТЕЛЕГРАМ-СОБЫТИЙ (HANDLERS)
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Система инициализирована. Ожидаю ввод текстовых запросов, документов, изображений или медиафайлов для анализа.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    print(f"[TELEGRAM_ROUTER] Текстовый запрос от {message.chat.id}")
    try:
        add_message(message.chat.id, "user", message.text)
        bot.send_chat_action(message.chat.id, 'typing')
        
        answer = generate_response(message.chat.id)
        
        add_message(message.chat.id, "assistant", answer)
        send_long_message(message.chat.id, answer)
    except Exception as e:
        print(f"[TEXT_HANDLER] Сбой обработки: {e}")
        bot.send_message(message.chat.id, "Внутренняя системная ошибка маршрутизации текста.")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    print(f"[TELEGRAM_ROUTER] Вектор/Изображение от {message.chat.id}")
    try:
        bot.send_chat_action(message.chat.id, 'upload_photo')
        # Получение изображения максимального разрешения из массива
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        b64_image = process_image(file_info, downloaded_file, ".jpg")
        if not b64_image:
            bot.reply_to(message, "Ошибка буферизации графического контента.")
            return

        prompt_text = message.caption if message.caption else "Выполни детализированный оптический анализ предоставленного изображения. Пиши на русском."
        add_message(message.chat.id, "user", f"[Отправлен визуальный контент] {message.caption or ''}")
        
        answer = generate_vision_response(message.chat.id, prompt_text, [b64_image])
        
        add_message(message.chat.id, "assistant", answer)
        send_long_message(message.chat.id, answer)
        
    except Exception as e:
        print(f"[PHOTO_HANDLER] Ошибка: {e}")
        bot.reply_to(message, "Критический сбой пайплайна обработки фото.")

@bot.message_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    print(f"[TELEGRAM_ROUTER] Аудио/Голосовое от {message.chat.id}")
    try:
        bot.send_chat_action(message.chat.id, 'record_audio')
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        ext = os.path.splitext(file_info.file_path)[1] or ".ogg"
        
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = os.path.join(temp_dir, f"audio{ext}")
            with open(audio_path, 'wb') as f:
                f.write(downloaded_file)
            
            bot.reply_to(message, "Инициирована акустическая транскрибация...")
            transcribed_text = transcribe_audio(audio_path)
            
            if not transcribed_text:
                bot.reply_to(message, "Сбой нейросетевого модуля распознавания речи.")
                return
                
            # Интеграция полученного текста в обычный текстовый пайплайн
            add_message(message.chat.id, "user", f"[Голосовое сообщение]: {transcribed_text}")
            answer = generate_response(message.chat.id)
            
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, f"Распознано: _{transcribed_text}_\n\n{answer}")

    except Exception as e:
        print(f"[AUDIO_HANDLER] Ошибка обработки аудио: {e}")
        bot.reply_to(message, "Не удалось обработать аудиодорожку.")

@bot.message_handler(content_types=['document', 'video'])
def handle_document_and_video(message):
    print(f"[TELEGRAM_ROUTER] Документ/Медиаконтейнер от {message.chat.id}")
    try:
        if message.content_type == 'video':
            file_id = message.video.file_id
            filename = message.video.file_name or "video.mp4"
            file_size = message.video.file_size
            bot.send_chat_action(message.chat.id, 'record_video')
        else:
            file_id = message.document.file_id
            filename = message.document.file_name or "file.unknown"
            file_size = message.document.file_size
            bot.send_chat_action(message.chat.id, 'upload_document')

        # Ограничение физического размера файла (Telegram API лимит 20 MB для стандартного клиента)
        if file_size > 20 * 1024 * 1024:
            bot.reply_to(message, "Размер бинарного объекта превышает протокольный лимит платформы (20 МБ).")
            return

        ext = os.path.splitext(filename)[1].lower()
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Инъекция логики обработки HEIC/HEIF
        if ext in ['.heic', '.heif']:
            b64_image = process_image(file_info, downloaded_file, ext)
            prompt_text = message.caption if message.caption else "Проанализируй данное HEIC-изображение."
            add_message(message.chat.id, "user", f"[HEIC Контейнер] {message.caption or ''}")
            answer = generate_vision_response(message.chat.id, prompt_text, [b64_image])
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, answer)
            return
            
        # Маршрутизатор видеопотоков
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mpeg', '.mpg']
        if ext in video_extensions or message.content_type == 'video':
            with tempfile.TemporaryDirectory() as temp_dir:
                video_path = os.path.join(temp_dir, f"video{ext}")
                with open(video_path, 'wb') as f:
                    f.write(downloaded_file)
                
                bot.reply_to(message, "Медиафайл загружен. Идет демультиплексирование и извлечение кадров...")
                frames = extract_video_frames(video_path, num_frames=6)
                
                if not frames:
                    bot.reply_to(message, "Модуль FFmpeg не смог извлечь матрицу кадров.")
                    return
                    
                prompt_text = message.caption if message.caption else "Перед тобой раскадровка видеоряда. Проанализируй динамику, объекты и смысл."
                add_message(message.chat.id, "user", f"[Отправлен видеопоток: {filename}] {message.caption or ''}")
                
                print(f"[VIDEO_PIPELINE] Векторизовано {len(frames)} кадров. Вызов Vision-модели.")
                answer = generate_vision_response(message.chat.id, prompt_text, frames)
                
                add_message(message.chat.id, "assistant", answer)
                send_long_message(message.chat.id, answer)
            return

        # Маршрутизатор статических документов
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(downloaded_file)
                
            print(f"[DOC_PIPELINE] Вызов подсистемы парсинга для {filename}")
            extracted_text = parse_file(file_path, filename)
            
            instruction = message.caption if message.caption else "Внимательно изучи предоставленный документ и выдели главную мысль."
            add_message(message.chat.id, "user", f"[Документ: {filename}]\nДиректива: {instruction}\n\nТранскрипция:\n{extracted_text}")
            
            answer = generate_response(message.chat.id)
            
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, answer)

    except Exception as e:
        print(f"[FILE_HANDLER] Глобальный сбой обработки потока: {e}")
        bot.reply_to(message, "Ошибка десериализации или анализа бинарного файла.")

# ==========================================
# 6. ЖИЗНЕННЫЙ ЦИКЛ ПОДКЛЮЧЕНИЯ И ЗАПУСК
# ==========================================
def check_telegram_token():
    """Верификация токена и очистка стейта перед стартом."""
    try:
        me = bot.get_me()
        print(f"[SYSTEM_BOOT] Fast Answer Bot Architecture Initialization")
        print(f"[API_GATEWAY] Токен валиден. Идентификатор: @{me.username}")
        # Принудительный сброс Webhook для подготовки к режиму Long Polling
        bot.remove_webhook()
        print(f"[API_GATEWAY] Webhook кэш очищен.")
        return True
    except Exception as e:
        print(f"[FATAL_ERROR] Сбой аутентификации Telegram API: {e}")
        return False

def main():
    if not check_telegram_token():
        return
        
    load_histories()
    print("[NETWORK] Протокол Long Polling активирован.")
    
    # Главный цикл событий
    while True:
        try:
            # Параметр none_stop=True игнорирует локальные таймауты
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except ApiTelegramException as e:
            if e.error_code == 409:
                print(f"[NETWORK_EXCEPTION] 409 Conflict: Зафиксирована коллизия процессов. Активация Exponential Backoff (15 сек)...")
                time.sleep(15)
            elif e.error_code == 401:
                print(f"[FATAL_ERROR] 401 Unauthorized: Токен отозван сервером. Немедленная остановка.")
                break
            elif e.error_code == 429:
                print(f"[NETWORK_EXCEPTION] 429 Too Many Requests: Троттлинг на стороне сервера. Ожидание 30 сек...")
                time.sleep(30)
            else:
                print(f"[API_EXCEPTION] Необработанная ошибка Telegram: {e}")
                time.sleep(5)
        except httpx.ReadTimeout:
             print("[NETWORK_EXCEPTION] Таймаут сокета. Инициирована переустановка соединения...")
             time.sleep(3)
        except Exception as e:
            print(f"[SYSTEM_EXCEPTION] Неожиданный сбой в главном потоке: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
