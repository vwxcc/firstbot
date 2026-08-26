import os
import telebot
import tempfile
import json
import time
from huggingface_hub import InferenceClient
from groq import Groq
import file_parser
import traceback
from collections import defaultdict

# ---------- Переменные окружения ----------
TOKEN = os.environ.get("BOT_TOKEN")
HF_TOKEN = os.environ.get("HF_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не задан!")

# ---------- Инициализация клиентов ----------
bot = telebot.TeleBot(TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)
hf_client = InferenceClient(token=HF_TOKEN) if HF_TOKEN else None

# ---------- Хранилище истории (в памяти) ----------
# Ключ: chat_id (int), значение: список словарей [{"role": "user/assistant", "content": "..."}]
conversation_histories = defaultdict(list)

# Параметры сжатия
MAX_MESSAGES = 12          # Максимальное количество сообщений в истории до сжатия
MAX_CONTENT_LENGTH = 2000  # Максимальная общая длина текста до сжатия

# ---------- Функция для получения истории ----------
def get_history(chat_id):
    """Возвращает историю для указанного чата."""
    return conversation_histories[chat_id]

# ---------- Функция для добавления сообщения в историю ----------
def add_to_history(chat_id, role, content):
    """Добавляет сообщение в историю и при необходимости сжимает."""
    history = conversation_histories[chat_id]
    history.append({"role": role, "content": content})
    
    # Проверяем, нужно ли сжать историю
    if len(history) > MAX_MESSAGES or sum(len(m["content"]) for m in history) > MAX_CONTENT_LENGTH:
        compress_history(chat_id)

# ---------- Функция сжатия истории (без уведомления) ----------
def compress_history(chat_id):
    """
    Сжимает историю диалога, заменяя всю историю на суммаризацию.
    Работает без вывода сообщений пользователю.
    """
    history = conversation_histories[chat_id]
    if not history:
        return
    
    # Формируем текст диалога для суммаризации
    dialog_text = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    
    try:
        # Запрос к Groq на суммаризацию
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "Ты — полезный ассистент. Сделай краткую, но информативную суммаризацию следующего диалога. Выдели основные темы, запросы пользователя и ключевые ответы. Сохрани важные детали."},
                {"role": "user", "content": f"Диалог:\n{dialog_text}"}
            ],
            model="gemma2-9b-it",
            max_tokens=500,
            temperature=0.5,
        )
        summary = completion.choices[0].message.content
    except Exception as e:
        # Если не удалось сжать, оставляем историю как есть (но можно обрезать)
        print(f"Ошибка сжатия истории для {chat_id}: {e}")
        # Вместо ошибки просто обрежем историю до последних 6 сообщений
        if len(history) > 6:
            conversation_histories[chat_id] = history[-6:]
        return

    # Заменяем историю на одно сообщение от ассистента с суммаризацией
    conversation_histories[chat_id] = [
        {"role": "assistant", "content": f"Краткое содержание предыдущего диалога:\n{summary}"}
    ]
    # Можно также добавить последние 2 сообщения пользователя, чтобы сохранить контекст,
    # но для простоты оставим только суммаризацию.

# ---------- Функция для генерации ответа с учётом истории ----------
def generate_response_with_history(chat_id, user_message):
    """
    Генерирует ответ, используя историю чата и новое сообщение пользователя.
    Добавляет ответ в историю.
    """
    history = get_history(chat_id)
    # Добавляем новое сообщение пользователя в историю (перед генерацией)
    history.append({"role": "user", "content": user_message})
    
    # Проверяем длину и при необходимости сжимаем (теперь уже после добавления)
    if len(history) > MAX_MESSAGES or sum(len(m["content"]) for m in history) > MAX_CONTENT_LENGTH:
        compress_history(chat_id)
        # После сжатия история стала короче, но текущее сообщение пользователя не сохранилось,
        # поэтому добавляем его заново
        history = get_history(chat_id)
        history.append({"role": "user", "content": user_message})
    
    # Формируем список сообщений для Groq (все кроме последнего? Все)
    messages = history.copy()  # уже включает user сообщение
    
    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="gemma2-9b-it",
            max_tokens=500,
            temperature=0.7,
        )
        assistant_reply = completion.choices[0].message.content
    except Exception as e:
        assistant_reply = f"❌ Ошибка при генерации текста: {e}"
    
    # Добавляем ответ ассистента в историю
    history.append({"role": "assistant", "content": assistant_reply})
    # Обновляем историю (сохраняем)
    conversation_histories[chat_id] = history
    
    return assistant_reply

# ---------- Остальные функции (фото, аудио, документы) ----------
# (они используют отдельные функции, но для них тоже можно добавить историю,
# но пока оставим как есть, чтобы не усложнять)

def describe_image(image_path: str) -> str:
    if not hf_client:
        return "⚠️ API для изображений не настроен (нет HF_TOKEN)."
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        result = hf_client.image_to_text(
            model="Salesforce/blip-image-captioning-large",
            image=image_bytes
        )
        return result.generated_text if result else "Не удалось описать изображение."
    except Exception as e:
        if "402" in str(e):
            return "⚠️ Закончились кредиты Hugging Face для изображений."
        return f"❌ Ошибка при описании фото: {e}"

def transcribe_audio(audio_path: str) -> str:
    if not hf_client:
        return "⚠️ API для аудио не настроен (нет HF_TOKEN)."
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        result = hf_client.automatic_speech_recognition(
            model="openai/whisper-large-v3",
            audio=audio_bytes
        )
        return result.text if result else "Не удалось распознать речь."
    except Exception as e:
        if "402" in str(e):
            return "⚠️ Закончились кредиты Hugging Face для аудио."
        return f"❌ Ошибка при транскрипции: {e}"

# ---------- Обработчики ----------
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "🚀 Привет! Я бот **Fast Answer**.\n"
        "📝 Отправь мне текст, фото, аудио или документ — я дам быстрый ответ.\n"
        "💬 Я запоминаю историю диалога и автоматически сжимаю длинные разговоры для экономии контекста.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "Пустое сообщение...")
        return
    if user_text == '/start':
        return
    bot.send_chat_action(message.chat.id, "typing")
    chat_id = message.chat.id
    answer = generate_response_with_history(chat_id, user_text)
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
        bot.reply_to(message, f"❌ Ошибка: {e}")
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

        if extension == 'heic':
            jpeg_path = file_parser.parse_heic(tmp_path)
            if isinstance(jpeg_path, str) and jpeg_path.endswith('.jpg'):
                description = describe_image(jpeg_path)
                bot.reply_to(message, f"🖼️ (HEIC) {description}")
                os.unlink(jpeg_path)
            else:
                bot.reply_to(message, f"⚠️ Ошибка HEIC: {jpeg_path}")
            return

        extracted_text = file_parser.parse_file(tmp_path, extension)
        if not extracted_text:
            bot.reply_to(message, "📄 В документе нет текста для обработки.")
            return

        if len(extracted_text) > 3000:
            extracted_text = extracted_text[:3000] + "\n... (текст обрезан)"

        bot.send_chat_action(message.chat.id, "typing")
        # Используем историю для ответа по документу
        prompt = f"Содержание документа:\n{extracted_text}\n\nДай краткий ответ по этому содержанию."
        chat_id = message.chat.id
        answer = generate_response_with_history(chat_id, prompt)
        bot.reply_to(message, f"📄 Ответ:\n{answer}")

    except file_parser.ParseError as e:
        bot.reply_to(message, f"⚠️ Ошибка парсинга файла: {e}")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
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
        if transcript.startswith("⚠️") or transcript.startswith("❌"):
            bot.reply_to(message, transcript)
            return

        bot.reply_to(message, f"🎤 Распознано:\n{transcript}")

        # Генерируем ответ по транскрипции с историей
        chat_id = message.chat.id
        answer = generate_response_with_history(chat_id, f"Вопрос по аудио: {transcript}")
        bot.send_message(message.chat.id, f"💬 {answer}")

    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.reply_to(message, "🎬 Видео пока не обрабатываются, но скоро добавлю!")

@bot.message_handler(content_types=['sticker', 'contact', 'location', 'venue', 'animation', 'video_note'])
def handle_other(message):
    bot.reply_to(message, "Извините, я пока не умею обрабатывать этот тип контента.")

# ---------- Запуск ----------
if __name__ == '__main__':
    bot.remove_webhook()
    print("🚀 Бот Fast Answer запущен (Long Polling)...")
    print("🤖 Текст → Groq | 📸 / 🎤 → Hugging Face")
    print(f"📚 История хранится в памяти, сжатие при >{MAX_MESSAGES} сообщений или >{MAX_CONTENT_LENGTH} символов.")
    bot.infinity_polling()
