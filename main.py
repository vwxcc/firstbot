import os
import telebot
import tempfile
import traceback
import time
import threading
import requests
from groq import Groq
import file_parser
from collections import defaultdict
from flask import Flask, request

# ---------- Переменные окружения ----------
TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")  # опционально

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не задан")

# ---------- Инициализация клиентов ----------
bot = telebot.TeleBot(TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)
app = Flask(__name__)

# Инициализация Gemini, если ключ есть
gemini_model = None
if GOOGLE_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GOOGLE_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        print("Gemini инициализирован для фото")
    except Exception as e:
        print(f"Ошибка инициализации Gemini: {e}")
else:
    print("GOOGLE_API_KEY не задан, функция описания фото отключена")

# ---------- Хранилище истории ----------
conversation_histories = defaultdict(list)
MAX_MESSAGES = 12
MAX_CONTENT_LENGTH = 2000

# ---------- Системный промпт ----------
SYSTEM_PROMPT = "Ты — полезный помощник. Отвечай всегда на русском языке. Будь кратким и по делу, без лишней воды."

def get_history(chat_id):
    return conversation_histories[chat_id]

def compress_history(chat_id):
    history = conversation_histories[chat_id]
    if not history:
        return
    dialog_text = "\n".join([f"{m['role']}: {m['content']}" for m in history])
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT + " Сделай краткую суммаризацию диалога, выдели ключевые темы и важные детали."},
                {"role": "user", "content": f"Диалог:\n{dialog_text}"}
            ],
            model="llama-3.1-8b-instant",
            max_tokens=500,
            temperature=0.5,
        )
        summary = completion.choices[0].message.content
        conversation_histories[chat_id] = [
            {"role": "assistant", "content": f"Краткое содержание предыдущего диалога:\n{summary}"}
        ]
    except Exception as e:
        print(f"Ошибка сжатия: {e}")
        if len(history) > 6:
            conversation_histories[chat_id] = history[-6:]

def add_to_history(chat_id, role, content):
    history = conversation_histories[chat_id]
    history.append({"role": role, "content": content})
    if len(history) > MAX_MESSAGES or sum(len(m["content"]) for m in history) > MAX_CONTENT_LENGTH:
        compress_history(chat_id)

def generate_response_with_history(chat_id, user_message):
    history = get_history(chat_id)
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_MESSAGES or sum(len(m["content"]) for m in history) > MAX_CONTENT_LENGTH:
        compress_history(chat_id)
        history = get_history(chat_id)
        history.append({"role": "user", "content": user_message})

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history.copy()
    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            max_tokens=500,
            temperature=0.7,
        )
        assistant_reply = completion.choices[0].message.content
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        assistant_reply = None

    if assistant_reply is None:
        assistant_reply = "Произошла ошибка при обработке запроса. Попробуйте позже."
        history.pop()
        return assistant_reply

    history.append({"role": "assistant", "content": assistant_reply})
    conversation_histories[chat_id] = history
    return assistant_reply

# ---------- Функция описания фото через Gemini ----------
def describe_image(image_path: str) -> str:
    if gemini_model is None:
        return "Функция описания фото временно недоступна (не задан ключ Google API)."
    try:
        with open(image_path, "rb") as f:
            image_data = f.read()
        response = gemini_model.generate_content(
            ["Опиши кратко, что изображено на этом фото, на русском языке.", {"mime_type": "image/jpeg", "data": image_data}]
        )
        return response.text if response.text else "Не удалось описать изображение."
    except Exception as e:
        print(f"Ошибка описания фото: {e}")
        return "Произошла ошибка при обработке фото. Попробуйте позже."

# ---------- Функция транскрипции аудио через Groq Whisper ----------
def transcribe_audio(audio_path: str) -> str:
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        files = {"file": (os.path.basename(audio_path), audio_bytes, "audio/mpeg")}
        data = {"model": "whisper-large-v3", "response_format": "text"}
        response = requests.post(url, headers=headers, files=files, data=data)
        if response.status_code == 200:
            return response.text.strip()
        else:
            print(f"Ошибка транскрипции: {response.text}")
            return "Не удалось распознать аудио."
    except Exception as e:
        print(f"Ошибка транскрипции: {e}")
        return "Произошла ошибка при обработке аудио."

# ---------- Обработчики Telegram ----------
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "Привет! Я бот Fast Answer.\n"
        "Отправьте текст, фото, аудио или документ.\n"
        "Текст обрабатывается через Groq, фото через Google Gemini (если ключ задан), аудио через Whisper.\n"
        "Я запоминаю историю диалога и автоматически сжимаю длинные разговоры.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text or user_text == '/start':
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
        bot.reply_to(message, description)
    except Exception as e:
        print(f"Ошибка обработки фото: {e}")
        bot.reply_to(message, "Произошла ошибка при обработке фото.")
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
                bot.reply_to(message, description)
                os.unlink(jpeg_path)
            else:
                bot.reply_to(message, "Не удалось обработать HEIC-файл.")
            return

        extracted_text = file_parser.parse_file(tmp_path, extension)
        if not extracted_text:
            bot.reply_to(message, "В документе нет текста для обработки.")
            return
        if len(extracted_text) > 3000:
            extracted_text = extracted_text[:3000] + "\n... (текст обрезан)"

        bot.send_chat_action(message.chat.id, "typing")
        prompt = f"Содержание документа:\n{extracted_text}\n\nДай краткий ответ по этому содержанию."
        chat_id = message.chat.id
        answer = generate_response_with_history(chat_id, prompt)
        bot.reply_to(message, answer)

    except file_parser.ParseError as e:
        bot.reply_to(message, f"Ошибка парсинга файла: {e}")
    except Exception as e:
        print(f"Ошибка обработки документа: {e}")
        bot.reply_to(message, "Произошла ошибка при обработке документа.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@bot.message_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    tmp_path = None
    try:
        if message.content_type == 'voice':
            file_id = message.voice.file_id
            ext = '.ogg'
        else:
            file_id = message.audio.file_id
            ext = '.mp3'
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(downloaded_file)
            tmp_path = tmp.name

        bot.send_chat_action(message.chat.id, "typing")
        transcript = transcribe_audio(tmp_path)
        if "ошибка" in transcript.lower() or "не удалось" in transcript.lower():
            bot.reply_to(message, transcript)
            return
        bot.reply_to(message, f"Распознанный текст:\n{transcript}")
        chat_id = message.chat.id
        answer = generate_response_with_history(chat_id, f"Вопрос по аудио: {transcript}")
        bot.send_message(message.chat.id, answer)

    except Exception as e:
        print(f"Ошибка обработки аудио: {e}")
        bot.reply_to(message, "Произошла ошибка при обработке аудио.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.reply_to(message, "Видео пока не поддерживаются.")

@bot.message_handler(content_types=['sticker', 'contact', 'location', 'venue', 'animation', 'video_note'])
def handle_other(message):
    bot.reply_to(message, "Извините, я не умею обрабатывать этот тип контента.")

# ---------- HTTP-сервер для поддержки порта ----------
@app.route('/')
def health():
    return "OK", 200

@app.route('/health')
def health_check():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ---------- Запуск бота с ручным управлением polling ----------
def start_bot():
    # Принудительно удаляем вебхук через API
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook")
        print("deleteWebhook response:", r.json())
    except Exception as e:
        print(f"Ошибка удаления вебхука: {e}")

    time.sleep(3)

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("HTTP-сервер для проверки порта запущен")

    print("Бот Fast Answer запущен (Long Polling)...")
    print("Текст -> Groq, Фото -> " + ("Gemini" if gemini_model else "отключено") + ", Аудио -> Groq Whisper")

    # Основной цикл с ручным управлением get_updates
    offset = None
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=30)
            if updates:
                for update in updates:
                    # Обновляем offset, чтобы не получать одно и то же
                    offset = update.update_id + 1
                    # Обрабатываем обновление
                    bot.process_new_updates([update])
            # Если обновлений нет, просто продолжаем цикл
        except Exception as e:
            print(f"Ошибка в цикле получения обновлений: {e}")
            if "409" in str(e):
                print("Конфликт (409), удаляем вебхук и ждём 30 секунд...")
                try:
                    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook")
                except:
                    pass
                time.sleep(30)
                offset = None  # сброс offset для перезапуска
            else:
                print("Неизвестная ошибка, ждём 10 секунд...")
                time.sleep(10)

if __name__ == '__main__':
    start_bot()
