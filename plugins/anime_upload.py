# ============================================================
# anime_upload.py — new admin-only commands that write straight into
# the website's Supabase database:
#
#   /stats           — episode/content counts, same numbers as the
#                       admin dashboard
#   /upload_episode  — pick type -> pick title -> pick season -> send
#                       episode title -> forward ONE file -> auto-generates
#                       the Fast Download URL and inserts it as the next
#                       episode of that season
#   /upload_bulk     — same picking flow, then forward files one after
#                       another; each gets its own confirm button so
#                       nothing uploads until you tap it. /done to stop.
#   /edit_title      — pick type -> pick title -> send new title text
#                       to rename that content item
#
# This file is fully self-contained and only touches the `content`
# and `episodes` tables — nothing here changes any existing plugin's
# behavior. It's admin-only (same ADMINS list config.py already uses).
# ============================================================
import re
import time
import random
import string
import logging
from urllib.parse import quote_plus

from pyrogram import Client, filters, ContinuePropagation
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMINS, LOG_CHANNEL, URL
from TechVJ.utils.file_properties import get_name, get_hash
from plugins.supabase_client import is_configured, sb_select, sb_insert, sb_update, sb_count, sb_auth_login

logger = logging.getLogger(__name__)

CONTENT_TYPES = [
    ("🎴 Anime", "anime"),
    ("📺 Series", "series"),
    ("🎬 Movies", "movie"),
    ("🧸 Cartoons", "cartoon"),
    ("🎙️ FanDub", "fandub"),
]
   TYPE_LABELS = {key: label for label, key in CONTENT_TYPES}
PAGE_SIZE = 10

# ------------------------------------------------------------
# Per-user in-memory session state. Good enough for a single bot
# instance; if the bot restarts mid-flow, the user just re-runs the
# command. Not persisted to any DB — nothing here is data loss risk,
# it's only "where am I in this conversation right now".
# ------------------------------------------------------------
SESSIONS = {}          # user_id -> dict describing where they are in a flow
PENDING_BULK_ITEMS = {}  # short token -> {content_id, video_url, episode_number, file_name}

# ------------------------------------------------------------
# Admin login (Supabase Auth email/password) — required once every
# ADMIN_LOGIN_TTL seconds per admin, on top of the existing Telegram
# ADMINS id check. In-memory only: a bot restart forces a fresh login,
# which is the safer default for a credential gate like this.
# ------------------------------------------------------------
ADMIN_LOGIN_TTL = 6 * 60 * 60  # 6 hours
LOGGED_IN = {}  # user_id -> unix timestamp of last successful login


async def _admin_check(_, __, update):
    user = update.from_user
    return bool(user and user.id in ADMINS)


admin_filter = filters.create(_admin_check)


def _type_keyboard(prefix: str):
    rows = [[InlineKeyboardButton(label, callback_data=f"{prefix}_type:{key}")] for label, key in CONTENT_TYPES]
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(rows)


def _content_keyboard(prefix: str, type_key: str, items: list, page: int, total: int):
    rows = [[InlineKeyboardButton(item["title"][:40], callback_data=f"{prefix}_content:{item['id']}")] for item in items]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_page:{type_key}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_page:{type_key}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(rows)


async def _fetch_content_page(type_key: str, page: int):
    items = sb_select(
        "content",
        {
            "type": f"eq.{type_key}",
            "select": "id,title",
            "order": "title.asc",
            "limit": str(PAGE_SIZE),
            "offset": str(page * PAGE_SIZE),
        },
    )
    total = sb_count("content", {"type": f"eq.{type_key}"})
    return items, total


def _random_token(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _build_fast_download_url(log_msg) -> str:
    """Exact same URL formula this bot's existing 'Fast Download' button
    already uses (see plugins/commands.py generate_stream_link) — reusing
    it here means the link behaves identically to one a user would get
    manually."""
    return f"{URL}{log_msg.id}/{quote_plus(get_name(log_msg))}?hash={get_hash(log_msg)}"


async def _next_episode_number(content_id: str, season_number: int = None) -> int:
    """Next episode number for this content. When season_number is given,
    numbering restarts per season (Episode 1 of Season 2 instead of
    continuing the Season 1 count)."""
    params = {"content_id": f"eq.{content_id}", "select": "episode_number", "order": "episode_number.desc", "limit": "1"}
    if season_number is not None:
        params["season_number"] = f"eq.{season_number}"
    rows = sb_select("episodes", params)
    return (rows[0]["episode_number"] + 1) if rows else 1


def _clear_session(user_id):
    SESSIONS.pop(user_id, None)


def _is_logged_in(user_id) -> bool:
    ts = LOGGED_IN.get(user_id)
    return bool(ts and (time.time() - ts) < ADMIN_LOGIN_TTL)


async def _ensure_logged_in(message, resume: str) -> bool:
    """Gate for /stats, /upload_episode, /upload_bulk, /edit_title. Returns True if
    the admin already has a live session (logged in within the last 6 hours) and the
    caller should proceed immediately. Otherwise starts the email/password flow and
    returns False — the caller's command ends here and resumes automatically once
    login succeeds (see RESUME_HANDLERS)."""
    user_id = message.from_user.id
    if _is_logged_in(user_id):
        return True
    SESSIONS[user_id] = {"cmd": "login", "step": "await_email", "resume": resume}
    await message.reply(
        "🔐 Admin login required (session expired or this is your first command in "
        "the last 6 hours).\n\nSend your admin <b>email</b>:"
    )
    return False


# ============================================================
# /stats
# ============================================================
@Client.on_message(filters.command("stats") & filters.private & admin_filter)
async def cmd_stats(client, message):
    if not is_configured():
        return await message.reply(
            "⚠️ Supabase isn't connected yet.\nSet SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY as environment variables, then try again."
        )
    if not await _ensure_logged_in(message, "stats"):
        return
    await _run_stats(client, message)


async def _run_stats(client, message):
    wait = await message.reply("⏳ Fetching stats…")
    try:
        lines = ["📊 <b>Content Stats</b>", ""]
        total_episodes = 0
        for label, type_key in CONTENT_TYPES:
            content_count = sb_count("content", {"type": f"eq.{type_key}"})
            episode_count = sb_count(
                "episodes",
                {"select": "id,content!inner(type)", "content.type": f"eq.{type_key}"},
            )
            total_episodes += episode_count
            lines.append(f"{label}: <b>{content_count}</b> titles · <b>{episode_count}</b> episodes")
        lines.append("")
        lines.append(f"🎞️ <b>Total episodes: {total_episodes}</b>")
        await wait.edit(
            "\n".join(lines),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("stats fetch failed")
        await wait.edit(f"❌ Could not fetch stats: {e}")


# ============================================================
# /upload_episode
# ============================================================
@Client.on_message(filters.command("upload_episode") & filters.private & admin_filter)
async def cmd_upload_episode(client, message):
    if not is_configured():
        return await message.reply("⚠️ Supabase isn't connected — set SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY first.")
    if not await _ensure_logged_in(message, "upload_episode"):
        return
    await _start_upload_episode(client, message)


async def _start_upload_episode(client, message):
    SESSIONS[message.from_user.id] = {"cmd": "upload_episode", "step": "select_type"}
    await message.reply("📁 What type is this episode for?", reply_markup=_type_keyboard("ue"))


@Client.on_callback_query(filters.regex(r"^ue_") & admin_filter)
async def cb_upload_episode(client, query):
    user_id = query.from_user.id
    data = query.data

    if data == "ue_cancel":
        _clear_session(user_id)
        return await query.message.edit("✖️ Cancelled.")

    if data.startswith("ue_type:"):
        type_key = data.split(":", 1)[1]
        items, total = await _fetch_content_page(type_key, 0)
        if not items:
            return await query.answer("No titles of this type yet.", show_alert=True)
        SESSIONS[user_id] = {"cmd": "upload_episode", "step": "select_content", "type": type_key}
        return await query.message.edit(
            f"📁 {TYPE_LABELS[type_key]} — pick a title:",
            reply_markup=_content_keyboard("ue", type_key, items, 0, total),
        )

    if data.startswith("ue_page:"):
        _, type_key, page = data.split(":")
        page = int(page)
        items, total = await _fetch_content_page(type_key, page)
        return await query.message.edit(
            f"📁 {TYPE_LABELS[type_key]} — pick a title:",
            reply_markup=_content_keyboard("ue", type_key, items, page, total),
        )

    if data.startswith("ue_content:"):
        content_id = data.split(":", 1)[1]
        content = sb_select("content", {"id": f"eq.{content_id}", "select": "id,title"})
        if not content:
            return await query.answer("That title wasn't found — it may have been deleted.", show_alert=True)
        SESSIONS[user_id] = {
            "cmd": "upload_episode",
            "step": "await_season",
            "content_id": content_id,
            "content_title": content[0]["title"],
        }
        await query.message.edit(
            f"✅ <b>{content[0]['title']}</b> selected.\n\n🔢 Which season is this episode for? Send the season number (e.g. 1)."
        )


# ============================================================
# /upload_bulk
# ============================================================
@Client.on_message(filters.command("upload_bulk") & filters.private & admin_filter)
async def cmd_upload_bulk(client, message):
    if not is_configured():
        return await message.reply("⚠️ Supabase isn't connected — set SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY first.")
    if not await _ensure_logged_in(message, "upload_bulk"):
        return
    await _start_upload_bulk(client, message)


async def _start_upload_bulk(client, message):
    SESSIONS[message.from_user.id] = {"cmd": "upload_bulk", "step": "select_type"}
    await message.reply("📁 What type are these episodes for?", reply_markup=_type_keyboard("ub"))


@Client.on_message(filters.command("done") & filters.private & admin_filter)
async def cmd_done(client, message):
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("cmd") != "upload_bulk":
        return await message.reply("Nothing to finish — you're not in a /upload_bulk session.")
    _clear_session(message.from_user.id)
    await message.reply("✅ Bulk upload session closed.")


@Client.on_callback_query(filters.regex(r"^ub_") & admin_filter)
async def cb_upload_bulk(client, query):
    user_id = query.from_user.id
    data = query.data

    if data == "ub_cancel":
        _clear_session(user_id)
        return await query.message.edit("✖️ Cancelled.")

    if data.startswith("ub_type:"):
        type_key = data.split(":", 1)[1]
        items, total = await _fetch_content_page(type_key, 0)
        if not items:
            return await query.answer("No titles of this type yet.", show_alert=True)
        SESSIONS[user_id] = {"cmd": "upload_bulk", "step": "select_content", "type": type_key}
        return await query.message.edit(
            f"📁 {TYPE_LABELS[type_key]} — pick a title:",
            reply_markup=_content_keyboard("ub", type_key, items, 0, total),
        )

    if data.startswith("ub_page:"):
        _, type_key, page = data.split(":")
        page = int(page)
        items, total = await _fetch_content_page(type_key, page)
        return await query.message.edit(
            f"📁 {TYPE_LABELS[type_key]} — pick a title:",
            reply_markup=_content_keyboard("ub", type_key, items, page, total),
        )

    if data.startswith("ub_content:"):
        content_id = data.split(":", 1)[1]
        content = sb_select("content", {"id": f"eq.{content_id}", "select": "id,title"})
        if not content:
            return await query.answer("That title wasn't found — it may have been deleted.", show_alert=True)
        SESSIONS[user_id] = {
            "cmd": "upload_bulk",
            "step": "collecting",
            "content_id": content_id,
            "content_title": content[0]["title"],
        }
        return await query.message.edit(
            f"✅ <b>{content[0]['title']}</b> selected.\n\n"
            "📤 Forward episode files one after another. Each one gets its own "
            "✅ Upload button — tap it to actually save that episode.\n\n"
            "Send /done when you're finished."
        )

    if data.startswith("ub_confirm:"):
        token = data.split(":", 1)[1]
        item = PENDING_BULK_ITEMS.pop(token, None)
        if not item:
            return await query.answer("This one already expired or was uploaded — forward it again.", show_alert=True)
        try:
            ep_number = await _next_episode_number(item["content_id"])
            sb_insert(
                "episodes",
                {
                    "content_id": item["content_id"],
                    "episode_number": ep_number,
                    "video_url": item["video_url"],
                },
            )
            await query.message.edit(f"✅ Uploaded — <b>Episode {ep_number}</b> ({item['file_name']})")
        except Exception as e:
            logger.exception("bulk episode insert failed")
            await query.message.edit(f"❌ Upload failed: {e}")


# ============================================================
# /edit_title
# ============================================================
@Client.on_message(filters.command("edit_title") & filters.private & admin_filter)
async def cmd_edit_title(client, message):
    if not is_configured():
        return await message.reply("⚠️ Supabase isn't connected — set SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY first.")
    if not await _ensure_logged_in(message, "edit_title"):
        return
    await _start_edit_title(client, message)


async def _start_edit_title(client, message):
    SESSIONS[message.from_user.id] = {"cmd": "edit_title", "step": "select_type"}
    await message.reply("✏️ Which type is the title you want to edit?", reply_markup=_type_keyboard("et"))


@Client.on_callback_query(filters.regex(r"^et_") & admin_filter)
async def cb_edit_title(client, query):
    user_id = query.from_user.id
    data = query.data

    if data == "et_cancel":
        _clear_session(user_id)
        return await query.message.edit("✖️ Cancelled.")

    if data.startswith("et_type:"):
        type_key = data.split(":", 1)[1]
        items, total = await _fetch_content_page(type_key, 0)
        if not items:
            return await query.answer("No titles of this type yet.", show_alert=True)
        SESSIONS[user_id] = {"cmd": "edit_title", "step": "select_content", "type": type_key}
        return await query.message.edit(
            f"✏️ {TYPE_LABELS[type_key]} — pick a title to rename:",
            reply_markup=_content_keyboard("et", type_key, items, 0, total),
        )

    if data.startswith("et_page:"):
        _, type_key, page = data.split(":")
        page = int(page)
        items, total = await _fetch_content_page(type_key, page)
        return await query.message.edit(
            f"✏️ {TYPE_LABELS[type_key]} — pick a title to rename:",
            reply_markup=_content_keyboard("et", type_key, items, page, total),
        )

    if data.startswith("et_content:"):
        content_id = data.split(":", 1)[1]
        content = sb_select("content", {"id": f"eq.{content_id}", "select": "id,title"})
        if not content:
            return await query.answer("That title wasn't found — it may have been deleted.", show_alert=True)
        SESSIONS[user_id] = {
            "cmd": "edit_title",
            "step": "await_title",
            "content_id": content_id,
            "old_title": content[0]["title"],
        }
        await query.message.edit(
            f"Current title: <b>{content[0]['title']}</b>\n\nSend the new title as a plain text message."
        )


# ============================================================
# Maps the "resume" tag stored on _ensure_logged_in's session back to
# the function that should run immediately after a successful login.
# ============================================================
RESUME_HANDLERS = {
    "stats": _run_stats,
    "upload_episode": _start_upload_episode,
    "upload_bulk": _start_upload_bulk,
    "edit_title": _start_edit_title,
}


# ============================================================
# Shared handlers: incoming forwarded files / plain text, routed by
# whatever session state the user is currently in.
# ============================================================
@Client.on_message(filters.private & (filters.document | filters.video) & admin_filter, group=-1)
async def handle_incoming_file(client, message):
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("step") not in ("await_file", "collecting"):
        # Not in one of our upload flows — let genlink.py's normal
        # "forward any file, get a share link" behavior handle it,
        # exactly as it did before this plugin existed.
        raise ContinuePropagation

    media = message.document or message.video
    file_name = media.file_name or "episode file"

    status = await message.reply("⏳ Processing…")
    try:
        log_msg = await client.send_cached_media(chat_id=LOG_CHANNEL, file_id=media.file_id)
        video_url = _build_fast_download_url(log_msg)
    except Exception as e:
        logger.exception("failed to forward file to log channel")
        return await status.edit(f"❌ Could not process that file: {e}")

    if session["cmd"] == "upload_episode":
        season = session.get("season")
        episode_title = session.get("episode_title")
        try:
            ep_number = await _next_episode_number(session["content_id"], season)
            row = {
                "content_id": session["content_id"],
                "season_number": season,
                "episode_number": ep_number,
                "video_url": video_url,
            }
            if episode_title:
                row["title"] = episode_title
            sb_insert("episodes", row)
            label = f"Season {season} Episode {ep_number}"
            if episode_title:
                label += f" — {episode_title}"
            await status.edit(
                f"✅ Updated — <b>{session['content_title']}</b> {label} added."
            )
        except Exception as e:
            logger.exception("episode insert failed")
            await status.edit(f"❌ Upload failed: {e}")
        _clear_session(message.from_user.id)  # /upload_episode only ever takes one episode

    elif session["cmd"] == "upload_bulk":
        token = _random_token()
        PENDING_BULK_ITEMS[token] = {
            "content_id": session["content_id"],
            "video_url": video_url,
            "file_name": file_name,
        }
        await status.edit(
            f"📦 <b>{file_name}</b>\nfor <b>{session['content_title']}</b>\n\nTap to save this as the next episode:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Upload", callback_data=f"ub_confirm:{token}")]]
            ),
        )


@Client.on_message(filters.private & filters.text & admin_filter & ~filters.command(["stats", "upload_episode", "upload_bulk", "edit_title", "done"]), group=-1)
async def handle_incoming_text(client, message):
    session = SESSIONS.get(message.from_user.id)
    if not session:
        raise ContinuePropagation

    if session.get("cmd") == "login":
        user_id = message.from_user.id
        step = session.get("step")

        if step == "await_email":
            session["email"] = message.text.strip()
            session["step"] = "await_password"
            return await message.reply("🔑 Now send your admin <b>password</b>:")

        if step == "await_password":
            email = session.get("email", "")
            password = message.text.strip()
            try:
                await message.delete()  # scrub the password out of the chat right away
            except Exception:
                pass
            try:
                sb_auth_login(email, password)
            except Exception as e:
                _clear_session(user_id)
                return await message.reply(f"❌ Login failed: {e}\n\nRun the command again to retry.")
            LOGGED_IN[user_id] = time.time()
            resume = session.get("resume")
            _clear_session(user_id)
            await message.reply("✅ Logged in — valid for the next 6 hours.")
            handler = RESUME_HANDLERS.get(resume)
            if handler:
                await handler(client, message)
            return

        raise ContinuePropagation

    if session.get("cmd") == "upload_episode" and session.get("step") == "await_season":
        season_text = message.text.strip()
        if not season_text.isdigit():
            return await message.reply("Please send a plain number for the season (e.g. 1).")
        session["season"] = int(season_text)
        session["step"] = "await_episode_title"
        return await message.reply("📝 Send the episode title (or send - to skip):")

    if session.get("cmd") == "upload_episode" and session.get("step") == "await_episode_title":
        title_text = message.text.strip()
        session["episode_title"] = None if title_text == "-" else title_text
        session["step"] = "await_file"
        return await message.reply("📤 Now forward the episode video file here.")

    if session.get("step") != "await_title":
        raise ContinuePropagation  # not editing a title right now — leave text alone for other plugins

    new_title = message.text.strip()
    if not new_title:
        return await message.reply("Title can't be empty — send some text.")

    try:
        sb_update("content", {"id": f"eq.{session['content_id']}"}, {"title": new_title})
        await message.reply(f"✅ Renamed <b>{session['old_title']}</b> → <b>{new_title}</b>")
    except Exception as e:
        logger.exception("title update failed")
        await message.reply(f"❌ Rename failed: {e}")
    _clear_session(message.from_user.id)
