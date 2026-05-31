import asyncio
import logging
import os
from datetime import datetime, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from flask import Flask
import threading
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import random
import math

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

# 🔹 Словарь для хранения активных таймеров
active_timers = {}
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
        "📈 /crash [ставка] — Крэш\n\n"
        "⚠️ Шанс есть всегда, но удача любит смелых!"
    )
    await message.answer(text, parse_mode="Markdown")
    
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
    text += "/casino — казино\n"
    text += "/help — полная справка"
    await message.answer(text, parse_mode="Markdown")

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
# === ИГРА: СЛОТЫ ===
@dp.message(Command("slots", "слоты"))
async def cmd_slots(message: Message):
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

    symbols = ["🍒", "", "🍉", "", "💎", "7️", "🔔"]
    
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
    args = message.text.split()
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
    args = message.text.split()
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
# === ИГРА: КРЭШ ===
active_crash_games = {}

@dp.message(Command("crash", "крэш"))
async def cmd_crash(message: Message):
    args = message.text.split()
    if len(args) < 2: return await message.answer("📈 Формат: `/crash [ставка]`")
    try:
        bet = int(args[1])
        if bet < 10: return await message.answer("Мин. ставка 10")
    except: return await message.answer("Ошибка ставки")

    user_id = message.from_user.id
    ok, _ = await deduct_balance(user_id, bet)
    if not ok: return await message.answer("❌ Недостаточно монет!")

    # Определяем точку краша заранее
    crash_point = round(random.uniform(1.0, 3.0), 2)
    if random.random() < 0.1: crash_point = round(random.uniform(3.0, 10.0), 2)

    active_crash_games[user_id] = {"status": "running", "crash_point": crash_point, "bet": bet}
    
    msg = await message.answer(f"📈 **КРЭШ** (Ставка: {bet})\n\nМножитель: **x1.00**")

    current_mult = 1.00
    step = 0.10
    
    while True:
        if user_id not in active_crash_games:
            return
        
        game = active_crash_games[user_id]
        if game["status"] != "running":
            return
        
        current_mult += step
        step += 0.02
        
        btns = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"💰 ЗАБРАТЬ x{current_mult:.2f}", callback_data=f"crash_out_{current_mult:.2f}")
        ]])
        
        try:
            await msg.edit_text(
                f"📈 **КРЭШ** (Ставка: {bet})\n\nМножитель: **x{current_mult:.2f}**",
                reply_markup=btns
            )
        except:
            pass
            
        if current_mult >= crash_point:
            active_crash_games.pop(user_id, None)
            await log_loss(user_id, bet, "crash")
            await msg.edit_text(
                f"📉 **КРАШ!** 💥\n\nМножитель: **x{current_mult:.2f}**\n\n💸 Вы не успели! Проигрыш: {bet}",
                parse_mode="Markdown"
            )
            break
        
        await asyncio.sleep(0.5)

@dp.callback_query(F.data.startswith("crash_out_"))
async def crash_cashout(cb: CallbackQuery):
    user_id = cb.from_user.id
    if user_id not in active_crash_games:
        return await cb.answer("Игра уже завершена!", show_alert=True)
    
    game = active_crash_games[user_id]
    if game["status"] != "running":
        return await cb.answer("Игра уже завершена!", show_alert=True)

    win_mult = float(cb.data.split("_")[2])
    bet = game["bet"]
    win_amount = int(bet * win_mult)
    
    game["status"] = "won"
    active_crash_games.pop(user_id, None)
    
    final_bal = await add_winnings(user_id, win_amount, bet, "crash")
    
    await cb.message.edit_text(
        f"💰 **ВЫ ЗАБРАЛИ!**\n\nМножитель: **x{win_mult:.2f}**\nВыигрыш: **+{win_amount}**\nБаланс: {final_bal}",
        parse_mode="Markdown"
    )
    await cb.answer(f"Вы забрали {win_amount}!")
# === СОЦИАЛКА: ТОП И ПРОФИЛЬ ===
@dp.message(Command("top", "топ"))
async def cmd_top(message: Message):
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT username, balance FROM users WHERE balance > 0 ORDER BY balance DESC LIMIT 10')
    
    if not rows:
        return await message.answer("🏆 Топ пока пуст. Стань первым!")
    
    text = "🏆 **ТОП-10 ИГРОКОВ**\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        text += f"{prefix} @{row['username']} — **{row['balance']}** 💰\n"
    
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

# Множители для сапёра (зависят от кол-ва мин и открытых клеток)
def get_mines_multiplier(mines_count: int, opened: int):
    """Рассчитывает множитель на основе вероятности"""
    if opened == 0:
        return 1.0
    total_cells = 25
    safe_cells = total_cells - mines_count
    multiplier = 1.0
    for i in range(opened):
        probability = (safe_cells - i) / (total_cells - i)
        multiplier *= (1 / probability)
    # Добавляем маржу казино 3%
    return round(multiplier * 0.97, 2)


@dp.message(Command("mines", "сапёр"))
async def cmd_mines_start(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("💣 Формат: `/mines [ставка] [кол-во мин 1-24]`\nПример: `/mines 100 5`")
    
    try:
        bet = int(args[1])
        mines_count = int(args[2])
        if bet < 10: return await message.answer("❌ Мин. ставка 10")
        if not (1 <= mines_count <= 24): return await message.answer("❌ Мины: от 1 до 24")
    except ValueError:
        return await message.answer("❌ Неверный формат")
    
    user_id = message.from_user.id
    ok, _ = await deduct_balance(user_id, bet)
    if not ok:
        return await message.answer("❌ Недостаточно монет!")
    
    # Генерируем поле: 0 = безопасно, 1 = мина
    field = [0] * 25
    mine_positions = random.sample(range(25), mines_count)
    for pos in mine_positions:
        field[pos] = 1
    
    await state.set_data({
        "bet": bet,
        "mines_count": mines_count,
        "field": field,
        "opened": [],
        "status": "playing"    })
    await state.set_state(MinesStates.playing)
    
    # Строим клавиатуру 5x5
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬜", callback_data=f"mine_{i}") for i in range(row*5, row*5+5)]
        for row in range(5)
    ])
    
    current_mult = get_mines_multiplier(mines_count, 0)
    text = (
        f"💣 **САПЁР** (Ставка: {bet})\n"
        f"Мин на поле: **{mines_count}**\n"
        f"Текущий множитель: **x{current_mult}**\n\n"
        f"Нажимай на клетки! Найди алмаз 💎 или мину 💣"
    )
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(MinesStates.playing, F.data.startswith("mine_"))
async def process_mines(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    cell_index = int(cb.data.split("_")[1])
    
    data = await state.get_data()
    if data["status"] != "playing":
        return await cb.answer("Игра завершена!", show_alert=True)
    
    if cell_index in data["opened"]:
        return await cb.answer("Эта клетка уже открыта!")
    
    field = data["field"]
    bet = data["bet"]
    mines_count = data["mines_count"]
    opened = data["opened"] + [cell_index]
    
    # Проверка: мина или алмаз?
    if field[cell_index] == 1:
        # БОМБА!
        await log_loss(user_id, bet, "mines")
        
        # Показываем всё поле
        reveal_text = ""
        for i in range(25):
            if field[i] == 1:
                reveal_text += "💣"
            elif i in opened:
                reveal_text += "💎"
            else:
                reveal_text += "⬜"
                if (i + 1) % 5 == 0:
                    reveal_text += "\n"
        
        text = (
            f"💥 **БОМБА!**\n\n{reveal_text}\n"
            f"Ты проиграл **{bet}** монет."
        )
        await cb.message.edit_text(text, parse_mode="Markdown")
        await state.clear()
        await cb.answer("💣 Бум!")
    else:
        # АЛМАЗ! Продолжаем
        current_mult = get_mines_multiplier(mines_count, len(opened))
        potential_win = int(bet * current_mult)
        
        await state.update_data(opened=opened)
        
        # Обновляем клавиатуру: открытые клетки меняются на 💎
        kb_rows = []
        for row in range(5):
            row_btns = []
            for col in range(5):
                idx = row * 5 + col
                if idx in opened:
                    row_btns.append(InlineKeyboardButton(text="💎", callback_data=f"mine_{idx}"))
                else:
                    row_btns.append(InlineKeyboardButton(text="⬜", callback_data=f"mine_{idx}"))
            kb_rows.append(row_btns)
        
        # Добавляем кнопку "Забрать"
        kb_rows.append([InlineKeyboardButton(
            text=f"💰 ЗАБРАТЬ {potential_win}", 
            callback_data="mine_cashout"
        )])
        
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        
        text = (
            f"💣 **САПЁР** (Ставка: {bet})\n"
            f"Мин на поле: **{mines_count}**\n"
            f"Открыто: **{len(opened)}** | Множитель: **x{current_mult}**\n"
            f"Потенциальный выигрыш: **{potential_win}**\n\n"
            f"Продолжить или забрать?"
        )
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await cb.answer(f"💎 x{current_mult}")


@dp.callback_query(MinesStates.playing, F.data == "mine_cashout")
async def mines_cashout(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    data = await state.get_data()
    
    if data["status"] != "playing":
        return await cb.answer("Игра завершена!", show_alert=True)
    
    bet = data["bet"]
    mines_count = data["mines_count"]
    opened = data["opened"]
    current_mult = get_mines_multiplier(mines_count, len(opened))
    win_amount = int(bet * current_mult)
    
    final_bal = await add_winnings(user_id, win_amount, bet, "mines")
    
    # Показываем где были мины
    field = data["field"]
    reveal_text = ""
    for i in range(25):
        if field[i] == 1:
            reveal_text += "💣"
        elif i in opened:
            reveal_text += "💎"
        else:
            reveal_text += "⬛"
        if (i + 1) % 5 == 0:
            reveal_text += "\n"
    
    text = (
        f"💰 **ТЫ ЗАБРАЛ!**\n\n{reveal_text}\n"
        f"Множитель: **x{current_mult}**\n"
        f"Выигрыш: **+{win_amount}**\n"
        f"Баланс: **{final_bal}**"
    )
    await cb.message.edit_text(text, parse_mode="Markdown")
    await state.clear()
    await cb.answer(f"Забрал {win_amount}!")

async def main():
    await init_db()
    logger.info("🤖 Запущен с PostgreSQL")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
