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
    if not await check_admin(message.from_user.id): return
    
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: `/quiz <время> <ID>`. Пример: `/quiz 1h 5`", parse_mode="Markdown")
    
    time_str, quiz_id = args[1], args[2]
    # Парсим время
    seconds = 0
    if time_str.endswith("h"): seconds = int(time_str[:-1]) * 3600
    elif time_str.endswith("m"): seconds = int(time_str[:-1]) * 60
    else: return await message.answer("Время в формате 30m или 1h")
    
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", int(quiz_id))
        
    if not quiz:
        return await message.answer("Викторина с таким ID не найдена")
        
    # Обновляем статус
    async with pool.acquire() as conn:
        await conn.execute("UPDATE quizzes SET status = 'active', chat_id = $1, started_at = NOW(), time_limit_seconds = $2 WHERE id = $3", message.chat.id, seconds, int(quiz_id))
        
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"start_quiz_{quiz_id}")]])
    await message.answer(
        f"🏆 **ВИКТОРИНА #{quiz_id}**\n\n💰 Банк: **{quiz['prize_pool']}** монет\n⏱️ Время: **{time_str}**\n📝 Вопросов: {len(json.loads(quiz['questions']))}",
        reply_markup=btn, parse_mode="Markdown"
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
    await cb.bot.send_message(user_id, "🎮 **Викторина началась!**\nОтвечай на вопросы...", parse_mode="Markdown")
    await send_quiz_question(cb.bot, user_id, quiz_id, 0)
    await cb.answer()

# 9. ОТПРАВКА ВОПРОСА (В ЛС)
async def send_quiz_question(bot, user_id, quiz_id, q_index):
    async with pool.acquire() as conn:
        # Выбираем ВСЁ (*)
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", quiz_id)
        
        if not quiz or quiz['status'] != 'active': 
            return
        
        # questions — это список словарей (asyncpg парсит JSONB сам, но иногда возвращает строку)
        # На всякий случай проверим тип
        questions = quiz['questions']
        if isinstance(questions, str):
            import json
            questions = json.loads(questions)
        
        if q_index >= len(questions):
            await bot.send_message(user_id, "✅ **Все вопросы пройдены!**\nЖди результатов в чате.", parse_mode="Markdown")
            return
            
        q = questions[q_index]
        
        # Теперь q — это словарь {'text': '...', 'options': [...], 'correct': 0}
        options = q['options']
        
        # Кнопки с ответами
        btns = [[InlineKeyboardButton(text=opt, callback_data=f"ans_{quiz_id}_{q_index}_{i}")] for i, opt in enumerate(options)]
        
        await bot.send_message(
            user_id, 
            f"❓ **Вопрос {q_index + 1}/{len(questions)}**\n\n{q['text']}", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
            parse_mode="Markdown"
        )

# 10. ОБРАБОТКА ОТВЕТА (В ЛС)
@dp.callback_query(F.data.startswith("ans_"))
async def process_answer(cb: CallbackQuery):
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
    await send_quiz_question(cb.bot, cb.from_user.id, quiz_id, q_idx + 1)
# 11. ФИНАЛ И НАГРАДЫ
async def finish_quiz_task(quiz_id, delay):
    await asyncio.sleep(delay)
    
    async with pool.acquire() as conn:
        # Блокируем викторину
        await conn.execute("UPDATE quizzes SET status = 'finished' WHERE id = $1", quiz_id)
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", quiz_id)
        
        if not quiz: return
        
        # Считаем очки: 1 за правильный ответ. Сортируем по очкам DESC.
        # (Упрощенная логика: считаем просто кол-во правильных)
        results = await conn.fetch('''
            SELECT user_id, COUNT(*) FILTER (WHERE is_correct = true) as score 
            FROM quiz_answers 
            WHERE quiz_id = $1 
            GROUP BY user_id 
            ORDER BY score DESC 
            LIMIT 3
        ''', quiz_id)
        
        text = f"🏁 **ВИКТОРИНА #{quiz_id} ЗАВЕРШЕНА!**\n\n"
        if not results:
            text += "Никто не участвовал "
        else:
            prize = quiz['prize_pool']
            distribution = [0.5, 0.3, 0.2] # 50%, 30%, 20%
            
            for i, row in enumerate(results):
                uid = row['user_id']
                score = row['score']
                reward = int(prize * distribution[i])
                
                # Выдаем монеты
                await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", reward, uid)
                
                username = await conn.fetchval("SELECT username FROM users WHERE user_id = $1", uid)
                medals = ["🥇", "🥈", "🥉"]
                text += f"{medals[i]} {username or uid}: {score} правильных → **+{reward} монет**\n"
        
        # Отправляем в чат
        try:
            await bot.send_message(quiz['chat_id'], text, parse_mode="Markdown")
        except:
            pass

async def main():
    await init_db()
    logger.info("🤖 Запущен с PostgreSQL")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
