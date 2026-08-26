import os
import telebot
import tempfile
from groq import Groq
import file_parser
import traceback
from collections import defaultdict

# ---------- Переменные окружения ----------
TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не задан!")

# ---------- Инициализация клиентов ----------
bot = telebot.TeleBot(TOKEN)
groq_client = Groq(api_key=GROQ_API_KEY)

# ---------- Хранилище истории ----------
conversation_histories = defaultdict(list)
MAX_MESSAGES = 12
MAX_CONTENT_LENGTH = 2000

# ---------- Функции истории ----------
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
                {"role": "system", "content": "Ты — полезный ассистент. Сделай краткую, но информативную суммаризацию следующего диалога. Выдели основные темы, запросы пользователя и ключевые ответы."},
                {"role": "user", "content": f"Диалог:\n{dialog_text}"}
            ],
            model="gemma2-9b-it",
            max_tokens=500,
            temperature=0.5,
        )
        summary = completion.choices[0].message.content
        conversation_histories[chat_id] = [{"role": "assistant", "content": f"Краткое содержание предыдущего диалога:\n{summary}"}]
    except Exception as e:
        print(f"Ошибка сжатия истории: {e}")
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
    messages = history.copy()
    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="gemma2-9b-it",
            max_tokens=500,
            temperature=0.7,
        )
        assistant_reply = completion.choices[0].message.content
    except Exception as e:
        assistant_reply = f"❌ Ошибка: {e}"
    history.append({"role": "assistant", "content": assistant_reply})
    conversation_histories[chat_id] = history
    return assistant_reply

# ---------- Обработчики ----------
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id,
        "🚀 Привет! Я бот **Fast Answer**.\n"
        "📝 Отправь мне текст, фото, аудио или документ — я дам быстрый ответ.\n"
        "💬 Я запоминаю историю диалога и автоматически сжимаю длинные разговоры.")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text or user_text == '/start':
        return
    bot.send_chat_action(message.chat.id, "typing")
    answer = generate_response_with_history(message.chat.id, user_text)
    bot.reply_to(message, answer)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    # Заглушка — пока только уведомление
    bot.reply_to(message, "📸 Обработка фото временно отключена для упрощения.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    bot.reply_to(message, "📄 Обработка документов временно отключена для упрощения.")

@bot.message_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    bot.reply_to(message, "🎤 Обработка аудио временно отключена для упрощения.")

@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.reply_to(message, "🎬 Видео пока не обрабатываются.")

@bot.message_handler(content_types=['sticker', 'contact', 'location', 'venue', 'animation', 'video_note'])
def handle_other(message):
    bot.reply_to(message, "Извините, я пока не умею обрабатывать этот тип контента.")

# ---------- Запуск ----------
if __name__ == '__main__':
    bot.remove_webhook()
    print("🚀 Бот Fast Answer запущен (Long Polling)...")
    print("🤖 Текст → Groq | История сохраняется и сжимается автоматически.")
    bot.infinity_polling()
