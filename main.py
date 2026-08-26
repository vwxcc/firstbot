import os
import telebot
from flask import Flask, request
import traceback

TOKEN = os.environ.get("BOT_TOKEN")
print(f"TOKEN loaded: {'YES' if TOKEN else 'NO'}")  # Проверка, что токен загружен

if not TOKEN:
    raise ValueError("BOT_TOKEN not set in environment!")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Привет! Я бот на Render! 🚀")

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        json_str = request.stream.read().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        print("ERROR in webhook:")
        print(traceback.format_exc())
        return "ERROR", 500

@app.route('/')
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
