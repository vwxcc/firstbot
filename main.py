import os
import telebot
from flask import Flask, request
import traceback
import json

TOKEN = os.environ.get("BOT_TOKEN")
print(f"TOKEN loaded: {'YES' if TOKEN else 'NO'} (token: {TOKEN[:5]}...)")  # покажем первые 5 символов для проверки

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
        print("RAW JSON:", json_str)  # выводим сырой JSON
        
        update = telebot.types.Update.de_json(json_str)
        print("PARSED UPDATE:", update)  # выводим распаршенный объект
        
        # Проверяем, есть ли сообщение
        if update.message:
            print("Message received from user:", update.message.text)
        else:
            print("No message in update, update type:", update)
        
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
