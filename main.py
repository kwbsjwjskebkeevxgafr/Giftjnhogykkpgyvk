import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ========================= CONFIGURATION =========================

BOT_TOKEN = "8939590147:AAGfsg8Wz6RKL_pUGnYRzkC82QzNDGwTUSw"
ADMIN_IDS = {8725461956}
GIVEAWAY_CHANNEL = "@thelitegiveaway"
CHANNEL_URL = "https://t.me/thelitegiveaway"
DB_PATH = "zoro_giveaway.db"

# ================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
router = Router()
bot_username = ""

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()

def str_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)

def esc(value) -> str:
    return html.escape(str(value or ""))

def full_name(user) -> str:
    return " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Telegram User"

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

class CreateGiveaway(StatesGroup):
    waiting_name = State()
    waiting_duration = State()

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS participants ( 
                giveaway_id INTEGER NOT NULL, 
                user_id INTEGER NOT NULL, 
                full_name TEXT NOT NULL, 
                username TEXT, 
                message_id INTEGER, 
                message_kind TEXT NOT NULL DEFAULT 'text', 
                joined_at TEXT NOT NULL, 
                PRIMARY KEY (giveaway_id, user_id), 
                FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE 
            ); 
            
            CREATE TABLE IF NOT EXISTS votes ( 
                giveaway_id INTEGER NOT NULL, 
                voter_id INTEGER NOT NULL, 
                participant_id INTEGER NOT NULL, 
                voted_at TEXT NOT NULL, 
                PRIMARY KEY (giveaway_id, voter_id), 
                FOREIGN KEY (giveaway_id, participant_id) REFERENCES participants(giveaway_id, user_id) ON DELETE CASCADE 
            ); 
            
            CREATE INDEX IF NOT EXISTS idx_votes_target ON votes(giveaway_id, participant_id); 
            CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways(status, ends_at); 
            """ 
        ) 
        await db.commit() 

async def db_one(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchone()

async def db_all(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            return await cur.fetchall()

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Create giveaway", callback_data="admin:create")],
        [
            InlineKeyboardButton(text="🟢 Active", callback_data="admin:list:active"),
            InlineKeyboardButton(text="🏁 Finished", callback_data="admin:list:finished"),
        ],
        [InlineKeyboardButton(text="📊 Overall statistics", callback_data="admin:stats")],
        [InlineKeyboardButton(text="ℹ️ Setup help", callback_data="admin:help")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")]
    ])

def join_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Join required channel", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ I joined — participate", callback_data=f"join:{giveaway_id}")],
    ])

def duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 day", callback_data="duration:1d"),
            InlineKeyboardButton(text="2 days", callback_data="duration:2d"),
            InlineKeyboardButton(text="3 days", callback_data="duration:3d"),
        ],
        [
            InlineKeyboardButton(text="5 days", callback_data="duration:5d"),
            InlineKeyboardButton(text="7 days", callback_data="duration:7d"),
        ],
        [InlineKeyboardButton(text="✖ Cancel", callback_data="admin:cancel")],
    ])

async def user_is_member(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(GIVEAWAY_CHANNEL, user_id)
        if member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        }:
            return True
        return member.status == ChatMemberStatus.RESTRICTED and bool(member.is_member)
    except Exception as exc:
        logging.warning("Could not verify membership for %s: %s", user_id, exc)
        return False

async def vote_count(giveaway_id: int, participant_id: int) -> int:
    row = await db_one(
        "SELECT COUNT(*) AS c FROM votes WHERE giveaway_id=? AND participant_id=?",
        (giveaway_id, participant_id),
    )
    return int(row["c"])

async def participant_caption(giveaway_id: int, participant_id: int, ended=False) -> str:
    row = await db_one(
        """SELECT p.*, g.name FROM participants p
        JOIN giveaways g ON g.id=p.giveaway_id
        WHERE p.giveaway_id=? AND p.user_id=?""",
        (giveaway_id, participant_id),
    )
    if not row:
        return ""
    count = await vote_count(giveaway_id, participant_id)
    username = f"@{esc(row['username'])}" if row["username"] else "No public username"
    status = f"🏁 Voting ended — {count} vote{'s' if count != 1 else ''}" if ended else f"🗳 Votes: {count}"
    return (
        f"⚔️ Zoro Giveaway\n\n"
        f"🎁 {esc(row['name'])}\n"
        f"👤 {esc(row['full_name'])}\n"
        f"🔗 {username}\n\n{status}"
    )

async def refresh_participant_post(bot: Bot, giveaway_id: int, participant_id: int, ended=False) -> None:
    row = await db_one(
        "SELECT message_id, message_kind FROM participants WHERE giveaway_id=? AND user_id=?",
        (giveaway_id, participant_id),
    )
    if not row or not row["message_id"]:
        return
    caption = await participant_caption(giveaway_id, participant_id, ended)
    count = await vote_count(giveaway_id, participant_id)
    markup = None if ended else InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗳 Vote • {count}", callback_data=f"vote:{giveaway_id}:{participant_id}")]
    ])
    try:
        if row["message_kind"] == "photo":
            await bot.edit_message_caption(
                chat_id=GIVEAWAY_CHANNEL, message_id=row["message_id"],
                caption=caption, reply_markup=markup, parse_mode=ParseMode.HTML,
            )
        else:
            await bot.edit_message_text(
                chat_id=GIVEAWAY_CHANNEL, message_id=row["message_id"],
                text=caption, reply_markup=markup, parse_mode=ParseMode.HTML,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logging.warning("Could not refresh participant post: %s", exc)

async def register_participant(bot: Bot, user, giveaway_id: int) -> tuple[bool, str]:
    giveaway = await db_one("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    if not giveaway:
        return False, "This giveaway does not exist."
    if giveaway["status"] != "active" or str_to_dt(giveaway["ends_at"]) <= utcnow():
        return False, "This giveaway has already ended."
    if not await user_is_member(bot, user.id):
        return False, "JOIN_REQUIRED"
    existing = await db_one(
        "SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, user.id)
    )
    if existing:
        return True, "You are already participating in this giveaway."

    name = full_name(user) 
    username = user.username 
    async with aiosqlite.connect(DB_PATH) as db: 
        await db.execute( 
            """INSERT OR IGNORE INTO participants (giveaway_id,user_id,full_name,username,joined_at) VALUES(?,?,?,?,?)""", 
            (giveaway_id, user.id, name, username, dt_to_str(utcnow())), 
        ) 
        await db.commit() 
        
    caption = await participant_caption(giveaway_id, user.id) 
    keyboard = InlineKeyboardMarkup(inline_keyboard=[ 
        [InlineKeyboardButton(text="🗳 Vote • 0", callback_data=f"vote:{giveaway_id}:{user.id}")] 
    ]) 
    sent = None 
    kind = "text" 
    
    try: 
        photos = await bot.get_user_profile_photos(user.id, limit=1) 
        if photos.total_count and photos.photos: 
            sent = await bot.send_photo( 
                GIVEAWAY_CHANNEL, 
                photos.photos[0][-1].file_id, 
                caption=caption, 
                reply_markup=keyboard, 
                parse_mode=ParseMode.HTML, 
            ) 
            kind = "photo" 
    except Exception as exc: 
        logging.info("Profile photo unavailable for %s: %s", user.id, exc) 
        
    if sent is None: 
        sent = await bot.send_message( 
            GIVEAWAY_CHANNEL, 
            caption, 
            reply_markup=keyboard, 
            parse_mode=ParseMode.HTML, 
        ) 
        
    async with aiosqlite.connect(DB_PATH) as db: 
        await db.execute( 
            "UPDATE participants SET message_id=?, message_kind=? WHERE giveaway_id=? AND user_id=?", 
            (sent.message_id, kind, giveaway_id, user.id), 
        ) 
        await db.commit() 
        
    return True, f"You joined “{giveaway['name']}”! Your voting card is now in the channel." 

async def show_admin(target: Message | CallbackQuery) -> None:
    text = "⚔️ Zoro Giveaway — Admin Panel\n\nCreate and manage voting giveaways."
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)
        await target.answer()
    else:
        await target.answer(text, reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)

@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("This command is only available to bot admins.")
        return
    await state.clear()
    await show_admin(message)

@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    await state.clear()
    await show_admin(callback)

@router.callback_query(F.data == "admin:create")
async def create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    await state.set_state(CreateGiveaway.waiting_name)
    await callback.message.edit_text(
        "🎁 Create giveaway\n\nSend the giveaway name.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Cancel", callback_data="admin:cancel")]
        ]), parse_mode=ParseMode.HTML,
    )
    await callback.answer()

@router.callback_query(F.data == "admin:cancel")
async def cancel_creation(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    await state.clear()
    await show_admin(callback)

@router.message(CreateGiveaway.waiting_name)
async def receive_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 100):
        await message.answer("Please send a name between 2 and 100 characters.")
        return
    await state.update_data(name=name)
    await state.set_state(CreateGiveaway.waiting_duration)
    await message.answer(
        f"🎁 {esc(name)}\n\nSelect the voting duration, or send a custom value such as 12h, 30m, or 10d.",
        reply_markup=duration_keyboard(), parse_mode=ParseMode.HTML,
    )

def parse_duration(value: str):
    match = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", value.lower())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
    if delta < timedelta(minutes=5) or delta > timedelta(days=365):
        return None
    return delta

async def finish_creation(message: Message, state: FSMContext, admin_id: int, duration_text: str):
    duration = parse_duration(duration_text)
    if not duration:
        await message.answer("Invalid duration. Use 5m–365d, for example: 12h, 2d, or 30m.")
        return
    data = await state.get_data()
    name = data.get("name")
    if not name:
        await state.clear()
        await message.answer("Creation session expired. Open /admin and try again.")
        return
    created = utcnow()
    ends = created + duration
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO giveaways(name,creator_id,created_at,ends_at,status) VALUES(?,?,?,?, 'active')",
            (name, admin_id, dt_to_str(created), dt_to_str(ends)),
        )
        giveaway_id = cur.lastrowid
        await db.commit()
    await state.clear()
    link = f"https://t.me/{bot_username}?start=g_{giveaway_id}"
    text = (
        f"✅ Giveaway created!\n\n"
        f"🎁 {esc(name)}\n"
        f"⏳ Ends: {ends.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"🔗 Participation link\n{esc(link)}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Open participation link", url=link)],
        [InlineKeyboardButton(text="📊 Manage giveaway", callback_data=f"admin:view:{giveaway_id}")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

@router.callback_query(CreateGiveaway.waiting_duration, F.data.startswith("duration:"))
async def receive_duration_button(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    value = callback.data.split(":", 1)[1]
    await callback.answer()
    await finish_creation(callback.message, state, callback.from_user.id, value)

@router.message(CreateGiveaway.waiting_duration)
async def receive_custom_duration(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await finish_creation(message, state, message.from_user.id, message.text or "")

@router.message(CommandStart())
async def start_command(message: Message, bot: Bot):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and re.fullmatch(r"g_\d+", parts[1]):
        giveaway_id = int(parts[1][2:])
        ok, response = await register_participant(bot, message.from_user, giveaway_id)
        if response == "JOIN_REQUIRED":
            giveaway = await db_one("SELECT name FROM giveaways WHERE id=?", (giveaway_id,))
            title = giveaway["name"] if giveaway else "this giveaway"
            await message.answer(
                f"📢 Join the required channel before participating in {esc(title)}, then tap the check button.",
                reply_markup=join_keyboard(giveaway_id), parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(("✅ " if ok else "❌ ") + response)
        return
    text = (
        "⚔️ Welcome to Zoro Giveaway!\n\n"
        "Open an official giveaway participation link to enter. You must remain in the required channel for your votes to count."
    )
    if is_admin(message.from_user.id):
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Admin panel", callback_data="admin:home")]
        ]), parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("join:"))
async def check_join(callback: CallbackQuery, bot: Bot):
    giveaway_id = int(callback.data.split(":")[1])
    ok, response = await register_participant(bot, callback.from_user, giveaway_id)
    if response == "JOIN_REQUIRED":
        return await callback.answer("You have not joined the required channel yet.", show_alert=True)
    await callback.answer(("✅ " if ok else "❌ ") + response, show_alert=True)
    if ok:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

@router.callback_query(F.data.startswith("vote:"))
async def cast_vote(callback: CallbackQuery, bot: Bot):
    try:
        _, gid, pid = callback.data.split(":")
        giveaway_id, participant_id = int(gid), int(pid)
    except (ValueError, AttributeError):
        return await callback.answer("Invalid voting button.", show_alert=True)

    giveaway = await db_one("SELECT * FROM giveaways WHERE id=?", (giveaway_id,)) 
    if not giveaway or giveaway["status"] != "active" or str_to_dt(giveaway["ends_at"]) <= utcnow(): 
        return await callback.answer("Voting has ended.", show_alert=True) 
        
    if callback.from_user.id == participant_id: 
        return await callback.answer("You cannot vote for yourself.", show_alert=True) 
        
    if not await user_is_member(bot, callback.from_user.id): 
        keyboard = InlineKeyboardMarkup(inline_keyboard=[ 
            [InlineKeyboardButton(text="📢 Join channel", url=CHANNEL_URL)] 
        ]) 
        try: 
            await callback.from_user.send_message( 
                "Join the required channel before voting, then return and tap Vote again.", 
                reply_markup=keyboard 
            ) 
        except Exception: 
            pass 
        return await callback.answer("Join the required channel before voting.", show_alert=True) 
        
    participant = await db_one( 
        "SELECT 1 FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, participant_id) 
    ) 
    if not participant: 
        return await callback.answer("Participant not found.", show_alert=True) 
        
    previous = await db_one(
        "SELECT participant_id FROM votes WHERE giveaway_id=? AND voter_id=?", 
        (giveaway_id, callback.from_user.id)
    ) 
    
    if previous and previous["participant_id"] == participant_id: 
        return await callback.answer("You already voted for this participant.", show_alert=True) 
        
    async with aiosqlite.connect(DB_PATH) as db: 
        await db.execute( 
            """INSERT INTO votes(giveaway_id,voter_id,participant_id,voted_at) 
               VALUES(?,?,?,?) 
               ON CONFLICT(giveaway_id,voter_id) 
               DO UPDATE SET participant_id=excluded.participant_id, voted_at=excluded.voted_at""", 
            (giveaway_id, callback.from_user.id, participant_id, dt_to_str(utcnow())), 
        ) 
        await db.commit() 
        
    await refresh_participant_post(bot, giveaway_id, participant_id) 
    
    if previous: 
        await refresh_participant_post(bot, giveaway_id, previous["participant_id"]) 
        await callback.answer("Your vote was changed! ✅") 
    else: 
        await callback.answer("Vote counted! ✅") 

async def giveaway_ranking(giveaway_id: int):
    return await db_all(
        """SELECT p.user_id,p.full_name,p.username,COUNT(v.voter_id) AS votes
        FROM participants p LEFT JOIN votes v
        ON v.giveaway_id=p.giveaway_id AND v.participant_id=p.user_id
        WHERE p.giveaway_id=?
        GROUP BY p.user_id,p.full_name,p.username
        ORDER BY votes DESC,p.joined_at ASC""",
        (giveaway_id,),
    )

async def close_giveaway(bot: Bot, giveaway_id: int, forced=False) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE giveaways SET status='finished' WHERE id=? AND status='active'", (giveaway_id,)
        )
        await db.commit()
    if cur.rowcount == 0:
        return False
        
    giveaway = await db_one("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    ranking = await giveaway_ranking(giveaway_id)
    for row in ranking:
        await refresh_participant_post(bot, giveaway_id, row["user_id"], ended=True)

    if not ranking: 
        result = ( 
            f"🏁 <b>Giveaway ended</b>\n\n🎁 <b>{esc(giveaway['name'])}</b>\n\n" 
            "No participants joined this giveaway." 
        ) 
    else: 
        highest = ranking[0]["votes"] 
        winners = [r for r in ranking if r["votes"] == highest] 
        winner_lines = [] 
        for r in winners: 
            mention = f"@{esc(r['username'])}" if r["username"] else f"<a href=\"tg://user?id={r['user_id']}\">{esc(r['full_name'])}</a>" 
            winner_lines.append(f"🏆 {mention} — <b>{r['votes']} votes</b>") 
            
        top_lines = [] 
        for index, r in enumerate(ranking[:10], 1): 
            label = f"@{esc(r['username'])}" if r["username"] else esc(r["full_name"]) 
            top_lines.append(f"{index}. {label} — <b>{r['votes']}</b>") 
            
        result = ( 
            f"🎊 <b>GIVEAWAY RESULT</b> 🎊\n\n" 
            f"🎁 <b>{esc(giveaway['name'])}</b>\n\n" 
            f"<b>{'Winner' if len(winners) == 1 else 'Winners (tie)'}</b>\n" + 
            "\n".join(winner_lines) + 
            "\n\n📊 <b>Top participants</b>\n" + 
            "\n".join(top_lines) 
        ) 
        
    await bot.send_message(GIVEAWAY_CHANNEL, result, parse_mode=ParseMode.HTML, disable_web_page_preview=True) 
    return True 

@router.callback_query(F.data.startswith("admin:list:"))
async def admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    status = callback.data.rsplit(":", 1)[1]
    rows = await db_all(
        """SELECT g.*,(SELECT COUNT(*) FROM participants p WHERE p.giveaway_id=g.id) AS participants,
        (SELECT COUNT(*) FROM votes v WHERE v.giveaway_id=g.id) AS votes
        FROM giveaways g WHERE status=? ORDER BY id DESC LIMIT 30""", (status,)
    )
    buttons = [[InlineKeyboardButton(
        text=f"#{r['id']} • {r['name'][:30]} ({r['participants']}👤/{r['votes']}🗳)",
        callback_data=f"admin:view:{r['id']}"
    )] for r in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")])
    title = "Active giveaways" if status == "active" else "Finished giveaways"
    await callback.message.edit_text(
        f"{title}\n\n" + ("Select a giveaway:" if rows else "No giveaways found."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin:view:"))
async def admin_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    giveaway_id = int(callback.data.rsplit(":", 1)[1])
    row = await db_one(
        """SELECT g.*,(SELECT COUNT(*) FROM participants p WHERE p.giveaway_id=g.id) AS participants,
        (SELECT COUNT(*) FROM votes v WHERE v.giveaway_id=g.id) AS votes
        FROM giveaways g WHERE id=?""", (giveaway_id,)
    )
    if not row:
        return await callback.answer("Giveaway not found.", show_alert=True)
    link = f"https://t.me/{bot_username}?start=g_{giveaway_id}"
    text = (
        f"🎁 {esc(row['name'])}\n\n"
        f"🆔 #{row['id']}\n"
        f"📌 Status: {esc(row['status'].title())}\n"
        f"👥 Participants: {row['participants']}\n"
        f"🗳 Votes: {row['votes']}\n"
        f"⏳ Ends: {str_to_dt(row['ends_at']).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"🔗 {esc(link)}"
    )
    buttons = []
    if row["status"] == "active":
        buttons.append([InlineKeyboardButton(text="🏁 End now", callback_data=f"admin:endask:{giveaway_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("admin:endask:"))
async def end_ask(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    gid = callback.data.rsplit(":", 1)[1]
    await callback.message.edit_text(
        "⚠️ End this giveaway now and publish the results?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yes, end it", callback_data=f"admin:end:{gid}")],
            [InlineKeyboardButton(text="⬅️ Go back", callback_data=f"admin:view:{gid}")],
        ]),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("admin:end:"))
async def end_now(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    giveaway_id = int(callback.data.rsplit(":", 1)[1])
    await callback.answer("Ending giveaway…")
    closed = await close_giveaway(bot, giveaway_id, forced=True)
    await callback.message.edit_text(
        "✅ Giveaway ended and results were published." if closed else "This giveaway is already finished.",
        reply_markup=back_keyboard(),
    )

@router.callback_query(F.data == "admin:stats")
async def overall_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    stats = await db_one(
        """SELECT (SELECT COUNT(*) FROM giveaways) AS giveaways,
        (SELECT COUNT(*) FROM giveaways WHERE status='active') AS active,
        (SELECT COUNT(*) FROM participants) AS entries,
        (SELECT COUNT(*) FROM votes) AS votes"""
    )
    await callback.message.edit_text(
        f"📊 Overall statistics\n\n"
        f"🎁 Giveaways: {stats['giveaways']}\n"
        f"🟢 Active: {stats['active']}\n"
        f"👥 Total entries: {stats['entries']}\n"
        f"🗳 Current stored votes: {stats['votes']}",
        reply_markup=back_keyboard(), parse_mode=ParseMode.HTML,
    )
    await callback.answer()

@router.callback_query(F.data == "admin:help")
async def admin_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Admins only.", show_alert=True)
    await callback.message.edit_text(
        "ℹ️ Required setup\n\n"
        "1. Add this bot as an administrator in @thelitegiveaway.\n"
        "2. Allow it to post and edit messages.\n"
        "3. Keep the bot in the channel so membership checks work.\n"
        "4. Set the BOT_TOKEN environment variable, then run main.py.\n\n"
        "When a voter leaves the channel, their stored vote is removed automatically through Telegram chat-member updates.",
        reply_markup=back_keyboard(), parse_mode=ParseMode.HTML,
    )
    await callback.answer()

@router.chat_member()
async def member_update(event: ChatMemberUpdated, bot: Bot):
    if event.chat.username and event.chat.username.lower() != GIVEAWAY_CHANNEL.lstrip("@").lower():
        return
    user_id = event.new_chat_member.user.id
    old_was_member = event.old_chat_member.status in {
        ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER
    } or (event.old_chat_member.status == ChatMemberStatus.RESTRICTED and bool(event.old_chat_member.is_member))
    
    new_is_member = event.new_chat_member.status in {
        ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER
    } or (event.new_chat_member.status == ChatMemberStatus.RESTRICTED and bool(event.new_chat_member.is_member))
    
    if not old_was_member or new_is_member:
        return
        
    affected = await db_all(
        "SELECT giveaway_id,participant_id FROM votes WHERE voter_id=?", (user_id,)
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM votes WHERE voter_id=?", (user_id,))
        await db.commit()
        
    for row in affected:
        giveaway = await db_one("SELECT status FROM giveaways WHERE id=?", (row["giveaway_id"],))
        if giveaway and giveaway["status"] == "active":
            await refresh_participant_post(bot, row["giveaway_id"], row["participant_id"])

async def expiry_worker(bot: Bot):
    while True:
        try:
            rows = await db_all(
                "SELECT id FROM giveaways WHERE status='active' AND ends_at<=?", (dt_to_str(utcnow()),)
            )
            for row in rows:
                await close_giveaway(bot, row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Giveaway expiry worker failed")
        await asyncio.sleep(20)

async def main():
    global bot_username
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    await init_db()
    
    bot = Bot(BOT_TOKEN)
    me = await bot.get_me()
    bot_username = me.username
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Start Zoro Giveaway"),
        BotCommand(command="admin", description="Open admin panel"),
    ])
    
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    worker = asyncio.create_task(expiry_worker(bot))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member"])
    finally:
        worker.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())