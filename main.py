import asyncio
import logging
import os
import json
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

pool = None
active_timers = {}
QUIZ_TIME_LIMIT = 15

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0,
            last_claim TEXT, is_admin INTEGER DEFAULT 0)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY, sender_id BIGINT, receiver_id BIGINT,
            amount INTEGER, type TEXT, created_at TIMESTAMP DEFAULT NOW())''')        await conn.execute('''CREATE TABLE IF NOT EXISTS catalog (
            id SERIAL PRIMARY KEY, name TEXT, description TEXT, price INTEGER, image_url TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS quizzes (
            id SERIAL PRIMARY KEY, chat_id BIGINT, message_id INTEGER,
            prize_pool INTEGER, time_limit_seconds INTEGER, questions JSONB,
            created_by BIGINT, status TEXT DEFAULT 'waiting', started_at TIMESTAMP)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS quiz_answers (
            quiz_id INTEGER, user_id BIGINT, question_index INTEGER,
            is_correct BOOLEAN, response_time_sec INTEGER, started_at TIMESTAMP,
            PRIMARY KEY (quiz_id, user_id, question_index))''')
    logger.info("✅ DB ready")

async def register_user(user_id: int, username: str):
    clean_username = username.replace("@", "") if username else f"user_{user_id}"
    async with pool.acquire() as conn:
        await conn.execute('INSERT INTO users (user_id, username, balance, is_admin) VALUES ($1, $2, 0, 0) ON CONFLICT (user_id) DO NOTHING', user_id, clean_username)
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
        await conn.execute('INSERT INTO catalog (name, description, price, image_url) VALUES ($1, $2, $3, $4)', name, description, price, image_url)

async def remove_from_catalog(item_id: int):
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM catalog WHERE id = $1', item_id)

async def get_catalog():
    async with pool.acquire() as conn:
        return await conn.fetch('SELECT id, name, description, price, image_url FROM catalog')

def build_quiz_kb(options: list):
    btns = [[InlineKeyboardButton(text=f"{i+1}. {opt}", callback_data=f"ans_{i}")] for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=btns)

@dp.message(Command("start"))
async def cmd_start(message: Message):    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    await register_user(user_id, username)
    text = "👋 Привет! Я BlessCoin Bot.\n\n📋 Команды:\n/balance — баланс\n/claim — награда\n/transfer @user сумма — перевод\n/catalog — товары\n/help — справка"
    await message.answer(text)

@dp.message(Command("history", "logs", "история"))
async def cmd_history(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒 Только админ")
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT * FROM transactions ORDER BY id DESC LIMIT 20')
    if not rows:
        return await message.answer(" Пусто")
    text = " ИСТОРИЯ:\n\n"
    for row in rows:
        t_type = row['type']
        desc = "💸 Перевод" if t_type == "transfer" else "➕ Выдача" if t_type == "admin_add" else "➖ Списание"
        text += f"⏰ {row['created_at'].strftime('%d.%m %H:%M')} | {desc}: {row['amount']} | От: {row['sender_id']} -> К: {row['receiver_id']}\n"
    await message.answer(text)

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    parts = message.text.split()
    if len(parts) > 1:
        target = parts[1].replace("@", "")
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT username, balance FROM users WHERE username = $1", target)
        if not row:
            return await message.answer(f"❌ @{target} не найден")
        return await message.answer(f"💰 @{row['username']}: **{row['balance']}**")
    data = await get_user_data(message.from_user.id)
    if not data:
        return await cmd_start(message)
    await message.answer(f"💰 Твой баланс: **{data['balance']}**")

@dp.message(Command("claim"))
async def cmd_claim(message: Message):
    user_id = message.from_user.id
    data = await get_user_data(user_id)
    if not data:
        await register_user(user_id, message.from_user.username)
        data = await get_user_data(user_id)
    now = datetime.utcnow()
    if data['last_claim']:
        last = datetime.fromisoformat(data['last_claim'])
        if now - last < timedelta(hours=24):
            wait = timedelta(hours=24) - (now - last)
            h, m = divmod(int(wait.total_seconds()) // 60, 60)
            return await message.answer(f"⏳ Жди {h}ч {m}м")    await add_balance(user_id, 10)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_claim = $1 WHERE user_id = $2", now.isoformat(), user_id)
    await message.answer(f"🎁 +10! Баланс: **{data['balance']+10}**")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(" /start /balance /claim /transfer @user сумма /catalog /help\n👑 /givemoney /takemoney /additem /removeitem /addadmin /removeadmin")

@dp.message(Command("transfer"))
async def cmd_transfer(message: Message):
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/transfer @user сумма")
    try:
        target, amount = parts[1].replace("@", ""), int(parts[2])
        if amount <= 0:
            return await message.answer("Сумма > 0")
        sender_data = await get_user_data(message.from_user.id)
        if not sender_data or sender_data['balance'] < amount:
            return await message.answer("Недостаточно средств")
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', target)
            if not t or t['user_id'] == message.from_user.id:
                return await message.answer("Ошибка получателя")
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, message.from_user.id)
            await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, t['user_id'])
            await conn.execute('INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)', message.from_user.id, t['user_id'], amount, 'transfer')
        await message.answer(f"✅ Переведено {amount} @{t['username']}")
        try:
            await bot.send_message(t['user_id'], f"💸 Тебе перевели {amount}!")
        except:
            pass
    except Exception as e:
        await message.answer(f"❌ {e}")

@dp.message(Command("catalog"))
async def cmd_catalog(message: Message):
    items = await get_catalog()
    if not items:
        return await message.answer("Пусто")
    await message.answer("📦 " + "\n".join(f"{i['name']} - {i['price']} монет" for i in items))

@dp.message(Command("additem"))
async def cmd_additem(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("/additem Название|Описание|Цена")    try:
        d = parts[1].split("|")
        if len(d) < 3:
            return await message.answer("❌ Формат")
        await add_to_catalog(d[0].strip(), d[1].strip(), int(d[2].strip()), d[3].strip() if len(d) > 3 else None)
        await message.answer(f"✅ Добавлен {d[0]}")
    except:
        await message.answer("❌ Ошибка")

@dp.message(Command("removeitem"))
async def cmd_removeitem(message: Message):
    if not await check_admin(message.from_user.id):
        return
    try:
        await remove_from_catalog(int(message.text.split()[1]))
        await message.answer("✅")
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
        target, amount = parts[1].replace("@", ""), int(parts[2])
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', target)
        if not t:
            return await message.answer("Не найден")
        await add_balance(t['user_id'], amount)
        async with pool.acquire() as conn:
            await conn.execute('INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)', message.from_user.id, t['user_id'], amount, 'admin_add')
        await message.answer(f"✅ Выдано {amount} @{t['username']}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@dp.message(Command("takemoney"))
async def cmd_takemoney(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒")
    parts = message.text.split()
    if len(parts) != 3:
        return await message.answer("/takemoney @user сумма")
    try:
        target, amount = parts[1].replace("@", ""), int(parts[2])
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username, balance FROM users WHERE username = $1', target)        if not t or t['balance'] < amount:
            return await message.answer("Ошибка")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET balance = balance - $1 WHERE user_id = $2', amount, t['user_id'])
            await conn.execute('INSERT INTO transactions (sender_id, receiver_id, amount, type) VALUES ($1, $2, $3, $4)', message.from_user.id, t['user_id'], amount, 'admin_remove')
        await message.answer(f"✅ Списано {amount} у @{t['username']}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@dp.message(Command("addadmin"))
async def cmd_addadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("")
    try:
        target = message.text.split()[1].replace("@", "")
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username FROM users WHERE username = $1', target)
        if not t:
            return await message.answer("❌ Не найден")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_admin = 1 WHERE user_id = $1', t['user_id'])
        await message.answer(f"✅ @{t['username']} админ")
        try:
            await bot.send_message(t['user_id'], "👑 Ты админ!")
        except:
            pass
    except Exception as e:
        await message.answer(f"❌ {e}")

@dp.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message):
    if not await check_admin(message.from_user.id):
        return await message.answer("🔒")
    try:
        target = message.text.split()[1].replace("@", "")
        async with pool.acquire() as conn:
            t = await conn.fetchrow('SELECT user_id, username FROM users WHERE username = $1', target)
        if not t:
            return await message.answer("❌")
        async with pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_admin = 0 WHERE user_id = $1', t['user_id'])
        await message.answer(f"✅ @{t['username']} снят")
    except Exception as e:
        await message.answer(f"❌ {e}")
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import json

class QuizCreateStates(StatesGroup):
    waiting_for_question = State()
    waiting_for_options = State()
    waiting_for_correct = State()
    asking_more = State()
    waiting_for_prize = State()

temp_quizzes = {}

@dp.message(Command("create_quiz"))
async def start_create_quiz(message: Message, state: FSMContext):
    if not await check_admin(message.from_user.id):
        return
    temp_quizzes[message.from_user.id] = {"questions": []}
    await message.answer("🎬 Вопрос №1:")
    await state.set_state(QuizCreateStates.waiting_for_question)

@dp.message(QuizCreateStates.waiting_for_question)
async def get_question(message: Message, state: FSMContext):
    temp_quizzes[message.from_user.id]["questions"].append({"text": message.text, "options": [], "correct": 0})
    await message.answer("Варианты через `|`:\nПример: `Синий|Красный|Зеленый`")
    await state.set_state(QuizCreateStates.waiting_for_options)

@dp.message(QuizCreateStates.waiting_for_options)
async def get_options(message: Message, state: FSMContext):
    opts = [o.strip() for o in message.text.split("|") if o.strip()]
    if len(opts) < 2:
        return await message.answer("Минимум 2 варианта!")
    temp_quizzes[message.from_user.id]["questions"][-1]["options"] = opts
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"✅ {i+1}. {o}")] for i, o in enumerate(opts)], resize_keyboard=True)
    await message.answer("Какой правильный? (нажми кнопку)", reply_markup=kb)
    await state.set_state(QuizCreateStates.waiting_for_correct)

@dp.message(QuizCreateStates.waiting_for_correct)
async def get_correct(message: Message, state: FSMContext):
    opts = temp_quizzes[message.from_user.id]["questions"][-1]["options"]
    idx = next((i for i, o in enumerate(opts) if o.strip() in message.text), -1)
    if idx == -1:
        return await message.answer("Нажми кнопку из списка.")
    temp_quizzes[message.from_user.id]["questions"][-1]["correct"] = idx
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="➕ Да"), KeyboardButton(text="Нет, завершить")]], resize_keyboard=True)
    await message.answer("Ещё вопрос?", reply_markup=kb)
    await state.set_state(QuizCreateStates.asking_more)

@dp.message(QuizCreateStates.asking_more)
async def ask_more(message: Message, state: FSMContext):    if message.text.lower() in ["да", "➕ да"]:
        n = len(temp_quizzes[message.from_user.id]["questions"]) + 1
        await message.answer(f"Вопрос №{n}:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(QuizCreateStates.waiting_for_question)
    else:
        await message.answer("Призовой фонд (монеты):", reply_markup=ReplyKeyboardRemove())
        await state.set_state(QuizCreateStates.waiting_for_prize)

@dp.message(QuizCreateStates.waiting_for_prize)
async def finish_create_quiz(message: Message, state: FSMContext):
    try:
        prize = int(message.text)
    except:
        return await message.answer("Это не число!")
    data = temp_quizzes[message.from_user.id]
    async with pool.acquire() as conn:
        row = await conn.fetchrow('INSERT INTO quizzes (chat_id, prize_pool, questions, created_by) VALUES (0, $1, $2, $3) RETURNING id', prize, json.dumps(data["questions"]), message.from_user.id)
    await message.answer(f"✅ Создана! ID: **{row['id']}**\nЗапуск: `/quiz 1h {row['id']}`")
    await state.clear()
    del temp_quizzes[message.from_user.id]

@dp.message(Command("quiz"))
async def publish_quiz(message: Message):
    if not await check_admin(message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3:
        return await message.answer("Формат: `/quiz 1h 5`")
    time_str, qid = args[1], args[2]
    sec = int(time_str[:-1]) * 3600 if time_str.endswith("h") else int(time_str[:-1]) * 60 if time_str.endswith("m") else 0
    if sec == 0:
        return await message.answer("Время: 30m или 1h")
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id = $1", int(qid))
    if not quiz:
        return await message.answer("Не найдена")
    btn = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"start_quiz_{qid}")]])
    sent = await message.answer(f"🏆 ВИКТОРИНА #{qid}\n💰 Банк: {quiz['prize_pool']}\n️ {time_str}\n📝 {len(json.loads(quiz['questions']))} вопросов", reply_markup=btn)
    try:
        await sent.pin()
    except:
        pass
    async with pool.acquire() as conn:
        await conn.execute("UPDATE quizzes SET status='active', chat_id=$1, started_at=NOW(), time_limit_seconds=$2, message_id=$3 WHERE id=$4", message.chat.id, sec, sent.message_id, int(qid))
    asyncio.create_task(finish_quiz_task(int(qid), sec))

@dp.callback_query(F.data.startswith("start_quiz_"))
async def quiz_click(cb: CallbackQuery):
    qid = int(cb.data.split("_")[2])
    uid = cb.from_user.id    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT COUNT(*) FROM quiz_answers WHERE quiz_id=$1 AND user_id=$2", qid, uid) > 0:
            return await cb.answer("Ты уже участвуешь!", show_alert=True)
    await cb.bot.send_message(uid, " Поехали!")
    await send_quiz_question(uid, qid, 0)
    await cb.answer()

async def send_quiz_question(user_id: int, quiz_id: int, q_index: int = 0):
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT questions FROM quizzes WHERE id = $1", quiz_id)
    if not quiz:
        return await bot.send_message(user_id, "❌")
    questions = json.loads(quiz['questions']) if isinstance(quiz['questions'], str) else quiz['questions']
    if q_index >= len(questions):
        async with pool.acquire() as conn:
            score = await conn.fetchval("SELECT COUNT(*) FROM quiz_answers WHERE quiz_id=$1 AND user_id=$2 AND is_correct=True", quiz_id, user_id)
        await bot.send_message(user_id, f"✅ Готово!\nРезультат: {score}/{len(questions)}")
        return
    q = questions[q_index]
    text = f"**Вопрос {q_index+1}/{len(questions)}**\n\n{q['text']}"
    msg = await bot.send_message(user_id, text, reply_markup=build_quiz_kb(q['options']), parse_mode="Markdown")
    async def time_is_up():
        try:
            await asyncio.sleep(QUIZ_TIME_LIMIT)
            await bot.edit_message_text(chat_id=user_id, message_id=msg.message_id, text=f"{text}\n\n⏰ Время вышло!", reply_markup=None, parse_mode="Markdown")
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO quiz_answers (quiz_id, user_id, question_index, is_correct, response_time_sec, started_at) VALUES ($1,$2,$3,$4,$5,NOW())", quiz_id, user_id, q_index, False, QUIZ_TIME_LIMIT)
            await asyncio.sleep(1.5)
            await send_quiz_question(user_id, quiz_id, q_index+1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Timer: {e}")
    timer = asyncio.create_task(time_is_up())
    active_timers[(user_id, quiz_id, q_index)] = timer

@dp.callback_query(F.data.startswith("ans_"))
async def process_answer(cb: CallbackQuery):
    uid = cb.from_user.id
    _, qid, qidx, ansidx = cb.data.split("_")
    qid, qidx, ansidx = int(qid), int(qidx), int(ansidx)
    key = (uid, qid, qidx)
    if key in active_timers:
        active_timers[key].cancel()
        del active_timers[key]
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT questions FROM quizzes WHERE id=$1", qid)
        questions = json.loads(quiz['questions']) if isinstance(quiz['questions'], str) else quiz['questions']
        is_correct = (questions[qidx]['correct'] == ansidx)
        await conn.execute("INSERT INTO quiz_answers (quiz_id, user_id, question_index, is_correct, response_time_sec, started_at) VALUES ($1,$2,$3,$4,$5,NOW())", qid, uid, qidx, is_correct, 0)    await cb.answer("Принят!")
    await cb.message.delete()
    await send_quiz_question(uid, qid, qidx+1)

async def finish_quiz_task(quiz_id, delay):
    await asyncio.sleep(delay)
    async with pool.acquire() as conn:
        quiz = await conn.fetchrow("SELECT * FROM quizzes WHERE id=$1", quiz_id)
        if not quiz or quiz['status'] == 'finished':
            return
        await conn.execute("UPDATE quizzes SET status='finished' WHERE id=$1", quiz_id)
        results = await conn.fetch('''SELECT user_id, COUNT(*) FILTER (WHERE is_correct=true) as score, EXTRACT(EPOCH FROM (MAX(started_at)-MIN(started_at))) as dur FROM quiz_answers WHERE quiz_id=$1 GROUP BY user_id ORDER BY score DESC, dur ASC''', quiz_id)
        text = f"🏁 ВИКТОРИНА #{quiz_id} ЗАВЕРШЕНА!\n\n"
        if not results:
            text += "😔 Никто не участвовал"
        else:
            prize = quiz['prize_pool']
            for i, r in enumerate(results):
                uid, sc, dur = r['user_id'], r['score'], r['dur'] or 0
                if i < 3:
                    reward = int(prize * [0.5, 0.3, 0.2][i])
                    await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", reward, uid)
                    medal = "🥇" if i == 0 else "🥈" if i == 1 else ""
                    text += f"{medal} @{await conn.fetchval('SELECT username FROM users WHERE user_id=$1', uid) or uid}: {sc} прав. ({dur:.0f}с) → +{reward}\n"
                else:
                    text += f"{i+1}. @{await conn.fetchval('SELECT username FROM users WHERE user_id=$1', uid) or uid}: {sc} прав.\n"
        try:
            res_msg = await bot.send_message(quiz['chat_id'], text)
            await res_msg.pin()
        except Exception as e:
            logger.error(f"Results: {e}")
        if quiz.get('message_id'):
            try:
                await bot.unpin_chat_message(chat_id=quiz['chat_id'], message_id=quiz['message_id'])
            except:
                pass

async def main():
    await init_db()
    logger.info("🤖 Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
