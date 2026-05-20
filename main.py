import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

import os
from flask import Flask
import threading
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_PATH = "currency_bot.db"
app = Flask(__name__)

@app.route('/')
def home():
    return "Trollcoin Bot is running! 🤖"

@app.route('/health')
def health():
    return "OK"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# Запускаем Flask в отдельном потоке
threading.Thread(target=run_flask, daemon=True).start()

logging.basicConfig(level=logging.INFO)
logging.getLogger('aiogram').setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                last_claim TEXT
            )
        ''')
        await db.commit()

async def register_user(user_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)',
            (user_id, username)
        )
        await db.commit()

async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT username, balance, last_claim FROM users WHERE user_id = ?', (user_id,))
        return await cursor.fetchone()

async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))        
        await db.commit()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    print(f"✅ /start от {message.from_user.id}")
    user_id = message.from_user.id
    print(f"1. user_id = {user_id}")
    username = message.from_user.username or f"user_{user_id}"
    print(f"2. username = {username}")
    await register_user(user_id, username)
    print(f"3. register_user выполнен")
    await message.answer(f"👋 Привет! Я тебя запомнил.")
    print(f"4. answer отправлен")
    
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    print(f"✅ /balance от {message.from_user.id}")
    data = await get_user_data(message.from_user.id)
    print(f"1. data = {data}")
    if not data:
        print("2. Пользователь не найден, регистрируем")
        await cmd_start(message)
        return
    _, balance, _ = data
    print(f"2. balance = {balance}")
    await message.answer(f"💰 Твой баланс: **{balance}** монет", parse_mode="Markdown")
    print(f"3. answer отправлен")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    print(f"✅ /claim от {message.from_user.id}")
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    print(f"1. data = {data}")
    if not data:
        await register_user(user_id, message.from_user.username or f"user_{user_id}")
        data = await get_user_data(user_id)

    _, balance, last_claim = data
    now = datetime.utcnow()
    print(f"2. balance = {balance}, last_claim = {last_claim}")
    
    if last_claim:
        last_time = datetime.fromisoformat(last_claim)
        if now - last_time < timedelta(hours=24):
            wait_time = timedelta(hours=24) - (now - last_time)
            hours, remainder = divmod(int(wait_time.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            await message.answer(f"⏳ Награда уже получена. Приходи через {hours}ч {minutes}м.")
            print(f"3. Награда уже получена")           
            return

    reward = 10
    await add_balance(user_id, reward)
    new_balance = (await get_user_data(user_id))[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_claim = ? WHERE user_id = ?", (now.isoformat(), user_id))
        await db.commit()
    await message.answer(f"🎁 Ты получил **{reward}** монет!\n💰 Новый баланс: **{new_balance}**", parse_mode="Markdown")
    print(f"3. Выдано {reward} монет, новый баланс: {new_balance}")

@dp.message(Command("give"))
async def cmd_give(message: Message):
    print(f"✅ /give от {message.from_user.id}")
    if message.from_user.id != ADMIN_ID:
        print("1. НЕ админ, отказано")
        await message.answer("🔒 Эта команда доступна только администратору.")
        return

    parts = message.text.split()
    print(f"1. parts = {parts}")
    if len(parts) != 3:
        await message.answer("📝 Использование: `/give <user_id> <сумма>`", parse_mode="Markdown")
        return

    try:
        target_id = int(parts[1])
        amount = int(parts[2])
        print(f"2. target_id = {target_id}, amount = {amount}")
        if amount <= 0:
            await message.answer("⛔ Сумма должна быть больше 0.")
            return

        await register_user(target_id, f"admin_given_{target_id}")
        await add_balance(target_id, amount)
        new_balance = (await get_user_data(target_id))[1]

        await message.answer(f"✅ Выдано **{amount}** монет пользователю `{target_id}`.\n💰 Его баланс: **{new_balance}**", parse_mode="Markdown")
        print(f"3. Выдано {amount} монет")

        try:
            await bot.send_message(
                target_id,
                f"🎁 Администратор выдал тебе **{amount}** монет.\n💰 Текущий баланс: **{new_balance}**",
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"4. Не удалось отправить уведомление: {e}")
            pass
    except ValueError:
        await message.answer("❌ Неверный формат. Используй числа для ID и суммы.")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    print(f"✅ /help от {message.from_user.id}")
    await message.answer(
        "📖 **Команды бота:**\n"
        "/start — Регистрация\n"
        "/balance — Проверить баланс\n"
        "/claim — Ежедневная награда (раз в 24ч)\n"
        "/give <id> <сумма> — Выдать валюту (только админ)",
        parse_mode="Markdown"
    )
    print(f"1. answer отправлен")
    
@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    print(f"✅ /reset от {message.from_user.id}")
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_claim = NULL WHERE user_id = ?", (user_id,))
        await db.commit()
    
    await message.answer("🔄 Время последней награды сброшено! Теперь можешь использовать `/claim`.")
    print(f"1. last_claim сброшен для {user_id}")

async def main():
    await init_db()
    logger.info("🤖 Бот запущен. Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())