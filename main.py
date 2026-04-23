import os
import re
import asyncio
import logging
from telegram import (
    Update, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import asyncpg

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
CHANNEL_IDS  = [int(x.strip()) for x in os.environ["CHANNEL_IDS"].split(",")]

# ── DB ────────────────────────────────────────────────────────────────
async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_db()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS footer (
            id      SERIAL PRIMARY KEY,
            content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_posts (
            id       SERIAL PRIMARY KEY,
            msg_type TEXT NOT NULL,
            caption  TEXT,
            file_id  TEXT,
            raw_text TEXT
        );
    """)
    await conn.close()
    logger.info("DB initialized.")

# ── HELPERS ───────────────────────────────────────────────────────────
URL_PATTERN = re.compile(
    r'(https?://[^\s]+|t\.me/[^\s]+|www\.[^\s]+)',
    re.IGNORECASE
)

def filter_links(text: str) -> str:
    """Keep only URLs that contain 'tera', remove everything else."""
    if not text:
        return text or ""
    def keep_or_remove(m):
        url = m.group(0)
        return url if "tera" in url.lower() else ""
    result = URL_PATTERN.sub(keep_or_remove, text)
    result = re.sub(r'  +', ' ', result).strip()
    return result

async def get_footer() -> str:
    conn = await get_db()
    row = await conn.fetchrow("SELECT content FROM footer ORDER BY id DESC LIMIT 1")
    await conn.close()
    return row["content"] if row else ""

async def get_pending() -> list:
    conn = await get_db()
    rows = await conn.fetch("SELECT * FROM pending_posts ORDER BY id ASC")
    await conn.close()
    return rows

async def clear_pending():
    conn = await get_db()
    await conn.execute("DELETE FROM pending_posts")
    await conn.close()

async def add_pending(msg_type, caption=None, file_id=None, raw_text=None):
    conn = await get_db()
    await conn.execute(
        "INSERT INTO pending_posts (msg_type, caption, file_id, raw_text) VALUES ($1,$2,$3,$4)",
        msg_type, caption, file_id, raw_text
    )
    await conn.close()

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_ID

def build_preview(text: str, footer: str) -> str:
    if footer:
        return f"{text}\n\n{footer}".strip()
    return text.strip()

# ── COMMANDS ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    pending = await get_pending()
    footer  = await get_footer()
    await update.message.reply_text(
        f"👋 *Link Filter Bot Active*\n\n"
        f"📦 Pending posts: `{len(pending)}`\n"
        f"📝 Footer: `{'Set ✅' if footer else 'Not set ❌'}`\n\n"
        f"*How to use:*\n"
        f"1. Posts bhejo (text/photo/video/doc)\n"
        f"2. Preview turant milega\n"
        f"3. /send karo channels mein distribute karne ke liye\n\n"
        f"_Sirf 'tera' wale links rakhta hai, baaki remove hote hain._",
        parse_mode="Markdown"
    )

async def cmd_footer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not ctx.args:
        current = await get_footer()
        msg = f"*Current footer:*\n{current}" if current else "Footer set nahi hai abhi."
        await update.message.reply_text(
            f"{msg}\n\n*Usage:* `/footer <text>`\n"
            f"Example: `/footer 🔥 Join @myChannel`\n\n"
            f"_Telegram formatting support hai: bold, italic, hyperlinks_",
            parse_mode="Markdown"
        )
        return

    content = " ".join(ctx.args)
    conn = await get_db()
    await conn.execute("DELETE FROM footer")
    await conn.execute("INSERT INTO footer (content) VALUES ($1)", content)
    await conn.close()
    await update.message.reply_text(
        f"✅ *Footer saved!*\n\n{content}",
        parse_mode="Markdown"
    )

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    count = len(await get_pending())
    await clear_pending()
    ctx.user_data["collecting"] = False
    await update.message.reply_text(
        f"🗑️ *Batch cancelled.*\n`{count}` posts clear ho gaye.\n\nNaye posts bhejo naya batch shuru karne ke liye.",
        parse_mode="Markdown"
    )

async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    posts = await get_pending()
    if not posts:
        await update.message.reply_text("⚠️ Koi pending post nahi hai. Pehle posts bhejo.")
        return

    footer = await get_footer()
    total  = len(posts)
    sent   = 0
    errors = 0

    status_msg = await update.message.reply_text(
        f"📤 Sending {total} posts to {len(CHANNEL_IDS)} channel(s)...",
    )

    for i, post in enumerate(posts):
        channel_id = CHANNEL_IDS[i % len(CHANNEL_IDS)]
        caption = post["caption"] or ""
        filtered_caption = filter_links(caption)
        final_caption = build_preview(filtered_caption, footer)

        try:
            msg_type = post["msg_type"]

            if msg_type == "text":
                text = filter_links(post["raw_text"] or "")
                final_text = build_preview(text, footer)
                await ctx.bot.send_message(
                    chat_id=channel_id,
                    text=final_text,
                    parse_mode="HTML"
                )

            elif msg_type == "photo":
                await ctx.bot.send_photo(
                    chat_id=channel_id,
                    photo=post["file_id"],
                    caption=final_caption,
                    parse_mode="HTML"
                )

            elif msg_type == "video":
                await ctx.bot.send_video(
                    chat_id=channel_id,
                    video=post["file_id"],
                    caption=final_caption,
                    parse_mode="HTML"
                )

            elif msg_type == "document":
                await ctx.bot.send_document(
                    chat_id=channel_id,
                    document=post["file_id"],
                    caption=final_caption,
                    parse_mode="HTML"
                )

            elif msg_type == "audio":
                await ctx.bot.send_audio(
                    chat_id=channel_id,
                    audio=post["file_id"],
                    caption=final_caption,
                    parse_mode="HTML"
                )

            sent += 1
            await asyncio.sleep(0.5)  # rate limit safety

        except Exception as e:
            logger.error(f"Error sending post {post['id']} to {channel_id}: {e}")
            errors += 1

    await clear_pending()
    ctx.user_data["collecting"] = False

    summary = f"✅ *Done!*\n\n📨 Sent: `{sent}/{total}`\n📺 Channels: `{len(CHANNEL_IDS)}`"
    if errors:
        summary += f"\n⚠️ Errors: `{errors}`"

    await status_msg.edit_text(summary, parse_mode="Markdown")

# ── MESSAGE HANDLER ───────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    msg = update.message
    if not msg:
        return

    # First post of new batch → clear old data
    if not ctx.user_data.get("collecting"):
        await clear_pending()
        ctx.user_data["collecting"] = True

    footer = await get_footer()

    # ── TEXT ──
    if msg.text and not msg.text.startswith("/"):
        filtered = filter_links(msg.text)
        await add_pending("text", raw_text=filtered)
        preview = build_preview(filtered, footer)
        await msg.reply_text(
            f"📋 *Preview:*\n\n{preview}",
            parse_mode="Markdown"
        )

    # ── PHOTO ──
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        caption = filter_links(msg.caption or "")
        await add_pending("photo", caption=caption, file_id=file_id)
        preview_caption = build_preview(caption, footer)
        await msg.reply_photo(
            photo=file_id,
            caption=f"📋 Preview:\n\n{preview_caption}"
        )

    # ── VIDEO ──
    elif msg.video:
        file_id = msg.video.file_id
        caption = filter_links(msg.caption or "")
        await add_pending("video", caption=caption, file_id=file_id)
        preview_caption = build_preview(caption, footer)
        await msg.reply_video(
            video=file_id,
            caption=f"📋 Preview:\n\n{preview_caption}"
        )

    # ── DOCUMENT ──
    elif msg.document:
        file_id = msg.document.file_id
        caption = filter_links(msg.caption or "")
        await add_pending("document", caption=caption, file_id=file_id)
        preview_caption = build_preview(caption, footer)
        await msg.reply_document(
            document=file_id,
            caption=f"📋 Preview:\n\n{preview_caption}"
        )

    # ── AUDIO ──
    elif msg.audio:
        file_id = msg.audio.file_id
        caption = filter_links(msg.caption or "")
        await add_pending("audio", caption=caption, file_id=file_id)
        preview_caption = build_preview(caption, footer)
        await msg.reply_audio(
            audio=file_id,
            caption=f"📋 Preview:\n\n{preview_caption}"
        )

    else:
        await msg.reply_text("⚠️ Unsupported media type.")
        return

    pending = await get_pending()
    await msg.reply_text(
        f"✅ Post `#{len(pending)}` added to batch.\n_/send karo jab saare posts ready hon._",
        parse_mode="Markdown",
        disable_notification=True
    )

# ── SETUP & MAIN ──────────────────────────────────────────────────────
async def post_init(app: Application):
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("start",  "Bot status dekhein"),
        BotCommand("footer", "Footer set ya dekhein"),
        BotCommand("send",   "Saare posts channels mein bhejein"),
        BotCommand("cancel", "Current batch cancel karein"),
    ])
    logger.info("Commands registered.")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("footer", cmd_footer))
    app.add_handler(CommandHandler("send",   cmd_send))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & filters.ChatType.PRIVATE,
        handle_message
    ))

    logger.info("Bot polling started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
