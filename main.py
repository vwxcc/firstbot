import os
import telebot
import time

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set in environment!")

bot = telebot.TeleBot(TOKEN)

# Обработчик команды /start
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Привет! Я бот на Render (Long Polling)! 🚀")

# Обработчик любого другого текста
@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.reply_to(message, f"Я получил: {message.text}")

print("Бот запущен, режим Long Polling...")
# Бесконечный цикл опроса
bot.infinity_polling()
