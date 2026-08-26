import os
import telebot
import tempfile
import requests
from huggingface_hub import InferenceClient
import file_parser
from PIL import Image
import io
import time
import traceback

# Переменные окружения
TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not HF_TOKEN:
    raise ValueError("HF_TOKEN не задан!")

bot = telebot.TeleBot(TOKEN)
client = InferenceClient(token=HF_TOKEN)

# ---------- Функции для Hugging Face ----------
def generate_text_response(prompt: str, max_len=500) -> str:
    try:
        messages = [{"role": "user", "content": prompt}]
        completion = client.chat_completion(
            model="google/gemma-2-2b-it",
            messages=messages,
            max_tokens=max_len
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Ошибка при генерации текста: {e}"

def describe_image(image_path: str) -> str:
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        result = client.image_to_text(
            model="Salesforce/blip-image-captioning-large",
            image=image_bytes
        )
        return result.generated_text if result else "Не удалось описать изображение."
    except Exception as e:
        return f"Ошибка при описании изображения: {e}"

def transcribe_audio(audio_path: str) -> str:
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        result = client.automatic_speech_recognition(
            model="openai/whisper-large-v3",
            audio=audio_bytes
        )
        return result.text if result else "Не удалось распознать речь."
    except Exception as e:
        return f"Ошибка при транскрипции аудио: {e}"

# ---------- Обработчики ----------
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, 
        "Привет! Я бот Fast Answer! Отправь мне текст, фото, аудио или документ, и я дам быстрый ответ.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "Пустое сообщение...")
        return
    bot.send_chat_action(message.chat.id, "typing")
    answer = generate_text_response(user_text)
    bot.reply_to(message, answer)

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
        description = describe_image(tmp_path)
        bot.reply_to(message, f"📸 {description}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

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

        # HEIC: конвертируем в JPEG и обрабатываем как фото
        if extension == 'heic':
            jpeg_path = file_parser.parse_heic(tmp_path)
            if isinstance(jpeg_path, str) and jpeg_path.endswith('.jpg'):
                description = describe_image(jpeg_path)
                bot.reply_to(message, f"🖼️ (HEIC) {description}")
                os.unlink(jpeg_path)
            else:
                bot.reply_to(message, f"Ошибка HEIC: {jpeg_path}")
            return

        # Остальные документы: извлекаем текст
        extracted_text = file_parser.parse_file(tmp_path, extension)
        if not extracted_text:
            bot.reply_to(message, "В документе нет текста для обработки.")
            return

        # Обрезаем длинные тексты
        if len(extracted_text) > 3000:
            extracted_text = extracted_text[:3000] + "... (обрезано)"
        
        bot.send_chat_action(message.chat.id, "typing")
        answer = generate_text_response(f"Содержание документа: {extracted_text}\n\nДай краткий ответ по этому содержанию.")
        bot.reply_to(message, f"📄 Ответ: {answer}")
    except file_parser.ParseError as e:
        bot.reply_to(message, f"Ошибка парсинга файла: {e}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
        traceback.print_exc()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

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
        transcript = transcribe_audio(tmp_path)
        if transcript.startswith("Ошибка"):
            bot.reply_to(message, f"Не удалось распознать: {transcript}")
            return
        bot.reply_to(message, f"🎤 Распознано: {transcript}")
        # Также даём ответ по тексту
        answer = generate_text_response(f"Вопрос: {transcript}")
        bot.send_message(message.chat.id, f"💬 {answer}")
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.reply_to(message, "Видео пока не обрабатываются, но скоро добавлю!")

@bot.message_handler(content_types=['sticker', 'contact', 'location', 'venue', 'animation', 'video_note'])
def handle_other(message):
    bot.reply_to(message, "Извините, я пока не умею обрабатывать этот тип контента.")

if __name__ == '__main__':
    bot.remove_webhook()
    print("Бот Fast Answer запущен (Long Polling)...")
    bot.infinity_polling()
