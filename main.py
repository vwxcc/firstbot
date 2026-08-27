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

# Поддержка формата HEIC (iPhone)
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
    print("[КРИТИЧЕСКАЯ ОШИБКА] BOT_TOKEN или GROQ_API_KEY отсутствуют. Остановка.")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

# Хранилище контекста диалогов
HISTORY_FILE = os.getenv("HISTORY_FILE_PATH", "/data/history.json")
if not os.path.exists(os.path.dirname(HISTORY_FILE)) and os.path.dirname(HISTORY_FILE) != "":
    HISTORY_FILE = "local_history.json"

COMPRESSION_EVERY = 12
MAX_CHARS_BEFORE_COMPRESSION = 2000

# ОБНОВЛЕННЫЕ МОДЕЛИ 2026 ГОДА
TEXT_MODELS = [
    "openai/gpt-oss-120b",  # Флагманская модель, 131k контекст, 500 t/s
    "openai/gpt-oss-20b"    # Резервная модель, 1000 t/s
]

VISION_MODELS = [
    "llama-3.2-90b-vision-preview" 
]

AUDIO_MODELS = [
    "whisper-large-v3-turbo",
    "whisper-large-v3"
]

SYSTEM_PROMPT = """Ты — полезный, дружелюбный AI-ассистент.
ПРАВИЛА:
1. Отвечай ИСКЛЮЧИТЕЛЬНО на русском языке.
2. Будь кратким, понятным и вежливым. Избегай сухого технического стиля.
3. Отвечай прямо на вопрос.
4. Учитывай контекст предыдущего диалога.
5. Используй эмодзи для передачи эмоций, но не переборщи."""

conversation_histories = {}

# ==========================================
# 2. ПОДСИСТЕМА ПАМЯТИ
# ==========================================
def load_histories():
    global conversation_histories
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                conversation_histories = json.load(f)
        except Exception as e:
            print(f"[ПАМЯТЬ] Ошибка чтения базы: {e}")
            conversation_histories = {}

def save_histories():
    """Атомарное сохранение для защиты от повреждения при сбое питания контейнера."""
    temp_file = HISTORY_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(conversation_histories, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, HISTORY_FILE)
    except Exception as e:
        print(f"[ПАМЯТЬ] Сбой сохранения: {e}")

def add_message(chat_id: int, role: str, content: str):
    chat_id_str = str(chat_id)
    if chat_id_str not in conversation_histories:
        conversation_histories[chat_id_str] = []
    
    conversation_histories[chat_id_str].append({"role": role, "content": content})
    
    user_msgs = sum(1 for m in conversation_histories[chat_id_str] if m['role'] == 'user')
    total_chars = sum(len(m['content']) for m in conversation_histories[chat_id_str])
    
    if user_msgs >= COMPRESSION_EVERY or total_chars > MAX_CHARS_BEFORE_COMPRESSION:
        compress_history(chat_id_str)
    else:
        save_histories()

def compress_history(chat_id: str):
    """Динамическое сжатие контекста."""
    history = conversation_histories[chat_id]
    to_compress = history[:-4]
    to_keep = history[-4:]
    
    if not to_compress:
        return

    dialogue_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in to_compress])
    
    prompt = (
        "Сделай максимально краткое резюме этого диалога (факты, имена, обсуждаемые вещи). Пиши на русском.\n\n"
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
        
        new_history = [{"role": "assistant", "content": f"[Краткие воспоминания]: {summary}"}]
        new_history.extend(to_keep)
        
        conversation_histories[chat_id] = new_history
        save_histories()
    except Exception as e:
        print(f"[ПАМЯТЬ] Ошибка сжатия: {e}")

# ==========================================
# 3. МАРШРУТИЗАЦИЯ LLM (ФОЛЛБЕК)
# ==========================================
def generate_response(chat_id: int) -> str:
    chat_id_str = str(chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_histories.get(chat_id_str, []))

    for model_name in TEXT_MODELS:
        try:
            response = groq_client.chat.completions.create(
                messages=messages,
                model=model_name,
                temperature=0.6,
                max_tokens=2500,
                timeout=25.0
            )
            return clean_answer(response.choices[0].message.content)
        except Exception as e:
            print(f"[ИИ] Ошибка {model_name}: {e}")
            if model_name != TEXT_MODELS[-1]:
                try:
                    bot.send_message(chat_id, "Ой, задумался 😅 Пробую еще раз, секундочку... ⏳")
                except Exception:
                    pass
    
    return "К сожалению, сейчас мои серверы перегружены 😔 Пожалуйста, попробуй написать чуть позже!"

def generate_vision_response(chat_id: int, prompt_text: str, base64_images: List[str]) -> str:
    content = [{"type": "text", "text": prompt_text}]
    for b64 in base64_images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    messages = [{"role": "user", "content": content}]

    for model_name in VISION_MODELS:
        try:
            response = groq_client.chat.completions.create(
                messages=messages,
                model=model_name,
                temperature=0.4,
                max_tokens=1500,
                timeout=35.0
            )
            return clean_answer(response.choices[0].message.content)
        except Exception as e:
            print(f"[ЗРЕНИЕ] Ошибка {model_name}: {e}")
            if model_name != VISION_MODELS[-1]:
                try:
                    bot.send_message(chat_id, "Мои глазки немного устали 😵‍💫 Пробую посмотреть через резервную сеть... 👁️")
                except Exception:
                    pass
    
    return "Прости, не могу разглядеть это изображение 😔 Возможно, файл поврежден."

def transcribe_audio(file_path: str) -> Optional[str]:
    for model_name in AUDIO_MODELS:
        try:
            with open(file_path, "rb") as file:
                return groq_client.audio.transcriptions.create(
                    file=(file_path, file.read()),
                    model=model_name,
                    prompt="Распознай русскую речь.",
                    response_format="text",
                    language="ru"
                )
        except Exception as e:
            print(f"[АУДИО] Ошибка {model_name}: {e}")
    return None

def clean_answer(text: str) -> str:
    text = re.sub(r'\*\*\*(.*?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'^###\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{3,}', '', text)
    return text.strip()

def send_long_message(chat_id: int, text: str):
    max_length = 4000
    for i in range(0, len(text), max_length):
        bot.send_message(chat_id, text[i:i+max_length])

# ==========================================
# 4. ОБРАБОТКА МУЛЬТИМЕДИА
# ==========================================
def process_image(file_info, downloaded_file, file_extension: str) -> Optional[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, f"input{file_extension}")
        with open(input_path, 'wb') as f:
            f.write(downloaded_file)
            
        try:
            img = Image.open(input_path)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            # Ограничение для матриц Llama 3.2 Vision
            img.thumbnail((1120, 1120))
            output_path = os.path.join(temp_dir, "optimized.jpg")
            img.save(output_path, "JPEG", quality=85)
            
            with open(output_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            print(f"[ИЗОБРАЖЕНИЕ] Ошибка: {e}")
            return None

def extract_video_frames(video_path: str, num_frames=6) -> List[str]:
    frames_b64 = []
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            probe_cmd = [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path
            ]
            result = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            duration = float(result.stdout.strip())
            
            if duration <= 0:
                raise ValueError("Продолжительность 0")

            intervals = [duration * i / (num_frames + 1) for i in range(1, num_frames + 1)]
            
            for i, ts in enumerate(intervals):
                frame_path = os.path.join(temp_dir, f"frame_{i}.jpg")
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                    "-vframes", "1", "-q:v", "2", "-vf", "scale=-1:720", frame_path
                ]
                subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                if os.path.exists(frame_path):
                    with open(frame_path, "rb") as f:
                        frames_b64.append(base64.b64encode(f.read()).decode('utf-8'))
        except Exception as e:
            print(f"[ВИДЕО] Ошибка раскадровки: {e}")
    return frames_b64

# ==========================================
# 5. ДИАЛОГИ (TELEGRAM HANDLERS)
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "Привет! 👋 Я твой умный ИИ-помощник. Отправь мне текст, голосовое сообщение, картинку или документ, и я с радостью помогу! ✨")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    try:
        add_message(message.chat.id, "user", message.text)
        bot.send_chat_action(message.chat.id, 'typing')
        
        answer = generate_response(message.chat.id)
        
        add_message(message.chat.id, "assistant", answer)
        send_long_message(message.chat.id, answer)
    except Exception as e:
        print(f"[ОШИБКА] Текст: {e}")
        bot.send_message(message.chat.id, "Ой, что-то пошло не так при обработке текста 🛠️ Попробуй еще раз чуть позже!")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    try:
        bot.send_chat_action(message.chat.id, 'upload_photo')
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        b64_image = process_image(file_info, downloaded_file, ".jpg")
        if not b64_image:
            bot.reply_to(message, "Не могу прочитать эту картинку 😔 Возможно, файл поврежден.")
            return

        prompt_text = message.caption if message.caption else "Посмотри на эту картинку и расскажи подробно, что на ней изображено."
        add_message(message.chat.id, "user", f"[Отправлена картинка] {message.caption or ''}")
        
        answer = generate_vision_response(message.chat.id, prompt_text, [b64_image])
        
        add_message(message.chat.id, "assistant", answer)
        send_long_message(message.chat.id, answer)
        
    except Exception as e:
        print(f"[ОШИБКА] Фото: {e}")
        bot.reply_to(message, "Произошла ошибка при загрузке фото 💔")

@bot.message_handler(content_types=['voice', 'audio'])
def handle_audio(message):
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
            
            bot.reply_to(message, "🎙️ Слушаю твое голосовое сообщение... 🎧")
            transcribed_text = transcribe_audio(audio_path)
            
            if not transcribed_text:
                bot.reply_to(message, "Не расслышал... 🙉 Что-то не так с аудио, попробуй записать еще раз!")
                return
                
            add_message(message.chat.id, "user", f"[Голосовое]: {transcribed_text}")
            answer = generate_response(message.chat.id)
            
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, f"📝 *Ты сказал(а):* _{transcribed_text}_\n\n{answer}")

    except Exception as e:
        print(f"[ОШИБКА] Аудио: {e}")
        bot.reply_to(message, "У меня не получилось открыть эту запись 😔")

@bot.message_handler(content_types=['document', 'video'])
def handle_document_and_video(message):
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

        if file_size > 20 * 1024 * 1024:
            bot.reply_to(message, "Ого, какой большой файл! 😲 Телеграм разрешает качать файлы только до 20 МБ. Попробуй сжать его.")
            return

        ext = os.path.splitext(filename)[1].lower()
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        if ext in ['.heic', '.heif']:
            b64_image = process_image(file_info, downloaded_file, ext)
            prompt_text = message.caption if message.caption else "Что изображено на этом фото?"
            add_message(message.chat.id, "user", f"[HEIC Фото] {message.caption or ''}")
            answer = generate_vision_response(message.chat.id, prompt_text, [b64_image])
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, answer)
            return
            
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mpeg', '.mpg']
        if ext in video_extensions or message.content_type == 'video':
            with tempfile.TemporaryDirectory() as temp_dir:
                video_path = os.path.join(temp_dir, f"video{ext}")
                with open(video_path, 'wb') as f:
                    f.write(downloaded_file)
                
                bot.reply_to(message, "🎥 Видео получено! Смотрю кадры, дай мне пару секунд... 🍿")
                frames = extract_video_frames(video_path, num_frames=6)
                
                if not frames:
                    bot.reply_to(message, "Не смог разглядеть кадры в этом видео 🎞️ Возможно, формат не поддерживается.")
                    return
                    
                prompt_text = message.caption if message.caption else "Посмотри на эти кадры из видео и расскажи, что там происходит."
                add_message(message.chat.id, "user", f"[Видео: {filename}] {message.caption or ''}")
                
                answer = generate_vision_response(message.chat.id, prompt_text, frames)
                
                add_message(message.chat.id, "assistant", answer)
                send_long_message(message.chat.id, answer)
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(downloaded_file)
                
            bot.reply_to(message, "📄 Читаю документ, секундочку... 🧐")
            extracted_text = parse_file(file_path, filename)
            
            instruction = message.caption if message.caption else "Прочитай этот документ и выдели главную мысль."
            add_message(message.chat.id, "user", f"[Документ: {filename}]\nЗапрос: {instruction}\n\nТекст:\n{extracted_text}")
            
            answer = generate_response(message.chat.id)
            
            add_message(message.chat.id, "assistant", answer)
            send_long_message(message.chat.id, answer)

    except Exception as e:
        print(f"[ОШИБКА] Файл: {e}")
        bot.reply_to(message, "Ох, не получилось прочитать этот файл 📂 Может, попробуем другой?")

# ==========================================
# 6. ЖИЗНЕННЫЙ ЦИКЛ ПОДКЛЮЧЕНИЯ И ЗАПУСК
# ==========================================
def check_telegram_token():
    try:
        me = bot.get_me()
        bot.remove_webhook()
        return True
    except Exception as e:
        print(f"[ФАТАЛЬНО] Ошибка токена: {e}")
        return False

def main():
    if not check_telegram_token():
        return
        
    load_histories()
    
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
        except ApiTelegramException as e:
            if e.error_code == 409:
                time.sleep(15)
            elif e.error_code == 401:
                break
            elif e.error_code == 429:
                time.sleep(30)
            else:
                time.sleep(5)
        except httpx.ReadTimeout:
             time.sleep(3)
        except Exception as e:
            time.sleep(5)

if __name__ == "__main__":
    main()
