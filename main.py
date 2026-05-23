import asyncio
import logging
import os
from datetime import datetime, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Trollcoin Bot is running!"

@app.route('/health')
def health():
    return "OK"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальный пул соединений
pool = None

def get_main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="🎁 Награда")],
        [KeyboardButton(text="📦 Каталог"), KeyboardButton(text="❓ Помощь")]
    ], resize_keyboard=True)

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                last_claim TEXT,
                is_admin INTEGER DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS catalog (
                id SERIAL PRIMARY KEY,
                name TEXT,
                description TEXT,
                price INTEGER,
                image_url TEXT
            )
        ''')

async def register_user(user_id: int, username: str):
    clean_username = username.replace("@", "") if username else f"user_{user_id}"
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO users (user_id, username, balance, is_admin) VALUES ($1, $2, 0, 0) ON CONFLICT (user_id) DO NOTHING',
            user_id, clean_username
        )
        await conn.execute('UPDATE users SET username = $1 WHERE user_id = $2', clean_username, user_id)

async def get_user_data(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow('SELECT username, balance, last_claim, is_admin FROM users WHERE user_id = $1', user_id)

async def add_balance(user_id: int, amount: int):
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, user_id)

async def check_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    data = await get_user_data(user_id)
    return data['is_admin'] == 1 if data else False

async def add_to_catalog(name: str, description: str, price: int, image_url: str = None):
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO catalog (name, description, price, image_url) VALUES ($1, $2, $3, $4)',
            name, description, price, image_url
        )

async def remove_from_catalog(item_id: int):
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM catalog WHERE id = $1', item_id)
async def get_catalog():
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT id, name, description, price, image_url FROM catalog')

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    await register_user(user_id, username)
    text = "👋 Привет! Я 𝗕𝗹𝗲𝘀𝘀𝗖𝗼𝗶𝗻 Bot.\n\n"
    text += "📋 **Доступные команды:**\n"
    text += "/balance — проверить баланс\n"
    text += "/claim — ежедневная награда\n"
    text += "/transfer — перевести монеты\n"
    text += "/catalog — магазин товаров\n"
    text += "/help — полная справка"
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")
    
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    parts = message.text.split()
    
    # Если указали юзернейм другого человека
    if len(parts) > 1:
        target_username = parts[1].replace("@", "")
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT username, balance FROM users WHERE username = $1", target_username)
        
        if not row:
            return await message.answer(f"❌ Пользователь @{target_username} не найден.")
        
        await message.answer(f"💰 Баланс **@{row['username']}**: **{row['balance']}** монет", parse_mode="Markdown")
    else:
        # Проверка своего баланса
        data = await get_user_data(message.from_user.id)
        if not data:
            return await cmd_start(message)
        await message.answer(f"💰 Твой баланс: **{data['balance']}** монет", parse_mode="Markdown")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, message.from_user.username or f"user_{user_id}")
        data = await get_user_data(user_id)
    
    now = datetime.utcnow()
    if data['last_claim']:
        last_time = datetime.fromisoformat(data['last_claim'])
        if now - last_time < timedelta(hours=24):
            wait = timedelta(hours=24) - (now - last_time)
            h, rem = divmod(int(wait.total_seconds()), 3600)
            m, _ = divmod(rem, 60)
            return await message.answer(f"⏳ Жди {h}ч {m}м")
    
    await add_balance(user_id, 10)
    new_data = await get_user_data(user_id)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_claim = $1 WHERE user_id = $2", now.isoformat(), user_id)
    await message.answer(f"🎁 +10 монет!\nБаланс: **{new_data['balance']}**", parse_mode="Markdown")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = "📖 **СПРАВОЧНИК КОМАНД**\n\n"
    text += "👤 **Основные:**\n"
    text += "/start — начать работу\n"
    text += "/balance — баланс\n"
    text += "/claim — награда (раз в 24ч)\n"
    text += "/transfer @user сумма — перевод\n\n"
    text += "📦 **Каталог:**\n"
    text += "/catalog — товары\n"
    text += "👑 **Админ:**\n"
    text += "/givemoney @user сумма — добавление монет пользователю\n"
    text += "/takemoney @user сумма — удаление монет у пользователя\n"
    text += "/additem Название|Описание|Цена — добавление товара\n"
    text += "/removeitem ID — удаление товара\n"
    text += "/addadmin @user —  назначить админа\n"
    text += "/removeadmin @user — снять админа"
    await message.answer(text, parse_mode="Markdown")
@dp.message(Command("transfer"))
async def cmd_transfer(message: Message):
    parts = message.text.split()
    if len(parts) != 3:        return await message.answer("/transfer @user сумма")
    try:
        target = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return await message.answer("Сумма > 0")
        sender = message.from_user.id
        sender_data = await get_user_data(sender)
        if not sender_data or sender_data['balance'] < amount:
            return await message.answer("Недостаточно средств")
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id FROM users WHERE username = $1', target)
            if not t or t['user_id'] == sender:
                return await message.answer("Ошибка")
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, sender)
            await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, t['user_id'])
        await message.answer(f"✅ {amount} монет переведено")
    except:
        await message.answer("Ошибка")

@dp.message(Command("catalog"))
async def cmd_catalog(message: Message):
    items = await get_catalog()
    if not items:
        return await message.answer("Пусто")
    text = "📦 Каталог:\n"
    for i in items:
        text += f"{i['name']} - {i['price']} монет\n"
    await message.answer(text)

@dp.message(Command("additem"))
async def cmd_additem(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("📝 /additem Название|Описание|Цена|ссылка")
    
    try:
        d = parts[1].split("|")
        if len(d) < 3:
            return await message.answer("❌ Формат: Название|Описание|Цена")
        
        name = d[0].strip()
        description = d[1].strip()
        price = int(d[2].strip())
        image_url = d[3].strip() if len(d) > 3 else None
        
        await add_to_catalog(name, description, price, image_url)
        await message.answer(f"✅ Товар **{name}** добавлен!", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
@dp.message(Command("removeitem"))
async def cmd_removeitem(message: Message):
    if not await check_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer(" /removeitem ID")
    try:
        await remove_from_catalog(int(parts[1]))
        await message.answer("✅ Удалено")
    except:
        pass

@dp.message(Command("givemoney"))
async def cmd_givemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/givemoney @user сумма")
    try:
        name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', name)
        if not t:
            return await message.answer("Не найден")
        await add_balance(t['user_id'], amount)
        await message.answer(f"✅ Выдано {amount} монет @{t['username']}")
    except:
        await message.answer("Ошибка")

@dp.message(Command("takemoney"))
async def cmd_takemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/takemoney @user сумма")
    try:
        name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0:
            return
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', name)
        if not t or t['balance'] < amount:
            return await message.answer("Ошибка")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, t['user_id'])
        await message.answer(f"✅ Списано {amount} монет")
    except:
        await message.answer("Ошибка")

@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("/addadmin @username")
    try:
        target_input = parts[1].replace("@", "")
        async with pool.acquire() as conn:
            target_data = await conn.fetchrow('SELECT user_id, username FROM users WHERE username = $1', target_input)
        if not target_data:
            return await message.answer(f"❌ @{target_input} не найден")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_admin = 1 WHERE user_id = $1', target_data['user_id'])
        await message.answer(f"✅ @{target_data['username']} — админ!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("/removeadmin @username")
    try:
        target_input = parts[1].replace("@", "")
        async with pool.acquire() as conn:
            target_data = await conn.fetchrow('SELECT user_id, username FROM users WHERE username = $1', target_input)
        if not target_data:
            return await message.answer(f"❌ @{target_input} не найден")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_admin = 0 WHERE user_id = $1', target_data['user_id'])
        await message.answer(f"✅ @{target_data['username']} снят!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(F.text == "💰 Баланс")
async def btn_balance(message: Message):
    # Показываем свой баланс (как старая версия функции)
    data = await get_user_data(message.from_user.id)
    if not data:
        return await cmd_start(message)
    await message.answer(f"💰 Твой баланс: **{data['balance']}** монет", parse_mode="Markdown")

@dp.message(F.text == "🎁 Награда")
async def btn_claim(m):
    await cmd_claim(m)

@dp.message(F.text == "📦 Каталог")
async def btn_catalog(m):
    await cmd_catalog(m)

@dp.message(F.text == "❓ Помощь")
async def btn_help(m):
    await cmd_help(m)

async def main():
    await init_db()
    logger.info("🤖 Запущен с PostgreSQL")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
