import asyncio
import logging
import os
from datetime import datetime, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
import threading
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import random
import math
import time
from aiogram.types import ChatPermissions
import uuid
from aiogram.filters import Command
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальный пул соединений
pool = None

# 🔹 Словарь для хранения активных таймеров
active_timers = {}
poker_locks = {}  # {game_uuid: asyncio.Lock()}
QUIZ_TIME_LIMIT = 15  # секунд на вопрос
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                last_claim TEXT,
                is_admin INTEGER DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                sender_id BIGINT,
                receiver_id BIGINT,
                amount INTEGER,
                type TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # === НОВАЯ ТАБЛИЦА ДЛЯ ВИКТОРИН ===
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS quizzes (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                message_id INTEGER,
                prize_pool INTEGER,
                time_limit_seconds INTEGER,
                questions JSONB, -- Тут хранятся все вопросы списком
                created_by BIGINT,
                status TEXT DEFAULT 'waiting', -- waiting, active, finished
                started_at TIMESTAMP
            )
        ''')
        # === ТАБЛИЦА ОТВЕТОВ ===
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS quiz_answers (
                quiz_id INTEGER,
                user_id BIGINT,
                question_index INTEGER,
                is_correct BOOLEAN,
                response_time_sec INTEGER,
                started_at TIMESTAMP,
                PRIMARY KEY (quiz_id, user_id, question_index)
            )
        ''')
        # === ТАБЛИЦА СТАТИСТИКИ ===
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                total_games INTEGER DEFAULT 0,
                total_won BIGINT DEFAULT 0
            )
        ''')
        # === ТАБЛИЦА КАТАЛОГА ===
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS catalog (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                price INTEGER NOT NULL,
                image_url TEXT
            )
        ''')
        # Сначала удаляем старую таблицу (если есть)
        await conn.execute('DROP TABLE IF EXISTS chat_messages')

        # Создаём новую таблицу с правильной структурой
        await conn.execute('''
            CREATE TABLE chat_messages (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                message_time TIMESTAMP DEFAULT NOW()
            )
        ''')
        
# Индекс для быстрого поиска
        await conn.execute('''
            CREATE INDEX idx_chat_messages_time 
            ON chat_messages(message_time)
        ''')

        await conn.execute('''
            CREATE INDEX idx_chat_messages_chat_user 
            ON chat_messages(chat_id, user_id)
        ''')
        

    
    # ... остальной код ...

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

async def cleanup_old_messages():
    """Удаляет сообщения старше 7 дней"""
    try:
        result = await pool.execute('''
            DELETE FROM chat_messages
            WHERE message_time < NOW() - INTERVAL '7 days'
        ''')
        logging.info(f"🧹 Удалено старых сообщений: {result}")
    except Exception as e:
        logging.error(f"Ошибка очистки: {e}")

async def periodic_cleanup():
    while True:
        await asyncio.sleep(3600)  # Каждый час
        await cleanup_old_messages()
        
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
# === ПОМОЩНИКИ ДЛЯ КАЗИНО ===
def get_roulette_color(num):
    red = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    if num == 0: return "🟢"
    return "🔴" if num in red else "⚫"

async def deduct_balance(user_id, amount):
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT balance FROM users WHERE user_id = $1', user_id)
        if not row or row['balance'] < amount:
            return False, 0
        await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, user_id)
        return True, row['balance'] - amount

async def add_winnings(user_id, amount, bet, game_type):
    async with pool.acquire() as conn:
        await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, user_id)
        await conn.execute(
            'INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES (0, $1, $2, $3)',
            user_id, amount, f'{game_type}_win'
        )
        row = await conn.fetchrow('SELECT balance FROM users WHERE user_id = $1', user_id)
        return row['balance']

async def log_loss(user_id, bet, game_type):
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES (0, $1, $2, $3)',
            user_id, -bet, f'{game_type}_lose'
        )



@dp.message(Command("casino", "казино"))
async def cmd_casino_menu(message: Message):
    text = (
        "🎰 **КАЗИНО BLESSCOIN**\n\n"
        "Доступные игры:\n"
        "🎰 /slots [ставка] — Игровые автоматы\n"
        "🎡 /roulette [ставка] [цвет/число] — Рулетка\n"
        "🃏 /blackjack [ставка] — Блэкджек\n"
        "🎴 /poker [ставка] [количество игроков] — Техасский покер\n\n"
        "⚠️ Шанс есть всегда, но удача любит смелых!"
    )
    await message.answer(text, parse_mode="Markdown")
    
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    await register_user(user_id, username)
    
    # 🔹 Проверяем, есть ли параметр (например, /start poker_abc123)
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("poker_"):
        table_id = args[1].replace("poker_", "")
        
        # Показываем кнопку для открытия Web App с table_id
        webapp_url = f"https://vopros00111111-cloud.github.io/Trollbotapp/?table={table_id}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Открыть покер", web_app=WebAppInfo(url=webapp_url))]
        ])
        
        await message.answer(
            f"🃏 **Вас пригласили за покерный стол!**\n\n"
            f"Нажмите кнопку ниже чтобы присоединиться.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return
    
    # Обычный /start без параметров
    text = "👋 Привет! Я 𝗕𝗹𝗲𝘀𝗖𝗼𝗶𝗻 Bot.\n\n"
    text += "📋  Доступные команды: \n"
    text += "/balance — проверить баланс\n"
    text += "/claim — ежедневная награда\n"
    text += "/transfer — перевести монеты\n"
    text += "/catalog — магазин товаров\n"
    text += "/casino — казино\n"
    text += "/help — полная справка"
    await message.answer(text, parse_mode="Markdown")
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

# Глобальный словарь для хранения chat_id пользователей
user_chat_context = {}  # {user_id: {"chat_id": 123, "username": "..."}}
@dp.message(Command("app", "играть"))
async def cmd_open_app(message: Message):
    webapp_url = "https://vopros00111111-cloud.github.io/Trollbotapp/"
    
    # 🔹 КЛЮЧЕВОЕ: Сохраняем chat_id ЧАТА, где написали /app
    # Даже если потом отправим кнопку в ЛС — контекст группы сохранится
    user_chat_context[message.from_user.id] = {
        "chat_id": message.chat.id,
        "username": message.from_user.username or "unknown"
    }
    
    logging.info(f"💾 Сохранён контекст для {message.from_user.id}: chat_id={message.chat.id}, type={message.chat.type}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Открыть BlessCoin", web_app=WebAppInfo(url=webapp_url))]
    ])
    
    # 🔹 Если это группа — отправляем кнопку в ЛС
    if message.chat.type in ["group", "supergroup"]:
        try:
            await bot.send_message(
                message.from_user.id,
                "🚀 Нажми на кнопку чтобы открыть приложение!",
                reply_markup=keyboard
            )
            await message.answer("📩 Отправил ссылку в личные сообщения!")
        except Exception as e:
            logging.error(f"❌ Не удалось отправить в ЛС: {e}")
            await message.answer(f"❌ Ошибка: напишите боту в ЛС сначала /start")
    else:
        # В ЛС отправляем напрямую
        await message.answer(
            "🚀 Нажми на кнопку ниже, чтобы открыть приложение!", 
            reply_markup=keyboard
        )
@dp.message(Command("history", "logs", "история"))
async def cmd_history(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    
    async with pool.acquire() as conn:
        # Берем последние 20 записей
        rows = await conn.fetch('SELECT * FROM transactions ORDER BY id DESC LIMIT 20')
    
    if not rows:
        return await message.answer("📭 История пуста")
    
    text = "📜 **ИСТОРИЯ ОПЕРАЦИЙ:**\n\n"
    for row in rows:
        sender = row['sender_id']
        receiver = row['receiver_id']
        amount = row['amount']
        t_type = row['type']
        time = row['created_at'].strftime("%d.%m %H:%M")
        
        # Красивое описание
        if t_type == "transfer":
            desc = f" Перевод: {amount} монет"
        elif t_type == "admin_add":
            desc = f"➕ Админ выдал: {amount} монет"
        elif t_type == "admin_remove":
            desc = f"➖ Админ списал: {amount} монет"
        else:
            desc = f"❓ {t_type}: {amount}"
            
        text += f"⏰ {time}\n{desc}\nID отправителя: {sender}\nID получателя: {receiver}\n\n"
    
    await message.answer(text)
    
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
    text += "/top — топ 10 по монетам\n"
    text += "/profile — твой профиль\n"
    text += "/transfer @user сумма — перевод\n\n"
    text += "📦 **Каталог:**\n"
    text += "/catalog — товары\n\n"
    text += "🎰 Казино:\n"
    text += "/casino — игры казино\n\n"
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
    if len(parts) != 3:
        return await message.answer("/transfer @user сумма")
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
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', target)
            if not t or t['user_id'] == sender:
                return await message.answer("Ошибка")
            
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, sender)
            await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, t['user_id'])
            
            # ЗАПИСЬ В ИСТОРИЮ
            await conn.execute(
                'INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)',
                sender, t['user_id'], amount, 'transfer'
            )
        
        new_sender_bal = sender_data['balance'] - amount
        new_target_bal = t['balance'] + amount
        
        await message.answer(f"✅ Переведено **{amount}** монет @{t['username']}\nТвой баланс: **{new_sender_bal}**", parse_mode="Markdown")
        try:
            await bot.send_message(t['user_id'], f"💸 Тебе перевели **{amount}** монет!\nБаланс: **{new_target_bal}**", parse_mode="Markdown")
        except: pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

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
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/givemoney @user сумма")
    try:
        name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0: return
        
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', name)
        if not t: return await message.answer("Не найден")
        
        await add_balance(t['user_id'], amount)
        new_bal = t['balance'] + amount
        
        async with pool.acquire() as conn:
             # ЗАПИСЬ В ИСТОРИЮ (sender_id = 0, так как это админ)
            await conn.execute(
                'INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)',
                message.from_user.id, t['user_id'], amount, 'admin_add'
            )

        await message.answer(f"✅ Выдано {amount} монет @{t['username']}\nБаланс: {new_bal}")
        try:
            await bot.send_message(t['user_id'], f"🎁 Админ выдал **{amount}** монет!\nБаланс: **{new_bal}**", parse_mode="Markdown")
        except: pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("takemoney"))
async def cmd_takemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/takemoney @user сумма")
    try:
        name = parts[1].replace("@", "")
        amount = int(parts[2])
        if amount <= 0: return
        
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', name)
        if not t or t['balance'] < amount:
            return await message.answer("Ошибка")
        
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, t['user_id'])
            # ЗАПИСЬ В ИСТОРИЮ
            await conn.execute(
                'INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)',
                message.from_user.id, t['user_id'], amount, 'admin_remove'
            )
        
        new_bal = t['balance'] - amount
        await message.answer(f"✅ Списано {amount} монет у @{t['username']}\nБаланс: {new_bal}")
        try:
            await bot.send_message(t['user_id'], f"⚠️ Админ списал **{amount}** монет!\nБаланс: **{new_bal}**", parse_mode="Markdown")
        except: pass
    except Exception as e:
        await message.answer(f" Ошибка: {e}")
        
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
        
        # Уведомление админу
        await message.answer(f"✅ @{target_data['username']} назначен админом!")
        
        # Уведомление новому админу
        try:
            await bot.send_message(
                target_data['user_id'],
                f"👑 Поздравляем! Ты назначен **администратором** бота!\nТеперь доступны команды: /givemoney, /takemoney, /additem, /removeitem, /addadmin, /removeadmin",
                parse_mode="Markdown"
            )
        except:
            pass
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
        
        # Уведомление админу
        await message.answer(f"✅ @{target_data['username']} снят с должности админа.")
        
        # Уведомление пользователю
        try:
            await bot.send_message(
                target_data['user_id'],
                f"⚠️ Ты **снят** с должности администратора.",
                parse_mode="Markdown"
            )
        except:
            pass
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timedelta
import json
import asyncio

# --- СОСТОЯНИЯ ДЛЯ СОЗДАНИЯ ВИКТОРИНЫ ---
class QuizCreateStates(StatesGroup):
    waiting_for_question = State()
    waiting_for_options = State()
    waiting_for_correct = State()
    asking_more = State()
    waiting_for_prize = State()

# ВРЕМЕННОЕ ХРАНИЛИЩЕ ДАННЫХ (в памяти)
temp_quizzes = {}

# 1. НАЧАЛО СОЗДАНИЯ
@dp.message(Command("create_quiz"))
async def start_create_quiz(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        return
    temp_quizzes[message.from_user.id] = {"questions": []}
    await message.answer("🎬 Создаём викторину.\n\nВведи **вопрос** №1:")
    await state.set_state(QuizCreateStates.waiting_for_question)

# 2. ВОПРОС
@dp.message(QuizCreateStates.waiting_for_question)
async def get_question(message: Message, state: FSMContext):
    temp_quizzes[message.from_user.id]["questions"].append({
        "text": message.text,
        "options": [],
        "correct": 0
    })
    await message.answer("Введи **варианты ответов** через `|`.\nПример: `Синий|Красный|Зеленый`")
    await state.set_state(QuizCreateStates.waiting_for_options)

# 3. ВАРИАНТЫ
@dp.message(QuizCreateStates.waiting_for_options)
async def get_options(message: Message, state: FSMContext):
    opts = message.text.split("|")
    # Убираем пробелы по краям и пустые варианты
    opts = [o.strip() for o in opts if o.strip()]
    
    if len(opts) < 2:
        return await message.answer("Нужно минимум 2 варианта!")
    
    q_list = temp_quizzes[message.from_user.id]["questions"]
    q_list[-1]["options"] = opts
    
    # Создаем кнопки явно через объект KeyboardButton
    btns = []
    for i, opt in enumerate(opts):
        btns.append([KeyboardButton(text=f"✅ {i+1}. {opt}")])
        
    keyboard = ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)
    
    await message.answer("Какой вариант правильный? (Нажми кнопку ниже)", reply_markup=keyboard)
    await state.set_state(QuizCreateStates.waiting_for_correct)
# 4. ПРАВИЛЬНЫЙ ОТВЕТ
@dp.message(QuizCreateStates.waiting_for_correct)
async def get_correct(message: Message, state: FSMContext):
    # Определяем номер ответа (ищем по тексту)
    opts = temp_quizzes[message.from_user.id]["questions"][-1]["options"]
    correct_idx = -1
    for i, opt in enumerate(opts):
        if opt.strip() in message.text:
            correct_idx = i
            break
            
    if correct_idx == -1:
        return await message.answer("Не понял, нажми на кнопку из списка выше.")

    temp_quizzes[message.from_user.id]["questions"][-1]["correct"] = correct_idx
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="➕ Да"), KeyboardButton(text=" Нет, завершить")]], resize_keyboard=True)
    await message.answer("Добавить ещё вопрос?", reply_markup=kb)
    await state.set_state(QuizCreateStates.asking_more)

# 5. ЕЩЁ ВОПРОС?
@dp.message(QuizCreateStates.asking_more)
async def ask_more(message: Message, state: FSMContext):
    if message.text.lower() in ["да", "➕ да"]:
        count = len(temp_quizzes[message.from_user.id]["questions"]) + 1
        await message.answer(f"Введи **вопрос** №{count}:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(QuizCreateStates.waiting_for_question)
    else:
        await message.answer("Введи **призовой фонд** (сумма монет, которую получат победители):", reply_markup=ReplyKeyboardRemove())
        await state.set_state(QuizCreateStates.waiting_for_prize)

# 6. ПРИЗ И СОХРАНЕНИЕ
@dp.message(QuizCreateStates.waiting_for_prize)
async def finish_create_quiz(message: Message, state: FSMContext):
    try:
        prize = int(message.text)
    except:
        return await message.answer("Это не число!")
    
    data = temp_quizzes[message.from_user.id]
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'INSERT INTO quizzes (chat_id, prize_pool, questions, created_by) VALUES (0, $1, $2, $3) RETURNING id',
            prize, json.dumps(data["questions"]), message.from_user.id
        )
    
    text = f"✅ Викторина создана!\nID: **{row['id']}**\nВопросов: {len(data['questions'])}\n\nТеперь напиши в чате: `/quiz 1h {row['id']}`"
    await message.answer(text, parse_mode="Markdown")
    
    await state.clear()
    del temp_quizzes[message.from_user.id]
# 7. ПУБЛИКАЦИЯ В ЧАТЕ
@dp.message(Command("quiz"))
async def publish_quiz(message: Message):
    if not await check_admin(message.from_user.id): 
        return
    
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: `/quiz <время> <ID>`. Пример: `/quiz 1h 5`", parse_mode="Markdown")
    
    time_str, quiz_id = args[1], args[2]
    
    # Парсим время
    seconds = 0
    if time_str.endswith("h"): 
        seconds = int(time_str[:-1]) * 3600
    elif time_str.endswith("m"): 
        seconds = int(time_str[:-1]) * 60
    else: 
        return await message.answer("Время в формате 30m или 1h")
    
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", int(quiz_id))
        
    if not quiz:
        return await message.answer("Викторина с таким ID не найдена")
    
    # Отправляем сообщение с кнопкой
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"start_quiz_{quiz_id}")]])
    
    sent_message = await message.answer(
        f"🏆 **ВИКТОРИНА #{quiz_id}**\n\n💰 Банк: **{quiz['prize_pool']}** монет\n⏱️ Время: **{time_str}**\n📝 Вопросов: {len(json.loads(quiz['questions']))}",
        reply_markup=btn, 
        parse_mode="Markdown"
    )
    
    # Закрепляем сообщение
    try:
        await sent_message.pin()
    except Exception as e:
        await message.answer(f"⚠️ Не удалось закрепить сообщение: {e}")
    
    # Обновляем статус и сохраняем message_id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE quizzes SET status = 'active', chat_id = $1, started_at = NOW(), time_limit_seconds = $2, message_id = $3 WHERE id = $4", 
            message.chat.id, seconds, sent_message.message_id, int(quiz_id)
        )
    
    # Запускаем таймер в фоне
    asyncio.create_task(finish_quiz_task(int(quiz_id), seconds))

# 8. УЧАСТИЕ (КНОПКА В ЧАТЕ)
@dp.callback_query(F.data.startswith("start_quiz_"))
async def quiz_click(cb: CallbackQuery):
    quiz_id = int(cb.data.split("_")[2])
    user_id = cb.from_user.id
    
    # Проверка: не участвовал ли уже?
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM quiz_answers WHERE quiz_id = $1 AND user_id = $2", quiz_id, user_id)
    
    if count > 0:
        return await cb.answer("Ты уже участвуешь! Ищи сообщение от бота в ЛС.", show_alert=True)       
        
    await cb.bot.send_message(user_id, " **Викторина началась!**\nОтвечай на вопросы...", parse_mode="Markdown")
    await send_quiz_question(user_id, quiz_id, 0)
    await cb.answer()

# 9. ОТПРАВКА ВОПРОСА (В ЛС)
def build_quiz_kb(options: list, quiz_id: int, q_index: int):
    btns = [[InlineKeyboardButton(text=f"{i+1}. {opt}", callback_data=f"ans_{quiz_id}_{q_index}_{i}")] for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=btns)
async def send_quiz_question(user_id: int, quiz_id: int, q_index: int = 0):
    # 🔹 Исправление 1: Получаем вопросы из таблицы quizzes (JSONB)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT questions FROM quizzes WHERE id = $1", quiz_id)
        
    if not row:
        return await bot.send_message(user_id, "❌ Викторина не найдена.")

    # Парсим JSON, если вопросы пришли строкой
    questions = row['questions']
    if isinstance(questions, str):
        import json
        questions = json.loads(questions)
        
    if not questions:
        return await bot.send_message(user_id, "❌ Вопросы не найдены.")
        
    # Если вопросы закончились — завершаем викторину
    if q_index >= len(questions):
        async with pool.acquire() as conn:
            score = await conn.fetchval(
                "SELECT COUNT(*) FROM quiz_answers WHERE quiz_id=$1 AND user_id=$2 AND is_correct=True",
                quiz_id, user_id
            )
        
        msg = f"✅ **Викторина завершена!**\n\n"
        msg += f"🎯 **Твой результат:**\n"
        msg += f"✅ Правильных ответов: **{score}/{len(questions)}**\n\n"
        msg += f"🏆 Итоги и награды будут опубликованы в чате после окончания таймера."
    
        await bot.send_message(user_id, msg, parse_mode="Markdown")
        return  # 🔹 Важно: выходим из функции, код ниже не выполнится
    # 3. Берем текущий вопрос
    question = questions[q_index]
    
    # 4. Формируем текст
    text = f"**Вопрос {q_index + 1}/{len(questions)}**\n\n{question['text']}"
    
    # 🔹 Исправление 2: Сохраняем отправленное сообщение в переменную msg
    msg = await bot.send_message(
        user_id,
        text,
        reply_markup=build_quiz_kb(question['options'], quiz_id, q_index),
        parse_mode="Markdown"
    )
    
    # 🔹 Запускаем таймер
    async def time_is_up():
        try:
            await asyncio.sleep(QUIZ_TIME_LIMIT)
            
            # Если код дошел сюда, значит пользователь НЕ ответил вовремя
            
            # 1. Редактируем сообщение (убираем кнопки)
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg.message_id,
                text=f"{text}\n\n⏰ **Время вышло!**",
                reply_markup=None,
                parse_mode="Markdown"
            )
            
            # 2. Записываем в БД как ошибку (0 баллов)
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO quiz_answers (quiz_id, user_id, question_index, is_correct, response_time_sec, started_at) VALUES ($1, $2, $3, $4, $5, NOW())",
                    quiz_id, user_id, q_index, False, QUIZ_TIME_LIMIT
                )
            
            # 3. Ждем немного и переходим к следующему вопросу
            await asyncio.sleep(1.5)
            await send_quiz_question(user_id, quiz_id, q_index + 1)
            
        except asyncio.CancelledError:
            # Таймер был отменен (пользователь успел ответить)
            pass
        except Exception as e:
            logging.error(f"Ошибка таймера: {e}")

    # Создаем задачу таймера и сохраняем её в словарь
    timer_task = asyncio.create_task(time_is_up())
    active_timers[(user_id, quiz_id, q_index)] = timer_task

# 10. ОБРАБОТКА ОТВЕТА (В ЛС)
@dp.callback_query(F.data.startswith("ans_"))
async def process_answer(cb: CallbackQuery):
    # 🔹 ОТМЕНА ТАЙМЕРА (добавь это!)
    user_id = cb.from_user.id
    if user_id in active_timers:
        active_timers[user_id].cancel()
        del active_timers[user_id]
    # 🔹 Конец отмены таймера
    _, quiz_id, q_idx, answer_idx = cb.data.split("_")
    quiz_id, q_idx, answer_idx = int(quiz_id), int(q_idx), int(answer_idx)
    
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT questions FROM quizzes WHERE id = $1", quiz_id)
        if not quiz: return
        
        questions = quiz['questions']
        # Парсим если строка
        if isinstance(questions, str):
            import json
            questions = json.loads(questions)
            
        is_correct = (questions[q_idx]['correct'] == answer_idx)
        
        # Записываем ответ
        await conn.execute(
            "INSERT INTO quiz_answers (quiz_id, user_id, question_index, is_correct, response_time_sec, started_at) VALUES ($1, $2, $3, $4, $5, NOW())",
            quiz_id, cb.from_user.id, q_idx, is_correct, 0
        )
        
    await cb.answer("Ответ принят!")
    await cb.message.delete() 
    await send_quiz_question(cb.from_user.id, quiz_id, q_idx + 1)
# 11. ФИНАЛ И НАГРАДЫ
async def finish_quiz_task(quiz_id, delay):
    await asyncio.sleep(delay)
    
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", quiz_id)
        
        if not quiz or quiz['status'] == 'finished': 
            return
        
        chat_id = quiz['chat_id']
        message_id = quiz.get('message_id')
        
        # 1. МЕНЯЕМ СТАТУС
        await conn.execute("UPDATE quizzes SET status = 'finished' WHERE id = $1", quiz_id)
        
        # 2. СЧИТАЕМ РЕЗУЛЬТАТЫ"
        # 1. Убрали LIMIT 3, чтобы получить ВСЕХ участников
        results = await conn.fetch('''
            SELECT user_id, 
                   COUNT(*) FILTER (WHERE is_correct = true) as score,
                   EXTRACT(EPOCH FROM (MAX(started_at) - MIN(started_at))) as duration_sec
            FROM quiz_answers 
            WHERE quiz_id = $1 
            GROUP BY user_id 
            ORDER BY score DESC, duration_sec ASC
        ''', quiz_id)
        
        text = f"🏁 **ВИКТОРИНА #{quiz_id} ЗАВЕРШЕНА!**\n\n"
        
        if not results:
            text += "😔 **Никто не участвовал**"
        else:
            prize = quiz['prize_pool']
            distribution = [0.5, 0.3, 0.2]  # Призы только топ-3
            
            for i, row in enumerate(results):
                uid = row['user_id']
                score = row['score']
                duration = row['duration_sec'] or 0
                
                # Топ-3: медали + призы
                if i < 3:
                    reward = int(prize * distribution[i])
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", reward, uid)
                    medals = ["🥇", "🥈", "🥉"]
                    prefix = f"{medals[i]} "
                    reward_str = f" → **+{reward} 🪙**"
                # Остальные: просто номер
                else:
                    prefix = f" {i+1}. "
                    reward_str = ""
                
                username = await conn.fetchval("SELECT username FROM users WHERE user_id = $1", uid)
                time_str = f"{duration:.1f}с"
                
                text += f"{prefix}{username or uid}: {score} прав. ({time_str}){reward_str}\n"
        # 3. ОТПРАВЛЯЕМ РЕЗУЛЬТАТЫ (ОБЯЗАТЕЛЬНО!)
        try:
            result_msg = await bot.send_message(chat_id, text, parse_mode="Markdown")
            await result_msg.pin()
            print(f"✅ Результаты викторины {quiz_id} отправлены и закреплены")
        except Exception as e:
            print(f"❌ Ошибка отправки результатов: {e}")
            # Пробуем отправить без Markdown
            try:
                await bot.send_message(chat_id, text.replace("**", "").replace("🪙", "монет"))
            except:
                pass
        
        # 4. ОТКРЕПЛЯЕМ СТАРОЕ (в конце, чтобы не мешало)
        if message_id:
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
                print(f"✅ Старое сообщение откреплено")
            except Exception as e:
                print(f"⚠️ Не удалось открепить: {e}")
# === СОСТОЯНИЯ ДЛЯ БЛЭКДЖЕКА ===
class BlackjackStates(StatesGroup):
    playing = State()
# === СОСТОЯНИЯ ДЛЯ САПЁРА ===
class MinesStates(StatesGroup):
    playing = State()
    
async def check_casino_spam(user_id: int, chat_id: int, bot: Bot) -> bool:
    """
    Проверяет спам командами казино.
    В ЛС не работает (нет мутов), только в чатах.
    Возвращает True если пользователь ЗАМУЧЕН.
    """
    # 🔹 В ЛС анти-спам не применяется
    if chat_id > 0:
        return False
    
    now = time.time()
    
    # Инициализируем список если нет
    if user_id not in casino_command_times:
        casino_command_times[user_id] = []
    
    # Убираем старые записи (старше 1 минуты)
    casino_command_times[user_id] = [
        t for t in casino_command_times[user_id] 
        if now - t < CASINO_SPAM_WINDOW
    ]
    
    # Проверяем лимит
    if len(casino_command_times[user_id]) >= CASINO_SPAM_LIMIT:
        # МУТИМ на 30 минут
        try:
            until_date = int(now + CASINO_MUTE_DURATION)
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔇 <a href='tg://user?id={user_id}'>Игрок</a> получил мут на 30 минут за спам казино!\nНе больше 3 игр в минуту.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Не удалось замутить {user_id}: {e}")
        
        # Сбрасываем счётчик
        casino_command_times[user_id] = []
        return True  # Замучен, команду выполнять НЕЛЬЗЯ
    
    # Добавляем текущее время
    casino_command_times[user_id].append(now)
    return False  # Не замучен, можно играть
    
# === ИГРА: СЛОТЫ ===
@dp.message(Command("slots", "слоты"))
async def cmd_slots(message: Message):
    # 🔹 АНТИ-СПАМ ПРОВЕРКА
    if await check_casino_spam(message.from_user.id, message.chat.id, bot):
        return
    args = message.text.split()
    if len(args) < 2:
        return await message.answer(" Введите ставку: `/slots 100`")
    try:
        bet = int(args[1])
        if bet < 10: return await message.answer("❌ Минимальная ставка: 10 монет")
    except ValueError:
        return await message.answer("❌ Некорректная ставка")

    user_id = message.from_user.id
    ok, new_bal = await deduct_balance(user_id, bet)
    if not ok:
        return await message.answer("❌ Недостаточно монет!")

    symbols = ["🍒", "❓", "🍉", "🍫", "💎", "7️", "🔔"]
    
    # Отправляем сообщение с "вращающимися" символами
    msg = await message.answer("🎰 **КРУТИМ БАРАБАНЫ** \n\n🍒 |  | 🍒")
    
    await asyncio.sleep(0.5)
    await msg.edit_text("🎰 **КРУТИМ БАРАБАНЫ** 🎰\n\n🍇 | 7️⃣ | 🍇", parse_mode="Markdown")
    
    await asyncio.sleep(0.5)
    
    # Финальный результат
    reel = [random.choice(symbols) for _ in range(3)]
    
    win_amount = 0
    result_text = ""
    
    if reel[0] == reel[1] == reel[2] == "7️⃣":
        win_amount = bet * 50
        result_text = " **ДЖЕКПОТ 777!** 🔥"
    elif reel[0] == reel[1] == reel[2]:
        win_amount = bet * 10
        result_text = "✨ **ТРИ В РЯД!** ✨"
    elif reel[0] == reel[1] or reel[1] == reel[2] or reel[0] == reel[2]:
        win_amount = bet * 2
        result_text = "✅ **ДВА СОВПАДЕНИЯ!**"
    else:
        result_text = "❌ **НЕ ПОВЕЗЛО**"
        await log_loss(user_id, bet, "slots")
        await msg.edit_text(f"🎰 **РЕЗУЛЬТАТ** 🎰\n\n{' | '.join(reel)}\n\n{result_text}\n\n💸 Проигрыш: **{bet}**", parse_mode="Markdown")
        return

    if win_amount > 0:
        final_bal = await add_winnings(user_id, win_amount, bet, "slots")
        await msg.edit_text(
            f"🎰 **РЕЗУЛЬТАТ** 🎰\n\n{' | '.join(reel)}\n\n{result_text}\n Выигрыш: **{win_amount}**\n💰 Баланс: **{final_bal}**",
            parse_mode="Markdown"
        )
# === ИГРА: РУЛЕТКА ===
@dp.message(Command("roulette", "рулетка"))
async def cmd_roulette(message: Message):
    # 🔹 АНТИ-СПАМ ПРОВЕРКА
    if await check_casino_spam(message.from_user.id, message.chat.id, bot):
        return
    
    args = message.text.split()
    # ... остальной код без изменений ...
    if len(args) < 3:
        return await message.answer(" Формат: `/roulette [ставка] [красное/чёрное/зелёное/число]`")
    
    try:
        bet = int(args[1])
        if bet < 10: return await message.answer("❌ Мин. ставка 10")
    except: return await message.answer("❌ Ошибка ставки")

    choice = args[2].lower()
    user_id = message.from_user.id

    ok, _ = await deduct_balance(user_id, bet)
    if not ok: return await message.answer("❌ Недостаточно монет!")

    msg = await message.answer("🎡 **БАРАБАН ВРАЩАЕТСЯ...**")
    await asyncio.sleep(1.5)

    number = random.randint(0, 36)
    color = get_roulette_color(number)
    color_name = "Зелёное" if number == 0 else ("Красное" if color == "🔴" else "Чёрное")

    win_amount = 0
    won = False

    # Проверка выигрыша
    if choice in ["красное", "красный", "red"]:
        if color == "🔴": win_amount = bet * 2; won = True
    elif choice in ["чёрное", "черное", "black"]:
        if color == "⚫": win_amount = bet * 2; won = True
    elif choice in ["зелёное", "зеленое", "green", "0"]:
        if number == 0: win_amount = bet * 14; won = True
    else:
        try:
            pick_num = int(choice)
            if pick_num == number: win_amount = bet * 36; won = True
        except:
            await log_loss(user_id, bet, "roulette")
            await msg.edit_text(f" **ВЫПАЛО: {number} {color_name}**\n❌ Вы сделали неверную ставку.")
            return

    if won:
        final_bal = await add_winnings(user_id, win_amount, bet, "roulette")
        await msg.edit_text(
            f"🎡 **ВЫПАЛО: {number} {color_name}**\n\n🎉 **ВЫИГРЫШ!** +{win_amount} монет\n💰 Баланс: {final_bal}",
            parse_mode="Markdown"
        )
    else:
        await log_loss(user_id, bet, "roulette")
        await msg.edit_text(f"🎡 **ВЫПАЛО: {number} {color_name}**\n\n Вы проиграли {bet} монет.")
# === ИГРА: БЛЭКДЖЕК ===
@dp.message(Command("blackjack", "блэкджек"))
async def cmd_blackjack_start(message: Message, state: FSMContext):
    # 🔹 АНТИ-СПАМ ПРОВЕРКА
    if await check_casino_spam(message.from_user.id, message.chat.id, bot):
        return
    
    args = message.text.split()
    # ... остальной код без изменений ...
    if len(args) < 2: return await message.answer("🃏 Формат: `/blackjack [ставка]`")
    try:
        bet = int(args[1])
        if bet < 10: return await message.answer("Мин. ставка 10")
    except: return await message.answer("Ошибка ставки")

    user_id = message.from_user.id
    ok, _ = await deduct_balance(user_id, bet)
    if not ok: return await message.answer("❌ Недостаточно монет!")

    # Раздача карт (упрощённая: картинки = 10, туз = 11)
    player_hand = [random.randint(2, 11), random.randint(2, 11)]
    dealer_hand = [random.randint(2, 11)] # Одна карта скрыта
    
    p_score = sum(player_hand)
    
    # Сохраняем данные игры
    await state.set_data({"bet": bet, "player_hand": player_hand, "dealer_hand": dealer_hand})
    await state.set_state(BlackjackStates.playing)

    btns = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👆 ЕЩЁ", callback_data="bj_hit"), InlineKeyboardButton(text="✋ ХВАТИТ", callback_data="bj_stand")]
    ])
    
    text = (
        f"🃏 **БЛЭКДЖЕК** (Ставка: {bet})\n\n"
        f"Ваши карты: {player_hand} (Сумма: {p_score})\n"
        f"Карты дилера: [{dealer_hand[0]}, ❓]"
    )
    await message.answer(text, reply_markup=btns, parse_mode="Markdown")

@dp.callback_query(BlackjackStates.playing)
async def process_blackjack(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    data = await state.get_data()
    bet = data["bet"]
    p_hand = data["player_hand"]
    d_hand = data["dealer_hand"]

    if cb.data == "bj_hit":
        # Берем карту
        new_card = random.randint(2, 11)
        p_hand.append(new_card)
        p_score = sum(p_hand)

        await state.update_data(player_hand=p_hand)
        if p_score > 21:
            await log_loss(user_id, bet, "blackjack")
            text = (
                f"🃏 **ПЕРЕБОР!** 💥\n\n"
                f"Ваши карты: {p_hand} (Сумма: {p_score})\n"
                f"Дилер выиграл!\n💸 Проигрыш: {bet}"
            )
            await cb.message.edit_text(text, parse_mode="Markdown")
            await state.clear()
        else:
            text = (
                f"🃏 **БЛЭКДЖЕК** (Ставка: {bet})\n\n"
                f"Ваши карты: {p_hand} (Сумма: {p_score})\n"
                f"Карты дилера: [{d_hand[0]}, ❓]"
            )
            btns = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=" ЕЩЁ", callback_data="bj_hit"), InlineKeyboardButton(text="✋ ХВАТИТ", callback_data="bj_stand")]
            ])
            await cb.message.edit_text(text, reply_markup=btns, parse_mode="Markdown")

    elif cb.data == "bj_stand":
        # Ход дилера (берет карты пока меньше 17)
        while sum(d_hand) < 17:
            d_hand.append(random.randint(2, 11))
        
        p_score = sum(p_hand)
        d_score = sum(d_hand)

        text = (
            f"🃏 **РЕЗУЛЬТАТ**\n\n"
            f"Ваши карты: {p_hand} ({p_score})\n"
            f"Карты дилера: {d_hand} ({d_score})\n\n"
        )

        if d_score > 21 or p_score > d_score:
            win = bet * 2
            final_bal = await add_winnings(user_id, win, bet, "blackjack")
            text += f"🎉 **ВЫ ВЫИГРАЛИ!** +{win} монет\n Баланс: {final_bal}"
        elif p_score == d_score:
            # Возврат ставки (ничья)
            await add_winnings(user_id, bet, bet, "blackjack_draw") 
            text += f"🤝 **НИЧЬЯ.** Ставка возвращена."
        else:
            text += f" **ДИЛЕР ВЫИГРАЛ.** Вы потеряли {bet} монет."
            await log_loss(user_id, bet, "blackjack")

        await cb.message.edit_text(text, parse_mode="Markdown")
        await state.clear()
        await cb.answer()
# === АНТИ-СПАМ КАЗИНО ===
casino_command_times = {}  # {user_id: [timestamp1, timestamp2, ...]}
CASINO_SPAM_LIMIT = 3       # макс команд
CASINO_SPAM_WINDOW = 60     # секунд (1 минута)
CASINO_MUTE_DURATION = 1800 # секунд (30 минут)

# === СОЦИАЛКА: ТОП И ПРОФИЛЬ ===
@dp.message(Command("top", "топ"))
async def cmd_top(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT username, balance FROM users WHERE balance > 0 AND username IS NOT NULL ORDER BY balance DESC LIMIT 10'
        )

    if not rows:
        return await message.answer("🏆 Топ пока пуст. Стань первым!")

    text = r"🏆 *ТОП\-10 ИГРОКОВ*" + "\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}\\."
        safe_name = str(row['username']).replace('_', r'\_').replace('*', r'\*').replace('[', r'\[')
        text += f"{prefix} @{safe_name} — *{row['balance']}* 💰\n"

    await message.answer(text, parse_mode="Markdown")
@dp.message(Command("profile", "профиль"))
async def cmd_profile(message: Message):
    args = message.text.split()
    
    if len(args) > 1:
        username = args[1].replace("@", "")
        async with pool.acquire() as conn:
            row = await conn.fetchrow('SELECT user_id, username, balance, is_admin FROM users WHERE username = $1', username)
        if not row:
            return await message.answer(f"❌ Пользователь @{username} не найден")
        target_id = row['user_id']
        target_name = row['username']
        balance = row['balance']
        is_admin = row['is_admin']
    else:
        target_id = message.from_user.id
        data = await get_user_data(target_id)
        if not data:
            return await message.answer("❌ Профиль не найден. Напиши /start")
        target_name = data['username']
        balance = data['balance']
        is_admin = data['is_admin']
    
    # 🔹 Считаем место в топе
    async with pool.acquire() as conn:
        rank = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM users WHERE balance > (SELECT balance FROM users WHERE user_id = $1)",
            target_id
        )
        total_wins = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE receiver_id = $1 AND type LIKE '%_win'", target_id) or 0
        total_games = await conn.fetchval("SELECT COUNT(*) FROM transactions WHERE sender_id = $1 OR receiver_id = $1", target_id) or 0
    
    status = "👑 Админ" if is_admin == 1 else "🎮 Игрок"
    
    text = (
        f"👤 **ПРОФИЛЬ** @{target_name}\n\n"
        f"🏅 Место в топе: **#{rank}**\n"
        f"💰 Баланс: **{balance}**\n"
        f"🏆 Всего выиграно: **{total_wins}**\n"
        f"🎲 Активность: **{total_games}** операций\n"
        f"⭐ Статус: {status}"
    )
    await message.answer(text, parse_mode="Markdown")
# === ИГРА: САПЁР (MINES) ===
class MinesStates(StatesGroup):
    playing = State()
class PokerStates(StatesGroup):
    waiting_for_opponents = State()
    preflop = State()
    flop = State()
    turn = State()
    river = State()
# === ПОКЕР: ВЫЗОВ И ПОИСК СОПЕРНИКОВ ===
active_poker_games = {}  # {message_id: {"host": user_id, "bet": int, "players": [], "max_players": int, "expires_at": float}}

@dp.message(Command("poker", "покер"))
async def cmd_poker_challenge(message: Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.answer("🃏 Формат: `/poker [ставка] [кол-во игроков 2-4]`\nПример: `/poker 500 3`")
    
    try:
        bet = int(args[1])
        max_players = int(args[2]) if len(args) > 2 else 2
        if bet < 50: return await message.answer("❌ Мин. ставка 50")
        if not (2 <= max_players <= 4): return await message.answer("❌ Игроков: от 2 до 4")
    except ValueError:
        return await message.answer("❌ Неверный формат")
    
    user_id = message.from_user.id
    
    # Проверяем баланс хоста (не списываем пока!)
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT balance FROM users WHERE user_id = $1', user_id)
        if not row or row['balance'] < bet:
            return await message.answer("❌ Недостаточно монет!")
    
    # Проверяем доступ к ЛС хоста
    try:
        await bot.send_message(user_id, "🃏 Проверка доступа... Если видите это — всё ок!")
    except Exception:
        return await message.answer(
            f"❌ @{message.from_user.username}, вы заблокировали бота!\n"
            f"Напишите /start в ЛС бота и попробуйте снова.",
            parse_mode="Markdown"
        )
    
    expires_at = time.time() + 60  # 1 минута на сбор
    
    btns = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять вызов", callback_data=f"poker_join_{message.message_id}")]
    ])
    
    # Генерируем уникальный ID для игры (не зависим от msg_id!)
    game_uuid = str(uuid.uuid4())[:8]
    
    msg = await message.answer(
        f"🃏 **ПОКЕР**\n\n"
        f"👤 @{message.from_user.username} ищет соперников!\n"
        f"💰 Ставка: **{bet}** монет\n"
        f"👥 Игроков: **1/{max_players}**\n"
        f"⏳ Ожидание: **60 сек**\n\n"
        f"Нажмите кнопку, чтобы присоединиться!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять вызов", callback_data=f"poker_join_{game_uuid}")]
        ]),
        parse_mode="Markdown"
    )
    
    active_poker_games[game_uuid] = {
        "uuid": game_uuid,
        "host": user_id,
        "host_name": message.from_user.username,
        "bet": bet,
        "max_players": max_players,
        "players": [{"user_id": user_id, "username": message.from_user.username}],
        "chat_id": message.chat.id,
        "expires_at": time.time() + 60,
        "msg_id": msg.message_id,
        "status": "waiting"
    }
    
    asyncio.create_task(_poker_wait_timer(game_uuid))

async def _poker_wait_timer(game_uuid: str):
    await asyncio.sleep(60)

    if game_uuid not in active_poker_games:
        return

    game = active_poker_games[game_uuid]

    if len(game["players"]) >= 2:
        await _start_poker_game(game)
    else:
        try:
            await bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["msg_id"],
                text="🃏 **ПОКЕР ОТМЕНЁН**\n\nНикто не принял вызов.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        del active_poker_games[game_uuid]

@dp.callback_query(F.data.startswith("poker_join_"))
async def poker_join(cb: CallbackQuery):
    game_uuid = cb.data.split("_")[2]
    user_id = cb.from_user.id

    if game_uuid not in active_poker_games:
        return await cb.answer("❌ Игра уже началась или отменена!", show_alert=True)

    game = active_poker_games[game_uuid]

    if game.get("status") != "waiting":
        return await cb.answer("❌ Игра уже началась!", show_alert=True)

    if user_id == game["host"]:
        return await cb.answer("Вы создатель игры!", show_alert=True)

    if any(p["user_id"] == user_id for p in game["players"]):
        return await cb.answer("Вы уже в игре!", show_alert=True)

    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT balance FROM users WHERE user_id = $1', user_id)
        if not row or row['balance'] < game["bet"]:
            return await cb.answer("❌ Недостаточно монет!", show_alert=True)

    try:
        await bot.send_message(user_id, "🃏 Вы присоединились к покеру!")
    except Exception:
        return await cb.answer("❌ Заблокировали бота! Напишите /start в ЛС.", show_alert=True)

    game["players"].append({"user_id": user_id, "username": cb.from_user.username})
    current = len(game["players"])
    max_p = game["max_players"]

    if current >= max_p:
        await cb.answer("🎉 Стол заполнен! Начинаем...")
        await _start_poker_game(game)
        return

    remaining = max(0, int(game["expires_at"] - time.time()))
    try:
        await cb.message.edit_text(
            f"🃏 **ПОКЕР**\n\n"
            f"👤 @{game['host_name']} ищет соперников!\n"
            f"💰 Ставка: **{game['bet']}** монет\n"
            f"👥 Игроков: **{current}/{max_p}**\n"
            f"⏳ Ожидание: **{remaining} сек**\n\n"
            f"Нажмите кнопку, чтобы присоединиться!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Принять вызов", callback_data=f"poker_join_{game_uuid}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception:
        pass
async def _start_poker_game(game: dict):
    """Запускает покерную партию"""
    game_uuid = game["uuid"]
    chat_id = game["chat_id"]
    msg_id = game["msg_id"]
    players = game["players"]
    bet = game["bet"]

    # Списываем ставки у всех участников
    for player in players:
        await deduct_balance(player["user_id"], bet)

    # Редактируем сообщение в чате
    player_names = ", ".join([f"@{p['username']}" for p in players])
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=(
                f"🃏 **ПОКЕР НАЧАЛСЯ!**\n\n"
                f"👥 Игроки: {player_names}\n"
                f"💰 Банк: **{bet * len(players)}** монет\n\n"
                f"📩 Карты разосланы в ЛС!"
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    # 🔹 НЕ удаляем из словаря! Просто ставим статус
    game["status"] = "started"

    # Раздаём карты
    await _deal_poker_cards(game)
    
    # TODO: Здесь будет логика раздачи карт (Часть 2)
# === ПОКЕР: КОЛОДА И ОЦЕНКА РУК ===
import itertools

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUES = {r: i for i, r in enumerate(RANKS, 2)}  # 2=2, ..., A=14

def create_deck():
    return [{"rank": r, "suit": s, "value": RANK_VALUES[r]} for r in RANKS for s in SUITS]

def evaluate_hand(hole_cards, community_cards):
    """Оценивает лучшую комбинацию из 7 карт."""
    all_cards = hole_cards + community_cards
    # 🔹 Инициализируем правильно: ((ранг, кикеры...), название)
    best_score = ((0,), "Старшая карта")

    for combo in itertools.combinations(all_cards, 5):
        score = _evaluate_5_cards(combo)
        # score = ((rank, tiebreakers...), name)
        if score[0] > best_score[0]:
            best_score = score

    return best_score

def _evaluate_5_cards(cards):
    """Оценивает ровно 5 карт. Возвращает (score_tuple, name)"""
    values = sorted([c["value"] for c in cards], reverse=True)
    suits = [c["suit"] for c in cards]
    is_flush = len(set(suits)) == 1
    
    # Проверка стрита
    is_straight = False
    straight_high = 0
    unique_vals = sorted(set(values), reverse=True)
    if len(unique_vals) == 5:
        if unique_vals[0] - unique_vals[4] == 4:
            is_straight = True
            straight_high = unique_vals[0]
        # Стрит A-2-3-4-5
        elif unique_vals == [14, 5, 4, 3, 2]:
            is_straight = True
            straight_high = 5
    
    # Подсчёт совпадений
    from collections import Counter
    counts = Counter(values)
    freq = sorted(counts.values(), reverse=True)
        # Рейтинг комбинаций (от старшей к младшей)
    if is_flush and is_straight:
        return ((9, straight_high), "Стрит-флеш")
    if freq == [4, 1]:
        quad_val = [v for v, c in counts.items() if c == 4][0]
        return ((8, quad_val), "Каре")
    if freq == [3, 2]:
        trip_val = [v for v, c in counts.items() if c == 3][0]
        return ((7, trip_val), "Фулл-хаус")
    if is_flush:
        return ((6, *values), "Флеш")
    if is_straight:
        return ((5, straight_high), "Стрит")
    if freq == [3, 1, 1]:
        trip_val = [v for v, c in counts.items() if c == 3][0]
        return ((4, trip_val), "Сет")
    if freq == [2, 2, 1]:
        pairs = sorted([v for v, c in counts.items() if c == 2], reverse=True)
        return ((3, *pairs), "Две пары")
    if freq == [2, 1, 1, 1]:
        pair_val = [v for v, c in counts.items() if c == 2][0]
        return ((2, pair_val), "Пара")
    
    return ((1, *values), "Старшая карта")


async def _deal_poker_cards(game: dict):
    # 🔹 Защита от повторной раздачи
    if game.get("cards_dealt"):
        return
    game["cards_dealt"] = True
    deck = create_deck()
    random.shuffle(deck)
    
    players = game["players"]
    bet = game["bet"]
    
    # Раздача: 2 карты каждому
    hands = {}
    for p in players:
        hands[p["user_id"]] = [deck.pop(), deck.pop()]
    
    # Общие карты (пока скрыты)
    community = [deck.pop() for _ in range(5)]
    
    # Сохраняем состояние игры
    game["hands"] = hands
    game["community"] = community
    game["stage"] = "preflop"
    game["pot"] = bet * len(players)
    game["active_players"] = [p["user_id"] for p in players]
    
    # Отправляем карты в ЛС
    for p in players:
        uid = p["user_id"]
        h = hands[uid]
        card_text = f"{h[0]['rank']}{h[0]['suit']}  {h[1]['rank']}{h[1]['suit']}"
        
        btns = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Колл", callback_data=f"poker_call_{game['uuid']}"),
             InlineKeyboardButton(text="❌ Фолд", callback_data=f"poker_fold_{game['uuid']}")]
        ])
        
        try:
            await bot.send_message(
                uid,
                f"🃏 **ВАШИ КАРТЫ:**\n\n{card_text}\n\n"
                f"💰 Банк: {game['pot']} | Ставка: {bet}\n"
                f"📍 Этап: Префлоп",
                reply_markup=btns,
                parse_mode="Markdown"
            )
        except Exception:
            pass  # Игрок заблокировал бота — пропускаем
# 🔹 Запускаем таймер на префлоп
    asyncio.create_task(_poker_move_timer(game["uuid"], "preflop"))
# === ПОКЕР: ХОДЫ И ФИНАЛ ===
@dp.callback_query(F.data.startswith("poker_call_"))
async def poker_call(cb: CallbackQuery):
    game_uuid = cb.data.split("_")[2]
    user_id = cb.from_user.id

    if game_uuid not in active_poker_games:
        return await cb.answer("❌ Игра не найдена!", show_alert=True)

    game = active_poker_games[game_uuid]

    if game_uuid not in poker_locks:
        poker_locks[game_uuid] = asyncio.Lock()

    async with poker_locks[game_uuid]:
        if game.get("finished"):
            return await cb.answer("❌ Игра завершена!", show_alert=True)
        if user_id not in game.get("active_players", []):
            return await cb.answer("❌ Вы выбыли!", show_alert=True)
        if user_id in game.get("responses", set()):
            return await cb.answer("⏳ Уже ответили!", show_alert=True)

        stage = game["stage"]
        bet = game["bet"]
        stage_bet = bet if stage in ("preflop", "flop") else bet * 2

        ok, _ = await deduct_balance(user_id, stage_bet)
        if not ok:
            game["active_players"].remove(user_id)
            try:
                await cb.message.edit_text("❌ Нет монет! Авто-фолд.")
            except Exception:
                pass
        else:
            game["pot"] += stage_bet
            if "responses" not in game:
                game["responses"] = set()
            game["responses"].add(user_id)
            try:
                await cb.message.edit_text(
                    f"✅ Колл ({stage_bet}) | 💰 Банк: {game['pot']}\n⏳ Ждём...",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await cb.answer("Колл!")

    # Вне блокировки — чтобы не было дедлока
    await _check_poker_stage_end(game)

@dp.callback_query(F.data.startswith("poker_fold_"))
async def poker_fold(cb: CallbackQuery):
    game_uuid = cb.data.split("_")[2]
    user_id = cb.from_user.id

    if game_uuid not in active_poker_games:
        return await cb.answer("❌ Игра не найдена!", show_alert=True)

    game = active_poker_games[game_uuid]

    if game_uuid not in poker_locks:
        poker_locks[game_uuid] = asyncio.Lock()

    async with poker_locks[game_uuid]:
        if game.get("finished"):
            return await cb.answer("❌ Игра завершена!", show_alert=True)
        if user_id not in game.get("active_players", []):
            return await cb.answer("❌ Вы выбыли!", show_alert=True)

        game["active_players"].remove(user_id)
        if "responses" not in game:
            game["responses"] = set()
        game["responses"].add(user_id)
        try:
            await cb.message.edit_text("❌ Вы сбросили карты.", parse_mode="Markdown")
        except Exception:
            pass
        await cb.answer("Фолд!")

    # Вне блокировки
    await _check_poker_stage_end(game)
    
async def _check_poker_stage_end(game: dict):
    game_uuid = game.get("uuid")
    if not game_uuid:
        return

    if game_uuid not in poker_locks:
        poker_locks[game_uuid] = asyncio.Lock()

    async with poker_locks[game_uuid]:
        # Двойная защита: finished + transitioning
        if game.get("finished") or game.get("transitioning"):
            return

        active = game.get("active_players", [])
        responses = game.get("responses", set())

        if len(active) <= 1:
            game["finished"] = True
            await _poker_finish(game)
            poker_locks.pop(game_uuid, None)
            return

        if not all(uid in responses for uid in active):
            return

        # Ставим флаг ДО любых действий
        game["transitioning"] = True
        game["responses"] = set()
        stage = game.get("stage", "preflop")
        community = game.get("community", [])

        if stage == "preflop":
            game["stage"] = "flop"
            reveal_cards = community[:3]
            next_text = "📍 Флоп"
        elif stage == "flop":
            game["stage"] = "turn"
            reveal_cards = community[:4]
            next_text = "📍 Терн"
        elif stage == "turn":
            game["stage"] = "river"
            reveal_cards = community[:5]
            next_text = "📍 Ривер"
        elif stage == "river":
            game["finished"] = True
            active_poker_games.pop(game_uuid, None)
            await _poker_finish(game)
            poker_locks.pop(game_uuid, None)
            return
        else:
            game["finished"] = True
            active_poker_games.pop(game_uuid, None)
            await _poker_finish(game)
            poker_locks.pop(game_uuid, None)
            return

        
# 🔹 ПРОВЕРКА: если игра завершена — НЕ отправляем карты!
        if game.get("finished"):
            return

        reveal_text = " ".join([f"{c['rank']}{c['suit']}" for c in reveal_cards])
        btns = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📞 Колл", callback_data=f"poker_call_{game_uuid}"),
             InlineKeyboardButton(text="❌ Фолд", callback_data=f"poker_fold_{game_uuid}")]
        ])
        for uid in active:
            h = game["hands"][uid]
            hole_text = f"{h[0]['rank']}{h[0]['suit']}  {h[1]['rank']}{h[1]['suit']}"
            try:
                await bot.send_message(
                    uid,
                    f"🃏 **{next_text}**\n\n"
                    f"Ваши карты: {hole_text}\n"
                    f"Стол: {reveal_text}\n"
                    f"💰 Банк: {game['pot']}\n\nВыбирайте действие:",
                    reply_markup=btns,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        # Снимаем флаг после отправки
        game["transitioning"] = False
# 🔹 Запускаем таймер на ход для нового этапа
        asyncio.create_task(_poker_move_timer(game_uuid, game["stage"]))
async def _poker_move_timer(game_uuid: str, stage: str):
    """Таймер на ход: 30 секунд. Если игрок не ответил — авто-фолд."""
    await asyncio.sleep(30)

    if game_uuid not in active_poker_games:
        return

    game = active_poker_games[game_uuid]

    # Если игра уже завершилась или перешла на другой этап — выходим
    if game.get("finished") or game.get("stage") != stage:
        return

    active = game.get("active_players", [])
    responses = game.get("responses", set())

    # Находим всех кто НЕ ответил
    timed_out = [uid for uid in active if uid not in responses]

    if not timed_out:
        return  # Все ответили, таймер не нужен

    # Авто-фолд для каждого зависшего
    for uid in timed_out:
        if uid in game.get("active_players", []):
            game["active_players"].remove(uid)
            game.setdefault("responses", set()).add(uid)
            try:
                await bot.send_message(uid, "⏰ Время вышло! Авто-фолд.")
            except Exception:
                pass

    # Проверяем, что делать дальше
    await _check_poker_stage_end(game)

async def _poker_finish(game: dict):
    """Определяет победителя, показывает карты всех и раздаёт банк"""
    active = game.get("active_players", [])
    pot = game.get("pot", 0)
    community = game.get("community", [])
    hands = game.get("hands", {})
    chat_id = game.get("chat_id")
    msg_id = game.get("msg_id")
    players = game.get("players", [])

    # Формируем текст общих карт
    comm_text = " ".join([f"{c['rank']}{c['suit']}" for c in community])

    if len(active) == 1:
        # Все сбросили — один остался
        winner_id   = active[0]
        winner_name = next((p["username"] for p in players if p["user_id"] == winner_id), "???")
        h           = hands.get(winner_id, [])
        winner_cards_str = f"{h[0]['rank']}{h[0]['suit']} {h[1]['rank']}{h[1]['suit']}" if len(h) == 2 else "???"

        await add_winnings(winner_id, pot, pot, "poker")

        winner_info_obj = {
            "winner_ids":   [winner_id],
            "winner_names": [winner_name],
            "combo":        "Все сбросили",
            "pot":          pot,
        }
        result_text = (
            f"🃏 **ИГРА ОКОНЧЕНА!**\n\n"
            f"🎴 Стол: {comm_text}\n\n"
            f"🏆 @{winner_name} забрал **{pot}** 💰\n"
            f"📝 Все остальные сбросили\n\n"
            f"🃏 Карты победителя: {winner_cards_str}"
        )
    else:
        # Шоудаун — оцениваем руки всех активных
        results = []
        for uid in active:
            score, name = evaluate_hand(hands[uid], community)
            username = next((p["username"] for p in players if p["user_id"] == uid), "???")
            h = hands[uid]
            cards_str = f"{h[0]['rank']}{h[0]['suit']} {h[1]['rank']}{h[1]['suit']}"
            results.append({
                "score": score, "uid": uid, "username": username,
                "combo": name, "cards": cards_str
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        best_score = results[0]["score"]
        winners    = [r for r in results if r["score"] == best_score]
        share      = pot // len(winners)
        winner_names = ", ".join([f"@{w['username']}" for w in winners])
        combo_name   = winners[0]["combo"]

        for w in winners:
            await add_winnings(w["uid"], share, share, "poker")

        winner_info_obj = {
            "winner_ids":   [w["uid"] for w in winners],
            "winner_names": [w["username"] for w in winners],
            "combo":        combo_name,
            "pot":          pot,
        }

        players_text = ""
        for r in results:
            marker = "🏆" if r["score"] == best_score else "▪️"
            players_text += f"{marker} @{r['username']}: {r['cards']} ({r['combo']})\n"

        result_text = (
            f"🃏 **ИГРА ОКОНЧЕНА!**\n\n"
            f"🎴 Стол: {comm_text}\n\n"
            f"🏆 {winner_names} выиграли **{pot}** 💰\n"
            f"📝 Комбинация: **{combo_name}**\n\n"
            f"🃏 **Карты игроков:**\n{players_text}"
        )

    # Помечаем игру как завершённую (для мини-приложения)
    game['finished'] = True
    game['stage']    = 'finished'
    game['winner_info'] = winner_info_obj

    # Редактируем сообщение в чате
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=result_text,
            parse_mode="Markdown"
        )
    except Exception:
        pass

    # Уведомляем всех участников в ЛС
    for p in players:
        try:
            await bot.send_message(p["user_id"], result_text, parse_mode="Markdown")
        except Exception:
            pass

    # Очищаем стол из активных через 60 секунд
    await asyncio.sleep(60)
    active_poker_games.pop(game.get('uuid', ''), None)
    
@dp.message(Command("random", "выбрать", "розыгрыш"))
async def cmd_random(message: Message):
    chat_id = message.chat.id
    try:
        result = await pool.fetch('''
            SELECT user_id, COUNT(*) as message_count 
            FROM chat_messages 
            WHERE chat_id = $1 
            AND message_time > NOW() - INTERVAL '5 days'
            GROUP BY user_id
            HAVING COUNT(*) > 400
            ORDER BY message_count DESC
        ''', chat_id)
        
        if not result:
            await message.answer("😔 Никто из участников ещё не написал 400+ сообщений за последние 5 дней!")
            return
        
        winner = random.choice(result)
        winner_id = winner['user_id']
        winner_count = winner['message_count']
        
        try:
            user = await bot.get_chat_member(chat_id, winner_id)
            username = user.user.username or f"пользователь {winner_id}"
            full_name = user.user.full_name or username
        except:
            username = f"пользователь {winner_id}"
            full_name = username
        
        # Создаём ссылку на профиль
        user_link = f"[{username}](tg://user?id={winner_id})"
        
        await message.answer(
            f"🎉 **Случайный выбор!**\n\n"
            f"🏆 Победитель: {user_link} ({full_name})\n"
            f"📊 Написал сообщений за 5 дней: **{winner_count}**\n\n"
            f"Поздравляем! 🎊",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logging.error(f"Ошибка в /random: {e}")
        await message.answer("❌ Произошла ошибка при выборе победителя!")
@dp.message(Command("stats", "статистика"))
async def cmd_stats(message: Message):
    chat_id = message.chat.id
    try:
        result = await pool.fetch('''
            SELECT user_id, COUNT(*) as message_count
            FROM chat_messages
            WHERE chat_id = $1 
            AND message_time > NOW() - INTERVAL '5 days'
            GROUP BY user_id
            ORDER BY message_count DESC
            LIMIT 10
        ''', chat_id)
        
        if not result:
            await message.answer("📊 Статистика за последние 5 дней пуста!")
            return
        
        text = "🏆 **Топ участников по сообщениям (5 дней):**\n\n"
        for i, row in enumerate(result, 1):
            try:
                user = await bot.get_chat_member(chat_id, row['user_id'])
                username = user.user.username or f"id{row['user_id']}"
            except:
                username = f"id{row['user_id']}"
            
            # Создаём ссылку на профиль
            user_link = f"[{username}](tg://user?id={row['user_id']})"
            
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} {user_link} — **{row['message_count']}** сообщений\n"
        
        await message.answer(text, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Ошибка в /stats: {e}")
        await message.answer("❌ Ошибка при загрузке статистики!")
@dp.message(lambda m: m.text and not m.text.startswith('/'))
async def count_messages(message: Message):
    # Пропускаем команды
    if not message.text or message.text.startswith('/'):
        return
    # Пропускаем ботов
    if message.from_user.is_bot:
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id

    try:
        await pool.execute('''
            INSERT INTO chat_messages (chat_id, user_id, message_time)
            VALUES ($1, $2, NOW())
        ''', chat_id, user_id)
    except Exception as e:
        logging.error(f"Ошибка подсчёта сообщений: {e}")
# ============================================
# WEB API СЕРВЕР (для Telegram WebApp)
# ============================================
from aiohttp import web

async def handle_balance(request):
    """GET /api/balance/{user_id}"""
    user_id = int(request.match_info['user_id'])
    
    # Проверяем, есть ли пользователь. Если нет — создаем автоматически!
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, f"user_{user_id}")
        data = await get_user_data(user_id)
    
    if data:
        return web.json_response({'balance': data['balance']})
    return web.json_response({'error': 'User not found'}, status=500)

async def handle_stats(request):
    """GET /api/stats/{user_id}"""
    user_id = int(request.match_info['user_id'])
    
    # То же самое: авто-регистрация, если пользователя нет
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, f"user_{user_id}")

    async with pool.acquire() as conn:
        # Место в топе
        rank = await conn.fetchval(
            "SELECT COUNT(*) + 1 FROM users WHERE balance > (SELECT balance FROM users WHERE user_id = $1)",
            user_id
        ) or 1
        
        # Игр сыграно (все транзакции)
        total_games = await conn.fetchval(
            "SELECT COUNT(*) FROM transactions WHERE sender_id = $1 OR receiver_id = $1",
            user_id
        ) or 0
        
        # Выиграно монет
        total_won = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE receiver_id = $1 AND type LIKE '%_win'",
            user_id
        ) or 0

    return web.json_response({
        'rank': rank,
        'totalGames': total_games,
        'totalWon': total_won
    })
async def handle_top(request):
    """GET /api/top?limit=10"""
    limit = int(request.query.get('limit', 10))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT $1',
            limit
        )
    top = [{'username': r['username'], 'balance': r['balance']} for r in rows]
    return web.json_response(top)

async def handle_catalog(request):
    """GET /api/catalog"""
    items = await get_catalog()
    result = [
        {
            'id': r['id'],
            'name': r['name'],
            'description': r['description'],
            'price': r['price']
        }
        for r in items
    ]
    return web.json_response(result)

async def handle_achievements(request):
    """GET /api/achievements/{user_id}"""
    return web.json_response(['🏆 Первый выигрыш', '💰 Богач'])

async def handle_transfer(request):
    """POST /api/transfer"""
    data = await request.json()
    from_id = data['from_id']
    to_username = data['to_username'].replace('@', '')
    amount = data['amount']
    comment = data.get('comment', '')
    
    from_data = await get_user_data(from_id)
    if not from_data or from_data['balance'] < amount:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    async with pool.acquire() as conn:
        to_user = await conn.fetchrow('SELECT user_id FROM users WHERE username = $1', to_username)
    
    if not to_user:
        return web.json_response({'error': 'Пользователь не найден'}, status=404)
    
    await deduct_balance(from_id, amount)
    await add_winnings(to_user['user_id'], amount, amount, 'transfer')
    
    try:
        await bot.send_message(to_user['user_id'], f'💸 Тебе перевели {amount} монет\n📝 {comment}')
    except:
        pass
    
    return web.json_response({'success': True})

async def handle_create_table(request):
    """POST /api/create-table"""
    data = await request.json()
    # Здесь потом добавишь реальную логику создания стола
    return web.json_response({'success': True, 'message': 'Стол создан'})
async def handle_games(request):
    """GET /api/games"""
    games = [
        {'id': 'poker', 'name': 'Покер', 'description': 'Техасский Холдем', 'icon': '🃏'},
        {'id': 'durak', 'name': 'Дурак', 'description': 'Классический подкидной', 'icon': '🃏'},
        {'id': 'slots', 'name': 'Слоты', 'description': 'Игровой автомат', 'icon': '🎰'},
        {'id': 'roulette', 'name': 'Рулетка', 'description': 'Красное/Чёрное', 'icon': '🎯'},
        {'id': 'blackjack', 'name': 'Блэкджек', 'description': '21 очко', 'icon': '🎴'}
    ]
    return web.json_response(games)

async def handle_game_bet(request):
    """POST /api/game-bet - Списать ставку"""
    data = await request.json()
    user_id = data['user_id']
    amount = data['amount']
    game = data['game']
    
    success, new_balance = await deduct_balance(user_id, amount)
    if not success:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    await log_loss(user_id, amount, game)
    return web.json_response({'success': True, 'balance': new_balance})

async def handle_game_win(request):
    """POST /api/game-win - Начислить выигрыш"""
    data = await request.json()
    user_id = data['user_id']
    amount = data['amount']
    game = data['game']
    
    new_balance = await add_winnings(user_id, amount, amount, game)
    return web.json_response({'success': True, 'balance': new_balance})
    
# === СЛОТЫ НА PYTHON ===
async def handle_play_slots(request):
    data = await request.json()
    user_id = data['user_id']
    bet = data['amount']
    
    # 1. Списываем ставку
    success, _ = await deduct_balance(user_id, bet)
    if not success:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    # 2. Генерируем результат НА СЕРВЕРЕ
    symbols = ['🍒', '🍋', '🍊', '🍇', '💎', '7️⃣', '🔔']
    result = [random.choice(symbols) for _ in range(3)]
    
    # 3. Считаем выигрыш
    win = 0
    if result[0] == result[1] == result[2]:
        win = bet * 50 if result[0] == '7️⃣' else bet * 10
    elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        win = bet * 2
    
    # 4. Начисляем выигрыш если есть
    new_balance = 0
    if win > 0:
        new_balance = await add_winnings(user_id, win, bet, 'slots')
    else:
        await log_loss(user_id, bet, 'slots')
        user_data = await get_user_data(user_id)
        new_balance = user_data['balance']
    
    return web.json_response({
        'success': True,
        'result': result,
        'win': win,
        'balance': new_balance
    })

# === РУЛЕТКА НА PYTHON ===
async def handle_play_roulette(request):
    data = await request.json()
    user_id = data['user_id']
    bet = data['amount']
    choice = data['choice']       # 'red', 'black', 'green' или число
    bet_number = data.get('bet_number')  # если ставка на число
    
    success, _ = await deduct_balance(user_id, bet)
    if not success:        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    # Генерируем число НА СЕРВЕРЕ
    number = random.randint(0, 36)
    red_numbers = [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]
    
    if number == 0:
        color = 'green'
    elif number in red_numbers:
        color = 'red'
    else:
        color = 'black'
    
    win = 0
    if choice == 'number' and bet_number == number:
        win = bet * 36
    elif choice == color:
        win = bet * 14 if color == 'green' else bet * 2
    
    new_balance = 0
    if win > 0:
        new_balance = await add_winnings(user_id, win, bet, 'roulette')
    else:
        await log_loss(user_id, bet, 'roulette')
        user_data = await get_user_data(user_id)
        new_balance = user_data['balance']
    
    return web.json_response({
        'success': True,
        'number': number,
        'color': color,
        'win': win,
        'balance': new_balance
    })

# === БЛЭКДЖЕК НА PYTHON ===
# Для блэкджека нужно хранить состояние игры (колода, карты)
# Используем словарь в памяти (для одного пользователя)
blackjack_games = {}  # {user_id: {deck, player_hand, dealer_hand, bet}}

def create_deck_py():
    suits = ['♠️', '♥️', '♦️', '♣️']
    ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
    deck = []
    for s in suits:
        for r in ranks:
            val = int(r) if r.isdigit() else (11 if r == 'A' else 10)
            deck.append({'rank': r, 'suit': s, 'value': val})
    random.shuffle(deck)
    return deck
def calc_score_py(hand):
    score = sum(c['value'] for c in hand)
    aces = sum(1 for c in hand if c['rank'] == 'A')
    while score > 21 and aces > 0:
        score -= 10
        aces -= 1
    return score

async def handle_blackjack_start(request):
    data = await request.json()
    user_id = data['user_id']
    bet = data['amount']
    
    success, _ = await deduct_balance(user_id, bet)
    if not success:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    deck = create_deck_py()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]
    
    blackjack_games[user_id] = {
        'deck': deck,
        'player_hand': player_hand,
        'dealer_hand': dealer_hand,
        'bet': bet,
        'finished': False
    }
    
    p_score = calc_score_py(player_hand)
    
    # Если сразу 21 — автоматическая победа
    if p_score == 21:
        win = bet * 2
        new_balance = await add_winnings(user_id, win, bet, 'blackjack')
        del blackjack_games[user_id]
        return web.json_response({
            'success': True,
            'player_hand': player_hand,
            'dealer_hand': dealer_hand,
            'player_score': p_score,
            'dealer_score': calc_score_py(dealer_hand),
            'win': win,
            'balance': new_balance,
            'finished': True,
            'message': '🎉 Блэкджек! Вы выиграли!'
        })
    
    return web.json_response({        'success': True,
        'player_hand': player_hand,
        'dealer_card': dealer_hand[0],  # Показываем только одну карту дилера
        'player_score': p_score,
        'finished': False
    })

async def handle_blackjack_hit(request):
    data = await request.json()
    user_id = data['user_id']
    
    game = blackjack_games.get(user_id)
    if not game or game['finished']:
        return web.json_response({'error': 'Игра не найдена'}, status=400)
    
    card = game['deck'].pop()
    game['player_hand'].append(card)
    p_score = calc_score_py(game['player_hand'])
    
    if p_score > 21:
        game['finished'] = True
        await log_loss(user_id, game['bet'], 'blackjack')
        user_data = await get_user_data(user_id)
        del blackjack_games[user_id]
        return web.json_response({
            'success': True,
            'player_hand': game['player_hand'],
            'player_score': p_score,
            'win': 0,
            'balance': user_data['balance'],
            'finished': True,
            'message': '❌ Перебор! Вы проиграли.'
        })
    
    return web.json_response({
        'success': True,
        'player_hand': game['player_hand'],
        'player_score': p_score,
        'finished': False
    })

async def handle_blackjack_stand(request):
    data = await request.json()
    user_id = data['user_id']
    
    game = blackjack_games.get(user_id)
    if not game or game['finished']:
        return web.json_response({'error': 'Игра не найдена'}, status=400)
    
    # Дилер берёт карты пока < 17    while calc_score_py(game['dealer_hand']) < 17:
        game['dealer_hand'].append(game['deck'].pop())
    
    ps = calc_score_py(game['player_hand'])
    ds = calc_score_py(game['dealer_hand'])
    bet = game['bet']
    
    win = 0
    message = ''
    if ds > 21 or ps > ds:
        win = bet * 2
        message = '🎉 Вы выиграли!'
    elif ps == ds:
        win = bet  # Возврат ставки
        message = '🤝 Ничья!'
    else:
        message = '😔 Дилер выиграл!'
    
    game['finished'] = True
    if win > 0:
        new_balance = await add_winnings(user_id, win, bet, 'blackjack')
    else:
        await log_loss(user_id, bet, 'blackjack')
        user_data = await get_user_data(user_id)
        new_balance = user_data['balance']
    
    del blackjack_games[user_id]
    
    return web.json_response({
        'success': True,
        'player_hand': game['player_hand'],
        'dealer_hand': game['dealer_hand'],
        'player_score': ps,
        'dealer_score': ds,
        'win': win,
        'balance': new_balance,
        'finished': True,
        'message': message
    })
# Хранилище активных покер-столов
poker_tables = {}  # {table_id: {...}}
async def handle_create_poker_table(request):  # ← ЭТА СТРОКА ОБЯЗАТЕЛЬНА!
    """POST /api/poker/create"""
    data = await request.json()
    user_id = data['user_id']
    bet = data['bet']
    max_players = data.get('max_players', 2)
    
    chat_id = data.get('chat_id', 0)
    if chat_id == 0:
        chat_info = user_chat_context.get(user_id, {})
        chat_id = chat_info.get('chat_id', 0)
    if chat_id == 0:
        chat_id = user_id
    
    # Проверяем баланс
    success, _ = await deduct_balance(user_id, bet)
    if not success:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    host_data = await get_user_data(user_id)
    host_username = host_data['username'] if host_data else f"user_{user_id}"
    # Создаём стол
    table_id = str(uuid.uuid4())[:8]
    poker_tables[table_id] = {
        'host': user_id,
        'bet': bet,
        'max_players': max_players,
        'players': [{'user_id': user_id, 'username': host_username}],
        'chat_id': chat_id,
        'status': 'waiting',
        'created_at': time.time()
    }
    
    # ОТПРАВЛЯЕМ ПРИГЛАШЕНИЕ
# 🔹 ОТПРАВЛЯЕМ ПРИГЛАШЕНИЕ В ЧАТ
    webapp_url = "https://vopros00111111-cloud.github.io/Trollbotapp/"
    bot_username = (await bot.get_me()).username  # Получаем username бота

    invite_text = (
        f"🃏 **ПОКЕРНЫЙ СТОЛ**\n\n"
        f"👤 Игрок создал стол!\n"
        f"💰 Ставка: **{bet}** монет\n"
        f"👥 Игроков: **1/{max_players}**\n\n"
        f"Нажмите кнопку чтобы присоединиться!"
    )

    # 🔹 Кнопка ведёт в ЛС бота с параметром
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Присоединиться к покеру", url=f"https://t.me/{bot_username}?start=poker_{table_id}")]
    ])

    sent = False
    try:
        await bot.send_message(chat_id, invite_text, reply_markup=keyboard, parse_mode="Markdown")
        sent = True
        logging.info(f"✅ Приглашение отправлено в чат {chat_id}")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить в чат {chat_id}: {e}")
        # Фолбэк: если не вышло в чат — пробуем в ЛС создателю
        if chat_id != user_id:
            try:
                keyboard_ls = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎮 Присоединиться к покеру", url=f"https://t.me/{bot_username}?start=poker_{table_id}")]
                ])
                await bot.send_message(user_id, invite_text, reply_markup=keyboard_ls, parse_mode="Markdown")
                sent = True
                logging.info(f"✅ Приглашение отправлено в ЛС {user_id} (фолбэк)")
            except Exception as e2:
                logging.error(f"❌ Не удалось отправить в ЛС {user_id}: {e2}")

    if not sent:
    # Возвращаем деньги если не смогли отправить приглашение
        await add_balance(user_id, bet)
        return web.json_response({'error': 'Не удалось отправить приглашение. Убедитесь что бот добавлен в чат.'}, status=500)

    return web.json_response({'success': True, 'table_id': table_id})
async def handle_join_poker_table(request):
    """POST /api/poker/join"""
    data = await request.json()
    user_id = data['user_id']
    table_id = data['table_id']
    
    if table_id not in poker_tables:
        return web.json_response({'error': 'Стол не найден'}, status=404)
    
    table = poker_tables[table_id]
    
    if table['status'] != 'waiting':
        return web.json_response({'error': 'Игра уже началась'}, status=400)
    
    if len(table['players']) >= table['max_players']:
        return web.json_response({'error': 'Стол заполнен'}, status=400)
    
    # Проверяем баланс
    success, _ = await deduct_balance(user_id, table['bet'])
    if not success:
        return web.json_response({'error': 'Недостаточно монет'}, status=400)
    
    joined_data = await get_user_data(user_id)
    joined_username = joined_data['username'] if joined_data else f"user_{user_id}"
    table['players'].append({'user_id': user_id, 'username': joined_username})

    current_count = len(table['players'])
    max_count = table['max_players']

    if current_count >= max_count:
        table['status'] = 'started'
        game_entry = {
            'uuid':        table_id,
            'host':        table['host'],
            'bet':         table['bet'],
            'max_players': max_count,
            'players':     table['players'],
            'chat_id':     table.get('chat_id', 0),
            'msg_id':      table.get('invite_msg_id', 0),
            'status':      'started',
        }
        active_poker_games[table_id] = game_entry
        asyncio.create_task(_deal_poker_cards(game_entry))
        return web.json_response({'success': True, 'game_started': True})

    return web.json_response({
        'success': True, 'game_started': False,
        'players': current_count, 'max_players': max_count
    })
async def handle_get_poker_tables(request):
    """GET /api/poker/tables - Получить список активных столов"""
    tables = []
    for table_id, table in poker_tables.items():
        if table['status'] == 'waiting':
            # Получаем username хоста
            host_data = await get_user_data(table['host'])
            tables.append({
                'table_id': table_id,
                'host_username': host_data['username'] if host_data else 'Unknown',
                'bet': table['bet'],
                'max_players': table['max_players'],
                'players': len(table['players'])
            })
    return web.json_response(tables)

async def handle_get_poker_table(request):
    table_id = request.match_info['table_id']  # UUID — строка!
    if table_id not in poker_tables:
        return web.json_response({'error': 'Стол не найден'}, status=404)

    table  = poker_tables[table_id]
    game   = active_poker_games.get(table_id, table)

    try:
        requesting_uid = int(request.query.get('user_id', 0))
    except (ValueError, TypeError):
        requesting_uid = 0

    hands        = game.get('hands', {})
    my_cards     = hands.get(requesting_uid, [])
    last_actions = game.get('last_actions', {})

    enriched_players = []
    for p in table['players']:
        uid = p['user_id']
        user_data = await get_user_data(uid)
        username  = p.get('username') or (user_data['username'] if user_data else f"user_{uid}")
        action    = last_actions.get(uid, '')
        enriched_players.append({
            'user_id':  uid,
            'username': username,
            'chips':    p.get('chips', 0),
            'action':   action,
        })

    stage     = game.get('stage', 'preflop')
    community = game.get('community', [])
    if stage == 'preflop':
        visible_community = []
    elif stage == 'flop':
        visible_community = community[:3]
    elif stage == 'turn':
        visible_community = community[:4]
    else:
        visible_community = community[:5]

    # Финал — передаём карты всех + победителя
    winner_info = None
    showdown    = None
    if game.get('finished'):
        winner_info = game.get('winner_info')
        showdown_hands = {}
        for uid, cards in hands.items():
            uname = next((p['username'] for p in table['players'] if p['user_id'] == uid), str(uid))
            showdown_hands[str(uid)] = {'username': uname, 'cards': cards}
        showdown = showdown_hands

    return web.json_response({
        'success':         True,
        'table_id':        table_id,
        'host':            table['host'],
        'bet':             table['bet'],
        'max_players':     table['max_players'],
        'status':          table['status'],
        'stage':           stage,
        'players':         enriched_players,
        'pot':             game.get('pot', 0),
        'current_bet':     game.get('current_bet', 0),
        'community_cards': visible_community,
        'my_cards':        my_cards,
        'game_started':    table['status'] == 'started',
        'finished':        bool(game.get('finished')),
        'winner_info':     winner_info,
        'showdown':        showdown,
    })

async def handle_poker_action(request):
    """POST /api/poker/action"""
    data     = await request.json()
    user_id  = int(data.get('user_id', 0))
    table_id = data.get('table_id', '')
    action   = data.get('action', '')
    amount   = int(data.get('amount', 0))

    if table_id not in active_poker_games:
        return web.json_response({'error': 'Игра не найдена'}, status=404)
    game = active_poker_games[table_id]
    if game.get('finished'):
        return web.json_response({'error': 'Игра уже завершена'}, status=400)
    if user_id not in game.get('active_players', []):
        return web.json_response({'error': 'Вы выбыли из игры'}, status=400)
    if user_id in game.get('responses', set()):
        return web.json_response({'error': 'Вы уже сделали ход'}, status=400)

    bet       = game.get('bet', 0)
    stage     = game.get('stage', 'preflop')
    stage_bet = bet if stage in ('preflop', 'flop') else bet * 2

    if table_id not in poker_locks:
        poker_locks[table_id] = asyncio.Lock()

    async with poker_locks[table_id]:
        if action == 'fold':
            game['active_players'].remove(user_id)
            game.setdefault('responses', set()).add(user_id)
            game.setdefault('last_actions', {})[user_id] = 'fold'
        elif action == 'call':
            ok, _ = await deduct_balance(user_id, stage_bet)
            if not ok:
                game['active_players'].remove(user_id)
                game.setdefault('responses', set()).add(user_id)
                game.setdefault('last_actions', {})[user_id] = 'fold'
            else:
                game['pot'] = game.get('pot', 0) + stage_bet
                game.setdefault('responses', set()).add(user_id)
                game.setdefault('last_actions', {})[user_id] = 'call:' + str(stage_bet)
                if table_id in poker_tables:
                    poker_tables[table_id]['pot'] = game['pot']
        elif action == 'raise':
            raise_amount = max(amount, stage_bet + 1)
            ok, _ = await deduct_balance(user_id, raise_amount)
            if not ok:
                return web.json_response({'error': 'Недостаточно монет'}, status=400)
            game['pot']         = game.get('pot', 0) + raise_amount
            game['current_bet'] = raise_amount
            game['responses']   = {user_id}
            game.setdefault('last_actions', {})[user_id] = 'raise:' + str(raise_amount)
            if table_id in poker_tables:
                poker_tables[table_id]['pot']         = game['pot']
                poker_tables[table_id]['current_bet'] = raise_amount
        else:
            return web.json_response({'error': 'Неизвестное действие'}, status=400)

    asyncio.create_task(_check_poker_stage_end(game))
    return web.json_response({'success': True, 'pot': game.get('pot', 0), 'stage': stage})

async def handle_health(request):
    """Простая страница, чтобы Render не засыпал"""
    return web.Response(text="Trollcoin Bot is running!")
async def handle_get_poker_tables(request):
    """GET /api/poker/tables - Список активных столов"""
    tables = []
    for table_id, table in poker_tables.items():
        if table['status'] == 'waiting':
            host_data = await get_user_data(table['host'])
            tables.append({
                'table_id': table_id,
                'host_username': host_data['username'] if host_data else 'Unknown',
                'bet': table['bet'],
                'max_players': table['max_players'],
                'players': len(table['players'])
            })
    return web.json_response(tables)
# ============================================
# СОЗДАЁМ ВЕБ-ПРИЛОЖЕНИЕ
# ============================================
web_app = web.Application()

# Настраиваем CORS (чтобы GitHub Pages мог обращаться к Render)
from aiohttp_cors import setup as cors_setup, ResourceOptions

cors = cors_setup(web_app, defaults={
    "*": ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
        allow_methods=["GET", "POST", "OPTIONS"]
    )
})

# Добавляем ВСЕ роуты через cors.add()
cors.add(web_app.router.add_get('/api/balance/{user_id}', handle_balance))
cors.add(web_app.router.add_get('/api/stats/{user_id}', handle_stats))
cors.add(web_app.router.add_get('/api/top', handle_top))
cors.add(web_app.router.add_get('/api/catalog', handle_catalog))
cors.add(web_app.router.add_get('/api/achievements/{user_id}', handle_achievements))
cors.add(web_app.router.add_post('/api/transfer', handle_transfer))
cors.add(web_app.router.add_post('/api/create-table', handle_create_table))
cors.add(web_app.router.add_get('/api/games', handle_games))
cors.add(web_app.router.add_post('/api/game-bet', handle_game_bet))
cors.add(web_app.router.add_post('/api/game-win', handle_game_win))
# 🔹 ДОБАВЬ ЭТИ ДВЕ СТРОКИ:
cors.add(web_app.router.add_post('/api/poker/create', handle_create_poker_table))
cors.add(web_app.router.add_post('/api/poker/join', handle_join_poker_table))
cors.add(web_app.router.add_post('/api/game/slots', handle_play_slots))
cors.add(web_app.router.add_post('/api/game/roulette', handle_play_roulette))
cors.add(web_app.router.add_post('/api/game/blackjack/start', handle_blackjack_start))
cors.add(web_app.router.add_post('/api/game/blackjack/hit', handle_blackjack_hit))
cors.add(web_app.router.add_post('/api/game/blackjack/stand', handle_blackjack_stand))
cors.add(web_app.router.add_get('/api/poker/tables', handle_get_poker_tables))
cors.add(web_app.router.add_get('/api/poker/table/{table_id}', handle_get_poker_table))
    cors.add(web_app.router.add_post('/api/poker/action', handle_poker_action))
cors.add(web_app.router.add_get('/', handle_health))
cors.add(web_app.router.add_get('/health', handle_health))

async def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Web API server started on port {port}")
# ============================================
# ОСНОВНАЯ ФУНКЦИЯ
# ============================================    
async def main():
    await init_db()
    await start_web_server()
    logger.info("🤖 Запущен с PostgreSQL")
    
    # Запускаем фоновую очистку
    asyncio.create_task(periodic_cleanup())
    
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())
