import os
import sqlite3
import random
import string
import asyncio
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- CONFIG: set these in Render as env vars ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "PASTE_TOKEN_HERE")
# ADMIN_IDS can be single id or comma separated
ADMIN_IDS = os.environ.get("ADMIN_IDS", "")  # e.g. "123456789,987654321"
ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip())

# DB
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# --- DB HELPERS ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            premium_until TEXT,
            banned INTEGER DEFAULT 0,
            free_redeem_used INTEGER DEFAULT 0
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY,
            days INTEGER,
            created_at TEXT,
            used INTEGER DEFAULT 0
        )
    """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS states (
            user_id INTEGER PRIMARY KEY,
            expecting TEXT
        )
    """
    )
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False, many=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if many:
        cur.executemany(query, params)
    else:
        cur.execute(query, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result

# --- UTIL ---
def is_admin(user_id: int):
    return user_id in ADMIN_IDS

def gen_key(length=12):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def user_add_or_update(user_id, username=None):
    row = db_execute("SELECT user_id FROM users WHERE user_id=?", (user_id,), fetch=True)
    if not row:
        db_execute(
            "INSERT INTO users (user_id, username, premium_until, banned, free_redeem_used) VALUES (?, ?, ?, 0, 0)",
            (user_id, username or "", None),
        )
    else:
        db_execute("UPDATE users SET username=? WHERE user_id=?", (username or "", user_id))

def get_user(user_id):
    r = db_execute("SELECT user_id, username, premium_until, banned, free_redeem_used FROM users WHERE user_id=?", (user_id,), fetch=True)
    return r[0] if r else None

def set_state(user_id, expecting):
    db_execute("INSERT OR REPLACE INTO states (user_id, expecting) VALUES (?, ?)", (user_id, expecting))

def get_state(user_id):
    r = db_execute("SELECT expecting FROM states WHERE user_id=?", (user_id,), fetch=True)
    return r[0][0] if r else None

def clear_state(user_id):
    db_execute("DELETE FROM states WHERE user_id=?", (user_id,))

def add_key(days):
    k = gen_key()
    db_execute("INSERT INTO keys (key, days, created_at, used) VALUES (?, ?, ?, 0)", (k, days, datetime.utcnow().isoformat()))
    return k

def use_key(k):
    r = db_execute("SELECT key, days, used FROM keys WHERE key=?", (k,), fetch=True)
    if not r:
        return None
    keyrow = r[0]
    if keyrow[2] == 1:
        return False  # already used
    # mark used
    db_execute("UPDATE keys SET used=1 WHERE key=?", (k,))
    return keyrow[1]

def check_premium_valid(user_row):
    if not user_row or not user_row[2]:
        return False
    try:
        unt = datetime.fromisoformat(user_row[2])
        return datetime.utcnow() < unt
    except:
        return False

def set_premium(user_id, days):
    until = datetime.utcnow() + timedelta(days=days)
    db_execute("UPDATE users SET premium_until=?, free_redeem_used=0 WHERE user_id=?", (until.isoformat(), user_id))

def ban_user(user_id):
    db_execute("UPDATE users SET banned=1 WHERE user_id=?", (user_id,))

def unban_user(user_id):
    db_execute("UPDATE users SET banned=0 WHERE user_id=?", (user_id,))

def mark_free_redeem_used(user_id):
    db_execute("UPDATE users SET free_redeem_used=1 WHERE user_id=?", (user_id,))

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_add_or_update(user.id, user.username)
    keyboard = [
        [InlineKeyboardButton("Redeem Request", callback_data="redeem")],
        [InlineKeyboardButton("Buy Premium", callback_data="buy")],
        [InlineKeyboardButton("Service", callback_data="service")],
        [InlineKeyboardButton("Dev", callback_data="dev")],
    ]
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_add_or_update(user.id, user.username)
    # check ban
    u = get_user(user.id)
    if u and u[3] == 1:
        await query.edit_message_text("You are banned.")
        return

    if query.data == "redeem":
        # check limits
        premium = check_premium_valid(u)
        if not premium and u and u[4] == 1:
            await query.edit_message_text("Free users can only redeem once. Buy premium for unlimited requests.")
            return
        # ask details
        set_state(user.id, "redeem_details")
        await query.edit_message_text("Enter Details for redeem request (send your message now):")
    elif query.data == "buy":
        set_state(user.id, "enter_key")
        await query.edit_message_text("Enter your premium key now (use /enterkey or just send the key):")
    elif query.data == "service":
        services = "1. Prime Video\n2. Spotify\n3. Crunchyroll\n4. Turbo VPN\n5. Hotspot Shield VPN"
        await query.edit_message_text(services)
    elif query.data == "dev":
        await query.edit_message_text("@YourAizen")

async def messages_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_add_or_update(user.id, user.username)
    u = get_user(user.id)
    if u and u[3] == 1:
        # banned
        return

    state = get_state(user.id)
    text = update.message.text or ""
    # If user is entering key
    if state == "enter_key":
        clear_state(user.id)
        key = text.strip()
        days = use_key(key)
        if days is None:
            await update.message.reply_text("Key invalid.")
            return
        if days is False:
            await update.message.reply_text("Key already used.")
            return
        # give premium
        set_premium(user.id, days)
        await update.message.reply_text(f"Premium activated for {days} days. Thank you!")
        # notify admin
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(aid, f"User @{user.username} ({user.id}) used key {key} for {days} days.")
            except Exception:
                pass
        return

    # If user is submitting redeem details
    if state == "redeem_details":
        clear_state(user.id)
        # check free usage limit
        premium = check_premium_valid(u)
        if not premium:
            if u and u[4] == 1:
                await update.message.reply_text("You have already used your free redeem.")
                return
            else:
                mark_free_redeem_used(user.id)
        # forward to admin with details
        details = text.strip()
        await update.message.reply_text("Your redeem request has been sent to admin.")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid,
                    f"📥 Redeem Request from @{user.username} ({user.id}):\n\n{details}"
                )
            except Exception:
                pass
        return

    # Normal chat fallback
    await update.message.reply_text("Use the buttons (send /start to see).")

# --- ADMIN COMMANDS ---
async def genk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Unauthorized.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /genk <days>")
        return
    days = int(args[0])
    key = add_key(days)
    await update.message.reply_text(f"Generated key: `{key}` for {days} days", parse_mode="Markdown")
    # optional: send key to admin privately (already sending to same chat)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    rows = db_execute("SELECT user_id FROM users", fetch=True)
    count = 0
    for (uid,) in rows:
        try:
            await context.bot.send_message(uid, f"[Broadcast]\n\n{msg}")
            count += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {count} users.")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
        ban_user(uid)
        await update.message.reply_text(f"Banned {uid}")
    except:
        await update.message.reply_text("Invalid user id.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
        unban_user(uid)
        await update.message.reply_text(f"Unbanned {uid}")
    except:
        await update.message.reply_text("Invalid user id.")

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return
    try:
        uid = int(context.args[0])
        msg = " ".join(context.args[1:])
        await context.bot.send_message(uid, f"[Admin Reply]\n\n{msg}")
        await update.message.reply_text("Sent.")
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")

# periodic cleanup / notify about expiries (optional)
async def check_premiums(app):
    rows = db_execute("SELECT user_id, username, premium_until FROM users WHERE premium_until IS NOT NULL", fetch=True)
    now = datetime.utcnow()
    for user_id, username, premium_until in rows:
        try:
            until = datetime.fromisoformat(premium_until) if premium_until else None
            if until and now > until:
                # expired -> clear premium_until
                db_execute("UPDATE users SET premium_until=NULL WHERE user_id=?", (user_id,))
                for aid in ADMIN_IDS:
                    try:
                        await app.bot.send_message(aid, f"Premium expired for @{username} ({user_id}).")
                    except Exception:
                        pass
        except Exception:
            pass

# --- STARTUP ---
def run():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages_handler))

    # admin commands
    app.add_handler(CommandHandler("genk", genk_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))

    # scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(check_premiums(app)), "interval", minutes=30)
    scheduler.start()

    print("Bot started (polling)...")
    app.run_polling()

if __name__ == "__main__":
    run()
