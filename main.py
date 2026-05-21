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

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DB_PATH = "currency_bot.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🎁 Награда")],
            [KeyboardButton(text="📦 Каталог"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True
    )
    return keyboard

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,                username TEXT,
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
    async with aiosqlite.connect(DB_PATH) as db:
        clean_username = username.replace("@", "") if username else f"user_{user_id}"
        await db.execute(
            'INSERT OR IGNORE INTO users (user_id, username, balance, is_admin) VALUES (?, ?, 0, 0)',
            (user_id, clean_username)
        )
        await db.execute('UPDATE users SET username = ? WHERE user_id = ?', (clean_username, user_id))
        await db.commit()

async def get_user_data(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
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

async def add_to_catalog(name, desc, price, img=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT INTO catalog (name, description, price, image_url) VALUES (?, ?, ?, ?)', (name, desc, price, img))
        await db.commit()
async def remove_from_catalog(item_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('DELETE FROM catalog WHERE id = ?', (item_id,))
        await db.commit()

async def get_catalog():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT id, name, description, price, image_url FROM catalog')
        return await cursor.fetchall()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(message.from_user.id, message.from_user.username or f"user_{message.from_user.id}")
    await message.answer(
        f"👋 Привет! Я Trollcoin Bot.\nИспользуй кнопки или команды:\n/balance, /claim, /help",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    data = await get_user_data(message.from_user.id)
    if not data:
        return await cmd_start(message)
    await message.answer(f"💰 Твой баланс: **{data[1]}** монет", parse_mode="Markdown")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, message.from_user.username or f"user_{user_id}")
        data = await get_user_data(user_id)
    
    _, balance, last_claim, _ = data
    now = datetime.utcnow()
    
    if last_claim:
        last_time = datetime.fromisoformat(last_claim)
        if now - last_time < timedelta(hours=24):
            wait = timedelta(hours=24) - (now - last_time)
            h, rem = divmod(int(wait.total_seconds()), 3600)
            m, _ = divmod(rem, 60)
            return await message.answer(f"⏳ Награда уже получена. Приходи через {h}ч {m}м.")

    await add_balance(user_id, 10)
    new_bal = (await get_user_data(user_id))[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_claim = ? WHERE user_id = ?", (now.isoformat(), user_id))
        await db.commit()
    await message.answer(f"🎁 Ты получил **10** монет!\n💰 Новый баланс: **{new_bal}**", parse_mode="Markdown")
@dp.message(Command("help", "помощь"))
async def cmd_help(message: Message):
    text = "📖 **Команды:**\n"
    text += "/start — Регистрация\n"
    text += "/balance — Баланс\n"
    text += "/claim — Награда\n"
    text += "/transfer @юзер сумма — Перевод\n"
    text += "/catalog — Товары\n\n"
    text += "👑 **Админ:**\n"
    text += "/givemoney @юзер сумма\n"
    text += "/takemoney @юзер сумма\n"
    text += "/additem Имя|Описание|Цена|Фото\n"
    text += "/removeitem ID\n"
    text += "/addadmin @юзер\n"
    text += "/removeadmin @юзер"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("transfer"))
async def cmd_transfer(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("📝 /transfer @username сумма")
    try:
        target_name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return await message.answer("⛔ Сумма должна быть больше 0")
        
        sender_id = message.from_user.id
        sender_data = await get_user_data(sender_id)
        if not sender_data:
            return await message.answer("❌ Сначала /start")
        if sender_data[1] < amount:
            return await message.answer(f"❌ Мало денег. У тебя {sender_data[1]}")
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id FROM users WHERE username = ?', (target_name,))
            target = await cursor.fetchone()
        
        if not target:
            return await message.answer(f"❌ Юзер @{target_name} не найден")
        if target[0] == sender_id:
            return await message.answer("❌ Себе нельзя")
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, sender_id))
            await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target[0]))
            await db.commit()
                new_bal = (await get_user_data(sender_id))[1]
        await message.answer(f"✅ Переведено **{amount}** монет @{target_name}\nБаланс: **{new_bal}**", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Ошибка формата")

@dp.message(Command("catalog", "каталог"))
async def cmd_catalog(message: Message):
    items = await get_catalog()
    if not items:
        return await message.answer("📦 Каталог пуст")
    text = "📦 **КАТАЛОГ:**\n\n"
    for item in items:
        text += f"🔹 **{item[1]}**\n"
        text += f"   {item[2]}\n"
        text += f"   💰 Цена: {item[3]} монет\n\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("additem"))
async def cmd_additem(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("📝 /additem Имя|Описание|Цена|Ссылка")
    try:
        d = parts[1].split("|")
        if len(d) < 3:
            raise ValueError
        name = d[0].strip()
        desc = d[1].strip()
        price = int(d[2].strip())
        img = d[3].strip() if len(d) > 3 else None
        await add_to_catalog(name, desc, price, img)
        await message.answer(f"✅ Товар **{name}** добавлен!", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Ошибка. Формат: Имя|Описание|Цена|Ссылка")

@dp.message(Command("removeitem"))
async def cmd_removeitem(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("📝 /removeitem ID")
    try:
        await remove_from_catalog(int(parts[1]))
        await message.answer("✅ Товар удалён!")
    except Exception:
        await message.answer("❌ Ошибка ID")
@dp.message(Command("myitems"))
async def cmd_myitems(message: Message):
    items = await get_catalog()
    if not items:
        return await message.answer("📦 Каталог пуст")
    text = "📦 **Товары (ID):**\n"
    for item in items:
        text += f"`{item[0]}` — {item[1]} ({item[3]} монет)\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("givemoney"))
async def cmd_givemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("📝 /givemoney @username сумма")
    try:
        target_name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return await message.answer("⛔ Сумма должна быть больше 0")
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username, balance FROM users WHERE username = ?', (target_name,))
            target = await cursor.fetchone()
        
        if not target:
            return await message.answer(f"❌ @{target_name} не найден")
        
        await add_balance(target[0], amount)
        new_bal = target[2] + amount
        await message.answer(f"✅ Выдано **{amount}** монет @{target[1]}\nБаланс: {new_bal}", parse_mode="Markdown")
        
        try:
            await bot.send_message(
                target[0],
                f"🎁 Админ выдал **{amount}** монет!\nБаланс: **{new_bal}**",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Ошибка формата")

@dp.message(Command("takemoney", "removemoney"))
async def cmd_takemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()    if len(parts) != 3:
        return await message.answer("📝 /takemoney @username сумма")
    try:
        target_name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return await message.answer("⛔ Сумма должна быть больше 0")
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username, balance FROM users WHERE username = ?', (target_name,))
            target = await cursor.fetchone()
        
        if not target:
            return await message.answer(f"❌ @{target_name} не найден")
        if target[2] < amount:
            return await message.answer(f"❌ У юзера мало денег ({target[2]})")
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amount, target[0]))
            await db.commit()
        
        new_bal = target[2] - amount
        await message.answer(f"✅ Списано **{amount}** монет у @{target[1]}\nБаланс: {new_bal}", parse_mode="Markdown")
        
        try:
            await bot.send_message(
                target[0],
                f"⚠️ Админ списал **{amount}** монет!\nБаланс: **{new_bal}**",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Ошибка формата")

@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("📝 /addadmin @username")
    try:
        name = parts[1].replace("@", "")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username FROM users WHERE username = ?', (name,))
            target = await cursor.fetchone()
        if not target:
            return await message.answer(f"❌ @{name} не найден")
        async with aiosqlite.connect(DB_PATH) as db:            await db.execute('UPDATE users SET is_admin = 1 WHERE user_id = ?', (target[0],))
            await db.commit()
        await message.answer(f"✅ **@{target[1]}** теперь админ!", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Ошибка")

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("📝 /removeadmin @username")
    try:
        name = parts[1].replace("@", "")
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('SELECT user_id, username FROM users WHERE username = ?', (name,))
            target = await cursor.fetchone()
        if not target:
            return await message.answer(f"❌ @{name} не найден")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute('UPDATE users SET is_admin = 0 WHERE user_id = ?', (target[0],))
            await db.commit()
        await message.answer(f"✅ **@{target[1]}** снят с админки!", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ Ошибка")

@dp.message(F.text == "💰 Баланс")
async def btn_balance(m: Message):
    await cmd_balance(m)

@dp.message(F.text == "🎁 Награда")
async def btn_claim(m: Message):
    await cmd_claim(m)

@dp.message(F.text == "📦 Каталог")
async def btn_catalog(m: Message):
    await cmd_catalog(m)

@dp.message(F.text == "❓ Помощь")
async def btn_help(m: Message):
    await cmd_help(m)

async def main():
    await init_db()
    logger.info("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())