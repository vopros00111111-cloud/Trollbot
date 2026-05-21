import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from flask import Flask
import threading

# ========== FLASK ДЛЯ RENDER ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Trollcoin Bot is running! 🤖"

@app.route('/health')
def health():
    return "OK"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_PATH = "currency_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ========== КНОПКИ ==========
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🎁 Награда")],
            [KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )
    return keyboard
# ========== БАЗА ДАННЫХ ==========
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
        clean_username = username.replace("@", "") if username else f"user_{user_id}"
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)',
            (user_id, clean_username)
        )
        await db.execute('UPDATE users SET username = ? WHERE user_id = ?', (clean_username, user_id))
        await db.commit()

async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT username, balance, last_claim FROM users WHERE user_id = ?', (user_id,))
        return await cursor.fetchone()

async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        await db.commit()

# ========== КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    await register_user(user_id, username)
    
    await message.answer(
        f"👋 Привет! Я Trollcoin Bot.\n\n"
        f"Используй кнопки внизу или команды:\n"
        f"/balance — баланс\n"
        f"/claim — награда\n"
        f"/help — помощь",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("balance"))async def cmd_balance(message: Message):
    data = await get_user_data(message.from_user.id)
    if not data:
        await cmd_start(message)
        return
    _, balance, _ = data
    await message.answer(f"💰 Твой баланс: **{balance}** монет", parse_mode="Markdown")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, message.from_user.username or f"user_{user_id}")
        data = await get_user_data(user_id)

    _, balance, last_claim = data
    now = datetime.utcnow()
    
    if last_claim:
        last_time = datetime.fromisoformat(last_claim)
        if now - last_time < timedelta(hours=24):
            wait_time = timedelta(hours=24) - (now - last_time)
            hours, remainder = divmod(int(wait_time.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            await message.answer(f"⏳ Награда уже получена. Приходи через {hours}ч {minutes}м.")
            return

    reward = 10
    await add_balance(user_id, reward)
    new_balance = (await get_user_data(user_id))[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_claim = ? WHERE user_id = ?", (now.isoformat(), user_id))
        await db.commit()
    await message.answer(f"🎁 Ты получил **{reward}** монет!\n💰 Новый баланс: **{new_balance}**", parse_mode="Markdown")

@dp.message(Command("help", "помощь"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 **Команды бота:**\n\n"
        "/start — Регистрация\n"
        "/balance — Проверить баланс\n"
        "/claim — Ежедневная награда (10 монет раз в 24ч)\n"
        "/transfer @username <сумма> — Передать монеты\n"
        "/help — Эта справка\n\n"
        "Или используй кнопки внизу 👇",
        parse_mode="Markdown"
    )

@dp.message(Command("transfer"))async def cmd_transfer(message: Message):
    parts = message.text.split()
    
    if len(parts) != 3:
        await message.answer("📝 Использование: `/transfer @username <сумма>`", parse_mode="Markdown")
        return
    
    try:
        target_username = parts[1].replace("@", "")
        amount = int(parts[2])
        
        if amount <= 0:
            await message.answer("⛔ Сумма должна быть больше 0.")
            return
        
        sender_id = message.from_user.id
        sender_data = await get_user_data(sender_id)
        
        if not sender_data:
            await message.answer("❌ Сначала используй /start")
            return
        
        if sender_data[1] < amount:
            await message.answer(f"❌ Недостаточно монет. У тебя {sender_data[1]}, нужно {amount}")
            return
        
        # Ищем получателя
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id FROM users WHERE username = ?', (target_username,))
            target_data = await cursor.fetchone()
        
        if not target_data:
            await message.answer(f"❌ Пользователь @{target_username} не найден.")
            return
        
        target_id = target_data[0]
        
        if target_id == sender_id:
            await message.answer("❌ Нельзя перевести самому себе.")
            return
        
        # Перевод
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, sender_id))
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target_id))
            await db.commit()
        
        new_balance = (await get_user_data(sender_id))[1]
        await message.answer(f"✅ Переведено **{amount}** монет @{target_username}\nТвой баланс: **{new_balance}**", parse_mode="Markdown")
            except ValueError:
        await message.answer("❌ Неверный формат. Пример: `/transfer @username 100`")

# ========== КНОПКИ ==========
@dp.message(F.text == "💰 Баланс")
async def btn_balance(message: Message):
    await cmd_balance(message)

@dp.message(F.text == "🎁 Награда")
async def btn_claim(message: Message):
    await cmd_claim(message)

@dp.message(F.text == "❓ Помощь")
async def btn_help(message: Message):
    await cmd_help(message)

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    logger.info("🤖 Бот запущен. Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())