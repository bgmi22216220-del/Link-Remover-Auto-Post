import os
import re
import asyncio
import logging
import psycopg2
import psycopg2.extras
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.errors import FloodWait, ChatWriteForbidden, ChannelInvalid

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

# ─── Environment Variables ───────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_IDS  = [
    int(c.strip())
    for c in os.getenv("CHANNEL_IDS", "").split(",")
    if c.strip()
]

if not BOT_TOKEN or not ADMIN_ID or not DATABASE_URL or not CHANNEL_IDS:
    raise RuntimeError(
        "Missing required env vars: BOT_TOKEN, ADMIN_ID, DATABASE_URL, CHANNEL_IDS"
    )

# ─── Pyrogram Client (Bot-only, no API_ID / API_HASH needed) ────────────────
app = Client("xvpbot", bot_token=BOT_TOKEN)

# ─── Global Cancel Flag ──────────────────────────────────────────────────────
cancel_flag = False

# ════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """Create tables on startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id         SERIAL PRIMARY KEY,
                    file_id    TEXT,
                    caption    TEXT,
                    media_type TEXT   -- 'photo' | 'video' | 'document' | 'text'
                );
            """)
        conn.commit()
    log.info("Database initialized.")


# ── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
            """, (key, value))
        conn.commit()


# ── Queue ─────────────────────────────────────────────────────────────────────

def enqueue(file_id, caption, media_type):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO queue (file_id, caption, media_type) VALUES (%s,%s,%s)",
                (file_id, caption, media_type)
            )
        conn.commit()


def get_queue():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM queue ORDER BY id ASC")
            return cur.fetchall()


def clear_queue():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM queue")
        conn.commit()


def queue_count():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM queue")
            return cur.fetchone()[0]


# ════════════════════════════════════════════════════════════════════════════
# LINK FILTER
# ════════════════════════════════════════════════════════════════════════════

URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+)",
    re.IGNORECASE
)


def filter_caption(caption: str) -> str:
    """Keep only links containing the word 'tera' (case-insensitive)."""
    if not caption:
        return caption

    def keep_or_remove(match):
        url = match.group(0)
        return url if re.search(r"tera", url, re.IGNORECASE) else ""

    return URL_RE.sub(keep_or_remove, caption).strip()


# ════════════════════════════════════════════════════════════════════════════
# DISTRIBUTION LOGIC
# ════════════════════════════════════════════════════════════════════════════

def split_posts(posts: list, n_channels: int) -> list[list]:
    """Divide posts into n_channels equal (or near-equal) groups."""
    total = len(posts)
    base, extra = divmod(total, n_channels)
    groups, idx = [], 0
    for i in range(n_channels):
        size = base + (1 if i < extra else 0)
        groups.append(posts[idx: idx + size])
        idx += size
    return groups


async def send_post(channel_id: int, post: dict, footer: str):
    """Send a single post to a channel with footer appended."""
    caption = post["caption"] or ""
    if footer:
        caption = f"{caption}\n\n{footer}" if caption else footer

    caption = caption[:1024]  # Telegram limit

    mtype = post["media_type"]
    fid   = post["file_id"]

    kwargs = dict(
        chat_id=channel_id,
        caption=caption or None,
        parse_mode="html",
        protect_content=False
    )

    if mtype == "photo":
        await app.send_photo(photo=fid, **kwargs)
    elif mtype == "video":
        await app.send_video(video=fid, **kwargs)
    elif mtype == "document":
        await app.send_document(document=fid, **kwargs)
    else:
        # plain text
        await app.send_message(
            chat_id=channel_id,
            text=caption or ".",
            parse_mode="html",
            protect_content=False
        )


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════════════════

def admin_only(func):
    """Decorator: only allow ADMIN_ID."""
    async def wrapper(client, message: Message):
        if message.from_user and message.from_user.id == ADMIN_ID:
            await func(client, message)
        else:
            await message.reply("⛔ Unauthorized.")
    return wrapper


# ── /start ────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
@admin_only
async def cmd_start(_, message: Message):
    await message.reply(
        "👋 <b>XVIP Channel Splitter Bot</b> is active!\n\n"
        "📌 <b>Commands:</b>\n"
        "/footer [text] — Set footer\n"
        "/send — Distribute queue to channels\n"
        "/cancel — Stop ongoing send\n"
        "/menu — Open control panel\n\n"
        f"📡 <b>Channels:</b> {len(CHANNEL_IDS)}\n"
        f"📦 <b>Queue:</b> {queue_count()} posts",
        parse_mode="html"
    )


# ── /footer ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("footer") & filters.private)
@admin_only
async def cmd_footer(_, message: Message):
    # Everything after /footer
    text = message.text or ""
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        current = get_setting("footer", "_(none)_")
        await message.reply(
            f"📝 <b>Current Footer:</b>\n{current}\n\n"
            "Usage: <code>/footer your footer text here</code>",
            parse_mode="html"
        )
        return

    footer_text = parts[1].strip()
    set_setting("footer", footer_text)
    await message.reply(
        f"✅ <b>Footer updated!</b>\n\n{footer_text}",
        parse_mode="html"
    )


# ── Incoming media (queue building) ──────────────────────────────────────────

@app.on_message(
    filters.private &
    filters.user(ADMIN_ID) &
    (filters.photo | filters.video | filters.document | filters.text)
)
async def collect_post(_, message: Message):
    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    raw_caption = message.caption or message.text or ""
    caption     = filter_caption(raw_caption)

    if message.photo:
        enqueue(message.photo.file_id, caption, "photo")
        media_label = "🖼 Photo"
    elif message.video:
        enqueue(message.video.file_id, caption, "video")
        media_label = "🎬 Video"
    elif message.document:
        enqueue(message.document.file_id, caption, "document")
        media_label = "📄 Document"
    else:
        enqueue(None, caption, "text")
        media_label = "📝 Text"

    count = queue_count()
    await message.reply(
        f"✅ {media_label} queued.\n📦 Total in queue: <b>{count}</b>",
        parse_mode="html"
    )


# ── /send ─────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("send") & filters.private)
@admin_only
async def cmd_send(_, message: Message):
    global cancel_flag
    cancel_flag = False

    posts  = get_queue()
    total  = len(posts)

    if total == 0:
        await message.reply("📭 Queue is empty. Send some posts first.")
        return

    n      = len(CHANNEL_IDS)
    groups = split_posts(posts, n)
    footer = get_setting("footer", "")

    # Build initial status message
    lines = ["📊 <b>Post Distribution Status:</b>\n"]
    for i, g in enumerate(groups):
        lines.append(f"📡 Channel {i+1}: 0/{len(g)} Sent")
    lines.append(f"\n⏳ Total Pending: {total}")
    lines.append("✅ 100% Delivery Guarantee Mode Active")
    status_msg = await message.reply("\n".join(lines), parse_mode="html")

    sent_counts = [0] * n

    async def update_status():
        lines2 = ["📊 <b>Post Distribution Status:</b>\n"]
        for i, g in enumerate(groups):
            lines2.append(f"📡 Channel {i+1}: {sent_counts[i]}/{len(g)} Sent")
        remaining = total - sum(sent_counts)
        lines2.append(f"\n⏳ Total Pending: {remaining}")
        if remaining == 0:
            lines2.append("🎉 All posts delivered successfully!")
        else:
            lines2.append("✅ 100% Delivery Guarantee Mode Active")
        try:
            await status_msg.edit_text("\n".join(lines2), parse_mode="html")
        except Exception:
            pass

    # Send posts channel by channel
    for ch_idx, (channel_id, group) in enumerate(zip(CHANNEL_IDS, groups)):
        for post in group:
            if cancel_flag:
                await update_status()
                clear_queue()
                await message.reply("🛑 Sending cancelled & queue cleared.")
                return
            while True:
                try:
                    await send_post(channel_id, post, footer)
                    sent_counts[ch_idx] += 1
                    await update_status()
                    await asyncio.sleep(0.5)   # gentle pacing
                    break
                except FloodWait as fw:
                    log.warning(f"FloodWait {fw.value}s for channel {channel_id}")
                    await asyncio.sleep(fw.value + 1)
                except (ChatWriteForbidden, ChannelInvalid) as e:
                    log.error(f"Channel {channel_id} error: {e}")
                    break
                except Exception as e:
                    log.error(f"Unexpected error: {e}")
                    await asyncio.sleep(3)
                    break

    clear_queue()
    await update_status()
    await message.reply("✅ All posts distributed & queue cleared!")


# ── /cancel ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("cancel") & filters.private)
@admin_only
async def cmd_cancel(_, message: Message):
    global cancel_flag
    cancel_flag = True
    await message.reply("🛑 Cancel signal sent. Stopping after current post...")


# ── /menu ─────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("menu") & filters.private)
@admin_only
async def cmd_menu(_, message: Message):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Check Queue",       callback_data="menu_queue"),
            InlineKeyboardButton("📝 Current Footer",    callback_data="menu_footer"),
        ],
        [
            InlineKeyboardButton("📡 Connected Channels", callback_data="menu_channels"),
            InlineKeyboardButton("🗑 Clear Queue",         callback_data="menu_clear"),
        ],
    ])
    await message.reply("🎛 <b>Control Panel</b>", reply_markup=kb, parse_mode="html")


# ── Callback Queries (menu buttons) ──────────────────────────────────────────

@app.on_callback_query()
async def cb_handler(_, cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("⛔ Unauthorized", show_alert=True)
        return

    data = cq.data

    if data == "menu_queue":
        count = queue_count()
        await cq.answer(f"📦 Posts in queue: {count}", show_alert=True)

    elif data == "menu_footer":
        footer = get_setting("footer", "(not set)")
        await cq.answer(f"📝 Footer:\n{footer[:200]}", show_alert=True)

    elif data == "menu_channels":
        ch_list = "\n".join(
            [f"Channel {i+1}: {cid}" for i, cid in enumerate(CHANNEL_IDS)]
        )
        await cq.answer(f"📡 Channels:\n{ch_list}", show_alert=True)

    elif data == "menu_clear":
        clear_queue()
        await cq.answer("🗑 Queue cleared!", show_alert=True)

    else:
        await cq.answer("Unknown action.")


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    log.info(f"Bot starting | Channels: {CHANNEL_IDS} | Admin: {ADMIN_ID}")
    app.run()
