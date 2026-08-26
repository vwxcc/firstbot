import os
import telebot

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set in environment!")

bot = telebot.TeleBot(TOKEN)

# Единый обработчик для всех текстовых сообщений
@bot.message_handler(content_types=['text'])
def handle_all_messages(message):
    text = message.text.strip()  # убираем лишние пробелы и переводы строк
    
    if text == '/start':
        bot.send_message(message.chat.id, "Привет! Я бот на Render (Long Polling)! 🚀")
    else:
        # На любое другое сообщение отвечаем эхом
        bot.reply_to(message, f"Я получил: {text}")

# Удаляем вебхук на случай, если он ещё активен
bot.remove_webhook()

print("Бот запущен, режим Long Polling...")
bot.infinity_polling()
