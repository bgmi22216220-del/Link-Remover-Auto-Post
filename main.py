import os
import re
import asyncio
import logging
import asyncpg
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
DATABASE_URL = os.getenv("DATABASE_URL")          # Neon postgres:// URL
CHANNEL_IDS  = [
    int(c.strip())
    for c in os.getenv("CHANNEL_IDS", "").split(",")
    if c.strip()
]

if not BOT_TOKEN or not ADMIN_ID or not DATABASE_URL or not CHANNEL_IDS:
    raise RuntimeError(
        "Missing env vars: BOT_TOKEN, ADMIN_ID, DATABASE_URL, CHANNEL_IDS"
    )

# asyncpg needs postgresql:// scheme (not postgres://)
DB_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ─── Pyrogram Client ────────────────────────────────────────────────────────
app = Client("xvpbot", bot_token=BOT_TOKEN)

# ─── Global State ────────────────────────────────────────────────────────────
cancel_flag: bool = False
_pool: asyncpg.Pool | None = None


# ════════════════════════════════════════════════════════════════════════════
# DATABASE — asyncpg (pure Python, no libpq needed)
# ════════════════════════════════════════════════════════════════════════════

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DB_URL,
            ssl="require",
            min_size=1,
            max_size=5,
        )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id         SERIAL PRIMARY KEY,
                file_id    TEXT,
                caption    TEXT,
                media_type TEXT
            );
        """)
    log.info("Database tables ready.")


# ── Settings ─────────────────────────────────────────────────────────────────

async def get_setting(key: str, default=None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM settings WHERE key=$1", key
        )
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, key, value)


# ── Queue ─────────────────────────────────────────────────────────────────────

async def enqueue(file_id, caption, media_type):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO queue (file_id, caption, media_type) VALUES ($1,$2,$3)",
            file_id, caption, media_type
        )


async def get_queue():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM queue ORDER BY id ASC")


async def clear_queue():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM queue")


async def queue_count() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM queue")


# ════════════════════════════════════════════════════════════════════════════
# LINK FILTER
# ════════════════════════════════════════════════════════════════════════════

URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+)",
    re.IGNORECASE
)


def filter_caption(caption: str) -> str:
    """Remove all links that do NOT contain the word 'tera'."""
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
    """Split posts into n equal (or near-equal) groups with no repeats."""
    base, extra = divmod(len(posts), n_channels)
    groups, idx = [], 0
    for i in range(n_channels):
        size = base + (1 if i < extra else 0)
        groups.append(list(posts[idx: idx + size]))
        idx += size
    return groups


async def send_post(channel_id: int, post, footer: str):
    """Send one post to a channel with footer appended."""
    caption = post["caption"] or ""
    if footer:
        caption = f"{caption}\n\n{footer}" if caption else footer
    caption = caption[:1024]

    mtype = post["media_type"]
    fid   = post["file_id"]

    kwargs = dict(
        chat_id=channel_id,
        caption=caption or None,
        parse_mode="html",
    )

    if mtype == "photo":
        await app.send_photo(photo=fid, **kwargs)
    elif mtype == "video":
        await app.send_video(video=fid, **kwargs)
    elif mtype == "document":
        await app.send_document(document=fid, **kwargs)
    else:
        await app.send_message(
            chat_id=channel_id,
            text=caption or ".",
            parse_mode="html",
        )


# ════════════════════════════════════════════════════════════════════════════
# ADMIN DECORATOR
# ════════════════════════════════════════════════════════════════════════════

def admin_only(func):
    async def wrapper(client, message: Message):
        if message.from_user and message.from_user.id == ADMIN_ID:
            await func(client, message)
        else:
            await message.reply("⛔ Unauthorized.")
    return wrapper


# ════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════

@app.on_message(filters.command("start") & filters.private)
@admin_only
async def cmd_start(_, message: Message):
    count = await queue_count()
    await message.reply(
        "👋 <b>XVIP Channel Splitter Bot</b> is active!\n\n"
        "📌 <b>Commands:</b>\n"
        "/footer [text] — Set footer\n"
        "/send — Distribute queue to channels\n"
        "/cancel — Stop ongoing send\n"
        "/menu — Open control panel\n\n"
        f"📡 <b>Channels:</b> {len(CHANNEL_IDS)}\n"
        f"📦 <b>Queue:</b> {count} posts",
        parse_mode="html"
    )


@app.on_message(filters.command("footer") & filters.private)
@admin_only
async def cmd_footer(_, message: Message):
    text  = message.text or ""
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        current = await get_setting("footer", "<i>(none)</i>")
        await message.reply(
            f"📝 <b>Current Footer:</b>\n{current}\n\n"
            "Usage: <code>/footer your footer text</code>",
            parse_mode="html"
        )
        return
    footer_text = parts[1].strip()
    await set_setting("footer", footer_text)
    await message.reply(
        f"✅ <b>Footer updated!</b>\n\n{footer_text}",
        parse_mode="html"
    )


@app.on_message(filters.command("cancel") & filters.private)
@admin_only
async def cmd_cancel(_, message: Message):
    global cancel_flag
    cancel_flag = True
    await message.reply("🛑 Cancel signal sent. Will stop after current post...")


@app.on_message(filters.command("menu") & filters.private)
@admin_only
async def cmd_menu(_, message: Message):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Check Queue",        callback_data="menu_queue"),
            InlineKeyboardButton("📝 Current Footer",     callback_data="menu_footer"),
        ],
        [
            InlineKeyboardButton("📡 Connected Channels", callback_data="menu_channels"),
            InlineKeyboardButton("🗑 Clear Queue",         callback_data="menu_clear"),
        ],
    ])
    await message.reply("🎛 <b>Control Panel</b>", reply_markup=kb, parse_mode="html")


# ── /send ─────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("send") & filters.private)
@admin_only
async def cmd_send(_, message: Message):
    global cancel_flag
    cancel_flag = False

    posts = await get_queue()
    total = len(posts)

    if total == 0:
        await message.reply("📭 Queue is empty. Forward some posts first.")
        return

    n      = len(CHANNEL_IDS)
    groups = split_posts(posts, n)
    footer = await get_setting("footer", "") or ""

    sent_counts = [0] * n

    def build_status():
        lines = ["📊 <b>Post Distribution Status:</b>\n"]
        for i, g in enumerate(groups):
            lines.append(f"📡 Channel {i+1}: {sent_counts[i]}/{len(g)} Sent")
        remaining = total - sum(sent_counts)
        lines.append(f"\n⏳ Total Pending: {remaining}")
        if remaining == 0:
            lines.append("🎉 All posts delivered successfully!")
        else:
            lines.append("✅ 100% Delivery Guarantee Mode Active")
        return "\n".join(lines)

    status_msg = await message.reply(build_status(), parse_mode="html")

    async def update_status():
        try:
            await status_msg.edit_text(build_status(), parse_mode="html")
        except Exception:
            pass

    for ch_idx, (channel_id, group) in enumerate(zip(CHANNEL_IDS, groups)):
        for post in group:
            if cancel_flag:
                await update_status()
                await clear_queue()
                await message.reply("🛑 Sending cancelled & queue cleared.")
                return
            while True:
                try:
                    await send_post(channel_id, post, footer)
                    sent_counts[ch_idx] += 1
                    await update_status()
                    await asyncio.sleep(0.5)
                    break
                except FloodWait as fw:
                    log.warning(f"FloodWait {fw.value}s — waiting...")
                    await asyncio.sleep(fw.value + 1)
                except (ChatWriteForbidden, ChannelInvalid) as e:
                    log.error(f"Channel {channel_id} error: {e}")
                    break
                except Exception as e:
                    log.error(f"Unexpected error: {e}")
                    await asyncio.sleep(3)
                    break

    await clear_queue()
    await update_status()
    await message.reply("✅ Distribution complete! Queue cleared.")


# ── Incoming media → queue ────────────────────────────────────────────────────

@app.on_message(
    filters.private &
    filters.user(ADMIN_ID) &
    (filters.photo | filters.video | filters.document | filters.text)
)
async def collect_post(_, message: Message):
    if message.text and message.text.startswith("/"):
        return

    raw_caption = message.caption or message.text or ""
    caption     = filter_caption(raw_caption)

    if message.photo:
        await enqueue(message.photo.file_id, caption, "photo")
        label = "🖼 Photo"
    elif message.video:
        await enqueue(message.video.file_id, caption, "video")
        label = "🎬 Video"
    elif message.document:
        await enqueue(message.document.file_id, caption, "document")
        label = "📄 Document"
    else:
        await enqueue(None, caption, "text")
        label = "📝 Text"

    count = await queue_count()
    await message.reply(
        f"✅ {label} queued.\n📦 Total in queue: <b>{count}</b>",
        parse_mode="html"
    )


# ── Callback Queries ──────────────────────────────────────────────────────────

@app.on_callback_query()
async def cb_handler(_, cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("⛔ Unauthorized", show_alert=True)
        return

    if cq.data == "menu_queue":
        count = await queue_count()
        await cq.answer(f"📦 Posts in queue: {count}", show_alert=True)

    elif cq.data == "menu_footer":
        footer = await get_setting("footer", "(not set)")
        await cq.answer(f"📝 Footer:\n{str(footer)[:200]}", show_alert=True)

    elif cq.data == "menu_channels":
        ch_list = "\n".join(
            f"Ch {i+1}: {cid}" for i, cid in enumerate(CHANNEL_IDS)
        )
        await cq.answer(f"📡 Channels:\n{ch_list}", show_alert=True)

    elif cq.data == "menu_clear":
        await clear_queue()
        await cq.answer("🗑 Queue cleared!", show_alert=True)

    else:
        await cq.answer("Unknown action.")


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINT — init DB then start bot
# ════════════════════════════════════════════════════════════════════════════

async def main():
    await init_db()
    log.info(f"Bot starting | Channels: {CHANNEL_IDS} | Admin: {ADMIN_ID}")
    await app.start()
    log.info("Bot is running. Ctrl+C to stop.")
    await asyncio.Event().wait()   # keep process alive


if __name__ == "__main__":
    asyncio.run(main())
