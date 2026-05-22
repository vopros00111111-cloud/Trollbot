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

# ========== FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Trollcoin Bot is running! "

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
            [KeyboardButton(text="📦 Каталог"), KeyboardButton(text="❓ Помощь")]
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
                last_claim TEXT,
                is_admin INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                description TEXT,
                price INTEGER,
                image_url TEXT
            )
        ''')
        await db.commit()

async def register_user(user_id: int, username: str):
    clean_username = username.replace("@", "") if username else f"user_{user_id}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance, is_admin) VALUES (?, ?, 0, 0)',
            (user_id, clean_username)
        )
        await db.execute('UPDATE users SET username = ? WHERE user_id = ?', (clean_username, user_id))
        await db.commit()
    await db.execute('UPDATE users SET username = ? WHERE user_id = ?', (clean_username, user_id))
    await db.commit()

async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # Добавлено is_admin в запрос
        cursor = await db.execute('SELECT username, balance, last_claim, is_admin FROM users WHERE user_id = ?', (user_id,))
        return await cursor.fetchone()

async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        await db.commit()

async def check_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    data = await get_user_data(user_id)
    if data and len(data) >= 4:
        return data[3] == 1
    return False
# ========== КАТАЛОГ ФУНКЦИИ ==========
async def add_to_catalog(name: str, description: str, price: int, image_url: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO catalog (name, description, price, image_url) VALUES (?, ?, ?, ?)',
            (name, description, price, image_url)
        )
        await db.commit()

async def remove_from_catalog(item_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM catalog WHERE id = ?', (item_id,))
        await db.commit()

async def get_catalog():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, name, description, price, image_url FROM catalog')
        return await cursor.fetchall()

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

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:
        await cmd_start(message)
        return
    _, balance, _, _ = data
    await message.answer(f"💰 Твой баланс: **{balance}** монет", parse_mode="Markdown")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:        await register_user(user_id, message.from_user.username or f"user_{user_id}")
    data = await get_user_data(user_id)

    _, balance, last_claim, _ = data
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
    await message.answer(f"🎁 Ты получил **{reward}** монет!\n Новый баланс: **{new_balance}**", parse_mode="Markdown")

@dp.message(Command("help", "помощь"))
async def cmd_help(message: Message):
    await message.answer(
        " **Команды бота:**\n\n"
        "👤 **Основные:**\n"
        "/start — Регистрация\n"
        "/balance — Проверить баланс\n"
        "/claim — Ежедневная награда\n"
        "/transfer (/передать) @username <сумма> — Передать монеты\n\n"
        "📦 **Каталог:**\n"
        "/catalog — Показать товары\n"
        "👑 **Админ:**\n"
        "/givemoney @username <сумма> — Выдать монеты\n"
        "/takemoney @username <сумма> — Забрать монеты\n"
        "/additem Название|Описание|Цена|ссылка\n"
        "/removeitem <ID> — Удалить товар\n"
        "/addadmin @username — Назначить админа\n"
        "/removeadmin @username — Снять админа\n\n"
        "/help — Эта справка",
        parse_mode="Markdown"
    )

@dp.message(Command("transfer", "передать"))
async def cmd_transfer(message: Message):
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
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id FROM users WHERE username = ?', (target_username,))
            target_data = await cursor.fetchone()
        
        if not target_data:
            await message.answer(f"❌ Пользователь @{target_username} не найден.")
            return
        
        target_id = target_data[0]
        if target_id == sender_id:
            await message.answer(" Нельзя перевести самому себе.")
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, sender_id))
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target_id))
            await db.commit()
        
        new_balance = (await get_user_data(sender_id))[1]
        await message.answer(f"✅ Переведено **{amount}** монет @{target_username}\nТвой баланс: **{new_balance}**", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: `/transfer @username 100`")

# ========== КАТАЛОГ КОМАНДЫ ==========
@dp.message(Command("catalog", "каталог"))
async def cmd_catalog(message: Message):
    items = await get_catalog()
    if not items:
        return await message.answer("Каталог пуст")
    
    text = "КАТАЛОГ:\n\n"
    for item in items:
        name = item[1]
        price = item[3]
        text = text + name + " - " + str(price) + " монет\n"
    
    await message.answer(text)
@dp.message(Command("additem"))
async def cmd_additem(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer(" Только администратор")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("📝 Использование: `/additem Название|Описание|Цена|ссылка`", parse_mode="Markdown")
        return
    try:
        data = parts[1].split("|")
        if len(data) < 3:
            raise ValueError
        name = data[0].strip()
        description = data[1].strip()
        price = int(data[2].strip())
        image_url = data[3].strip() if len(data) > 3 else None
        await add_to_catalog(name, description, price, image_url)
        await message.answer(f"✅ Товар **{name}** добавлен в каталог!", parse_mode="Markdown")
    except (ValueError, IndexError):
        await message.answer("❌ Ошибка формата. Проверь данные!")

@dp.message(Command("removeitem"))
async def cmd_removeitem(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer("🔒 Только администратор")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("📝 Использование: `/removeitem <ID товара>`", parse_mode="Markdown")
        return
    try:
        item_id = int(parts[1])
        await remove_from_catalog(item_id)
        await message.answer(f"✅ Товар с ID {item_id} удалён!")
    except ValueError:
        await message.answer("❌ ID должен быть числом!")

@dp.message(Command("myitems"))
async def cmd_myitems(message: Message):
    items = await get_catalog()
    if not items:
        await message.answer("📦 Каталог пуст")
        text = "📦 **КАТАЛОГ (для админа):**\n\n"
        return text
    for item in items:
        item_id, name, desc, price, _ = item
        text += f"`{item_id}` — {name} ({price} монет)\n"
    text += "\nИспользуй `/removeitem <ID>` для удаления"
    await message.answer(text, parse_mode="Markdown")

# ========== АДМИН: ДЕНЬГИ (НОВОЕ) ==========
@dp.message(Command("givemoney"))
async def cmd_givemoney(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer("🔒 Только админ")
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("📝 /givemoney @username сумма")
        return
    try:
        target_name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            await message.answer("⛔ Сумма > 0")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username, balance FROM users WHERE username = ?', (target_name,))
            target = await cursor.fetchone()
        if not target:
            await message.answer(f"❌ @{target_name} не найден")
            return
        await add_balance(target[0], amount)
        new_bal = target[2] + amount
        await message.answer(f"✅ Выдано **{amount}** монет @{target[1]}\nБаланс: {new_bal}", parse_mode="Markdown")
        try:
            await bot.send_message(target[0], f"🎁 Админ выдал **{amount}** монет!\nБаланс: **{new_bal}**", parse_mode="Markdown")
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Ошибка формата")

@dp.message(Command("takemoney", "removemoney"))
async def cmd_takemoney(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer("🔒 Только админ")
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("📝 /takemoney @username сумма")
        return
    try:
        target_name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            await message.answer("⛔ Сумма > 0")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username, balance FROM users WHERE username = ?', (target_name,))
            target = await cursor.fetchone()
        if not target:
            await message.answer(f"❌ @{target_name} не найден")
            return
        if target[2] < amount:
            await message.answer(f"❌ Мало денег ({target[2]})")
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, target[0]))
            await db.commit()
        new_bal = target[2] - amount
        await message.answer(f"✅ Списано **{amount}** монет у @{target[1]}\nБаланс: {new_bal}", parse_mode="Markdown")
        try:
            await bot.send_message(target[0], f"⚠️ Админ списал **{amount}** монет!\nБаланс: **{new_bal}**", parse_mode="Markdown")
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Ошибка формата")

# ========== АДМИНКИ ==========
@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    await message.answer(f"DEBUG: Твой ID: {message.from_user.id}")
    
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("📝 /addadmin @username")
    
    try:
        target_input = parts[1].replace("@", "")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username FROM users WHERE username = ?', (target_input,))
            target_data = await cursor.fetchone()
        
        if not target_data:
            return await message.answer(f"❌ @{target_input} не найден")
        
        target_id, target_username = target_data
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET is_admin = 1 WHERE user_id = ?', (target_id,))
            await db.commit()
        
        await message.answer(f"✅ @{target_username} — админ!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await check_admin(message.from_user.id):
        await message.answer("🔒 Только администратор")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("📝 Использование: `/removeadmin @username`", parse_mode="Markdown")
        return
    try:
        target_input = parts[1].replace("@", "")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username FROM users WHERE username = ?', (target_input,))
            target_data = await cursor.fetchone()
        if not target_data:
            await message.answer(f" Пользователь @{target_input} не найден.")
            return
        target_id, target_username = target_data
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET is_admin = 0 WHERE user_id = ?', (target_id,))
            await db.commit()
        await message.answer(f"✅ **@{target_username}** снят с должности администратора.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== КНОПКИ ==========
@dp.message(F.text == "💰 Баланс")
async def btn_balance(message: Message):
    await cmd_balance(message)
@dp.message(F.text == "🎁 Награда")
async def btn_claim(message: Message):
    await cmd_claim(message)

@dp.message(F.text == "📦 Каталог")
async def btn_catalog(message: Message):
    await cmd_catalog(message)

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
