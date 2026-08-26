import os
import telebot
import tempfile
from huggingface_hub import InferenceClient
import file_parser
from PIL import Image
import time
import traceback

# ------------------- Конфигурация -------------------
TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not HF_TOKEN:
    raise ValueError("HF_TOKEN не задан!")

bot = telebot.TeleBot(TOKEN)
client = InferenceClient(token=HF_TOKEN)

# Списки моделей для разных задач (с приоритетом)
TEXT_MODELS = [
    "meta-llama/Llama-3.1-8B-Instruct",
    "Qwen/Qwen2.5-3B-Instruct",
    "mistralai/Mistral-7B-Instruct-v0.2",
    "google/gemma-2-9b-it",
    "deepseek-ai/DeepSeek-V3-0324"
]

IMAGE_MODELS = [
    "Salesforce/blip-image-captioning-large",
    "google/vit-gpt2-image-captioning",
    "nlpconnect/vit-gpt2-image-captioning"
]

AUDIO_MODEL = "openai/whisper-large-v3"

# ------------------- Вспомогательные функции -------------------

def generate_text_with_fallback(prompt: str, max_len=500) -> str:
    """Пытается сгенерировать текст, перебирая модели при ошибках."""
    last_error = None
    for model in TEXT_MODELS:
        try:
            print(f"Попытка использовать модель: {model}")
            messages = [{"role": "user", "content": prompt}]
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_len,
                temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Модель {model} не сработала: {e}")
            last_error = e
            continue
    return f"⚠️ Все модели временно недоступны. Последняя ошибка: {last_error}"

def describe_image_with_fallback(image_path: str) -> str:
    """Пытается описать изображение, перебирая модели."""
    last_error = None
    for model in IMAGE_MODELS:
        try:
            print(f"Попытка описать изображение через {model}")
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            result = client.image_to_text(
                model=model,
                image=image_bytes
            )
            if result and result.generated_text:
                return result.generated_text
        except Exception as e:
            print(f"Модель {model} не сработала: {e}")
            last_error = e
            continue
    return f"⚠️ Не удалось описать изображение. Ошибка: {last_error}"

def transcribe_audio_with_fallback(audio_path: str) -> str:
    """Транскрибирует аудио (без переключения моделей, только одна)."""
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        result = client.automatic_speech_recognition(
            model=AUDIO_MODEL,
            audio=audio_bytes
        )
        return result.text if result else "Не удалось распознать речь."
    except Exception as e:
        return f"⚠️ Ошибка распознавания аудио: {e}"

# ------------------- Обработчики сообщений -------------------

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "🤖 Привет! Я бот Fast Answer.\n"
        "Отправь мне текст, фото, аудио, документ (PDF, DOCX, PPTX, XLSX, CSV, TXT) или HEIC-файл.\n"
        "Я постараюсь дать быстрый ответ!")

# ---------- Текст ----------
@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "Пустое сообщение...")
        return
    # Сообщаем о начале генерации
    bot.send_chat_action(message.chat.id, "typing")
    msg = bot.reply_to(message, "🧠 Генерирую ответ...")
    answer = generate_text_with_fallback(user_text)
    bot.edit_message_text(f"💬 {answer}", chat_id=message.chat.id, message_id=msg.message_id)

# ---------- Фото ----------
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    tmp_path = None
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            tmp.write(downloaded_file)
            tmp_path = tmp.name

        bot.send_chat_action(message.chat.id, "typing")
        msg = bot.reply_to(message, "🖼️ Смотрю на фото...")
        description = describe_image_with_fallback(tmp_path)
        bot.edit_message_text(f"📸 {description}", chat_id=message.chat.id, message_id=msg.message_id)
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка при обработке фото: {e}")
        traceback.print_exc()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ---------- Документы (все типы) ----------
@bot.message_handler(content_types=['document'])
def handle_document(message):
    tmp_path = None
    try:
        file_id = message.document.file_id
        file_name = message.document.file_name
        extension = file_name.split('.')[-1].lower() if '.' in file_name else ''
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{extension}') as tmp:
            tmp.write(downloaded_file)
            tmp_path = tmp.name

        # ---- HEIC: конвертируем в JPEG и обрабатываем как фото ----
        if extension == 'heic':
            bot.send_chat_action(message.chat.id, "typing")
            msg = bot.reply_to(message, "🔄 Конвертирую HEIC в JPEG...")
            jpeg_path = file_parser.parse_heic(tmp_path)
            if isinstance(jpeg_path, str) and jpeg_path.endswith('.jpg'):
                description = describe_image_with_fallback(jpeg_path)
                bot.edit_message_text(f"🖼️ (HEIC) {description}", chat_id=message.chat.id, message_id=msg.message_id)
                os.unlink(jpeg_path)
            else:
                bot.edit_message_text(f"❌ Не удалось обработать HEIC: {jpeg_path}", chat_id=message.chat.id, message_id=msg.message_id)
            return

        # ---- Остальные документы: извлекаем текст ----
        bot.send_chat_action(message.chat.id, "typing")
        msg = bot.reply_to(message, "📄 Извлекаю текст из документа...")
        extracted_text = file_parser.parse_file(tmp_path, extension)

        if not extracted_text:
            bot.edit_message_text("⚠️ В документе нет текста для обработки.", chat_id=message.chat.id, message_id=msg.message_id)
            return

        # Обрезаем длинные тексты
        if len(extracted_text) > 3000:
            extracted_text = extracted_text[:3000] + "... (обрезано)"

        # Генерируем ответ по содержанию
        bot.edit_message_text("🧠 Генерирую ответ по документу...", chat_id=message.chat.id, message_id=msg.message_id)
        answer = generate_text_with_fallback(f"Содержание документа: {extracted_text}\n\nДай краткий ответ по этому содержанию.")
        bot.edit_message_text(f"📄 Ответ: {answer}", chat_id=message.chat.id, message_id=msg.message_id)

    except file_parser.ParseError as e:
        bot.reply_to(message, f"❌ Ошибка парсинга файла: {e}")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
        traceback.print_exc()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ---------- Аудио / голосовые ----------
@bot.message_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    tmp_path = None
    try:
        file_id = message.voice.file_id if message.content_type == 'voice' else message.audio.file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        ext = '.ogg' if message.content_type == 'voice' else '.mp3'
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(downloaded_file)
            tmp_path = tmp.name

        bot.send_chat_action(message.chat.id, "typing")
        msg = bot.reply_to(message, "🎤 Распознаю речь...")
        transcript = transcribe_audio_with_fallback(tmp_path)

        if transcript.startswith("⚠️") or "ошибка" in transcript.lower():
            bot.edit_message_text(f"{transcript}", chat_id=message.chat.id, message_id=msg.message_id)
            return

        # Показываем транскрипцию и генерируем ответ
        bot.edit_message_text(f"📝 Распознано: {transcript}", chat_id=message.chat.id, message_id=msg.message_id)
        # Генерируем ответ на основе транскрипции
        answer = generate_text_with_fallback(f"Вопрос: {transcript}")
        bot.send_message(message.chat.id, f"💬 {answer}")

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
        traceback.print_exc()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

# ---------- Видео (задел) ----------
@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.reply_to(message, "🎬 Видео пока не обрабатываются, но скоро добавлю!")

# ---------- Остальные типы ----------
@bot.message_handler(content_types=['sticker', 'contact', 'location', 'venue', 'animation', 'video_note'])
def handle_other(message):
    bot.reply_to(message, "Извините, я пока не умею обрабатывать этот тип контента.")

# ---------- Запуск ----------
if __name__ == '__main__':
    bot.remove_webhook()
    print("🤖 Бот Fast Answer запущен (Long Polling)...")
    bot.infinity_polling()
