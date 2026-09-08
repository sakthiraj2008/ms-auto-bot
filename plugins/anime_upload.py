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
#   /add_content     — pick type -> title -> IMDb id -> poster upload ->
#                       banner upload -> year -> pick language -> pick
#                       genre(s), multi-select -> inserts a new row into
#                       `content`. Poster/banner photos are uploaded to a
#                       Supabase Storage bucket and the public URL is what
#                       gets saved on the row.
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

from config import ADMINS, LOG_CHANNEL, URL, SUPABASE_STORAGE_BUCKET
from TechVJ.utils.file_properties import get_name, get_hash
from plugins.supabase_client import (
    is_configured, sb_select, sb_insert, sb_update, sb_count, sb_auth_login, sb_storage_upload,
)

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

# Fixed pick-lists for /add_content. Edit these two lists to match what
# your site actually uses — nothing else in the code needs to change.
LANGUAGES = ["English", "Hindi", "Tamil", "Telugu", "Japanese", "Korean", "Chinese", "Malayalam", "Kannada"]
GENRES = [
    "Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Isekai",
    "Mystery", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller",
]
SEASON_QUICK_PICKS = list(range(1, 13))  # Season 1..12 as buttons; "Custom" covers anything else

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


def _season_keyboard(prefix: str):
    rows = []
    row = []
    for n in SEASON_QUICK_PICKS:
        row.append(InlineKeyboardButton(str(n), callback_data=f"{prefix}_season:{n}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Custom number", callback_data=f"{prefix}_season_custom")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(rows)


def _language_keyboard(prefix: str):
    rows = [[InlineKeyboardButton(lang, callback_data=f"{prefix}_lang:{lang}")] for lang in LANGUAGES]
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"{prefix}_cancel")])
    return InlineKeyboardMarkup(rows)


def _genre_keyboard(prefix: str, selected: list):
    rows = []
    row = []
    for genre in GENRES:
        mark = "✅ " if genre in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{genre}", callback_data=f"{prefix}_genre:{genre}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(f"✅ Done ({len(selected)} selected)", callback_data=f"{prefix}_genre_done")])
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
            "step": "select_season",
            "content_id": content_id,
            "content_title": content[0]["title"],
        }
        return await query.message.edit(
            f"✅ <b>{content[0]['title']}</b> selected.\n\n🔢 Which season is this episode for?",
            reply_markup=_season_keyboard("ue"),
        )

    if data.startswith("ue_season:"):
        season = int(data.split(":", 1)[1])
        SESSIONS[user_id]["season"] = season
        SESSIONS[user_id]["step"] = "await_episode_title"
        return await query.message.edit(f"✅ Season {season}.\n\n📝 Send the episode title (or send - to skip):")

    if data == "ue_season_custom":
        SESSIONS[user_id]["step"] = "await_season_custom"
        return await query.message.edit("🔢 Send the season number as plain text (e.g. 13):")


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
# /add_content
# type -> title -> imdb id -> poster photo -> banner photo -> year ->
# language (single) -> genre (multi-select) -> insert into `content`
# ============================================================
@Client.on_message(filters.command("add_content") & filters.private & admin_filter)
async def cmd_add_content(client, message):
    if not is_configured():
        return await message.reply("⚠️ Supabase isn't connected — set SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY first.")
    if not await _ensure_logged_in(message, "add_content"):
        return
    await _start_add_content(client, message)


async def _start_add_content(client, message):
    SESSIONS[message.from_user.id] = {"cmd": "add_content", "step": "select_type", "genres": []}
    await message.reply("📁 What type of content is this?", reply_markup=_type_keyboard("ac"))


@Client.on_callback_query(filters.regex(r"^ac_") & admin_filter)
async def cb_add_content(client, query):
    user_id = query.from_user.id
    data = query.data
    session = SESSIONS.get(user_id)

    if data == "ac_cancel":
        _clear_session(user_id)
        return await query.message.edit("✖️ Cancelled.")

    if data.startswith("ac_type:"):
        type_key = data.split(":", 1)[1]
        SESSIONS[user_id] = {"cmd": "add_content", "step": "await_title", "type": type_key, "genres": []}
        return await query.message.edit(f"✅ {TYPE_LABELS[type_key]}\n\n📝 Send the <b>title</b>:")

    if not session or session.get("cmd") != "add_content":
        return await query.answer("This flow expired — run /add_content again.", show_alert=True)

    if data.startswith("ac_lang:"):
        session["language"] = data.split(":", 1)[1]
        session["step"] = "select_genre"
        return await query.message.edit(
            f"✅ Language: <b>{session['language']}</b>\n\n🎭 Pick genre(s), then tap Done:",
            reply_markup=_genre_keyboard("ac", session["genres"]),
        )

    if data.startswith("ac_genre:"):
        genre = data.split(":", 1)[1]
        selected = session.setdefault("genres", [])
        if genre in selected:
            selected.remove(genre)
        else:
            selected.append(genre)
        return await query.message.edit(
            f"✅ Language: <b>{session['language']}</b>\n\n🎭 Pick genre(s), then tap Done:",
            reply_markup=_genre_keyboard("ac", selected),
        )

    if data == "ac_genre_done":
        if not session.get("genres"):
            return await query.answer("Pick at least one genre first.", show_alert=True)
        await query.message.edit("⏳ Saving…")
        try:
            row = {
                "type": session["type"],
                "title": session["title"],
                "imdb_id": session.get("imdb_id"),
                "poster_url": session["poster_url"],
                "banner_url": session["banner_url"],
                "year": session["year"],
                "language": session["language"],
                "genre": session["genres"],
            }
            sb_insert("content", row)
            await query.message.edit(
                f"✅ Added — <b>{session['title']}</b> ({TYPE_LABELS[session['type']]}, {session['year']})\n"
                f"Language: {session['language']}\nGenres: {', '.join(session['genres'])}"
            )
        except Exception as e:
            logger.exception("add_content insert failed")
            await query.message.edit(f"❌ Save failed: {e}")
        _clear_session(user_id)


# ============================================================
# Maps the "resume" tag stored on _ensure_logged_in's session back to
# the function that should run immediately after a successful login.
# ============================================================
RESUME_HANDLERS = {
    "stats": _run_stats,
    "upload_episode": _start_upload_episode,
    "upload_bulk": _start_upload_bulk,
    "edit_title": _start_edit_title,
    "add_content": _start_add_content,
}


# ============================================================
# Shared handlers: incoming forwarded files / plain text, routed by
# whatever session state the user is currently in.
# ============================================================
@Client.on_message(filters.private & filters.photo & admin_filter, group=-1)
async def handle_incoming_poster_or_banner(client, message):
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("cmd") != "add_content" or session.get("step") not in ("await_poster", "await_banner"):
        # Not in the /add_content image steps — leave photos alone for
        # whatever else (if anything) normally handles them.
        raise ContinuePropagation

    which = session["step"]  # "await_poster" or "await_banner"
    label = "poster" if which == "await_poster" else "banner"
    status = await message.reply(f"⏳ Uploading {label}…")
    try:
        file_bytes = await client.download_media(message, in_memory=True)
        file_bytes.seek(0)
        path = f"{label}s/{_random_token(12)}.jpg"
        public_url = sb_storage_upload(SUPABASE_STORAGE_BUCKET, path, file_bytes.read(), content_type="image/jpeg")
    except Exception as e:
        logger.exception("failed to upload %s to storage", label)
        return await status.edit(
            f"❌ Could not upload {label}: {e}\n\n"
            f"Make sure the '{SUPABASE_STORAGE_BUCKET}' bucket exists in Supabase Storage and is set to Public."
        )

    session[f"{label}_url"] = public_url
    if which == "await_poster":
        session["step"] = "await_banner"
        await status.edit(f"✅ Poster saved.\n\n🖼️ Now send the <b>banner</b> image.")
    else:
        session["step"] = "await_year"
        await status.edit(f"✅ Banner saved.\n\n📅 Send the <b>release year</b> (e.g. 2024).")


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


@Client.on_message(filters.private & filters.text & admin_filter & ~filters.command(["stats", "upload_episode", "upload_bulk", "edit_title", "add_content", "done"]), group=-1)
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

    if session.get("cmd") == "upload_episode" and session.get("step") == "await_season_custom":
        season_text = message.text.strip()
        if not season_text.isdigit():
            return await message.reply("Please send a plain number for the season (e.g. 13).")
        session["season"] = int(season_text)
        session["step"] = "await_episode_title"
        return await message.reply("📝 Send the episode title (or send - to skip):")

    if session.get("cmd") == "upload_episode" and session.get("step") == "await_episode_title":
        title_text = message.text.strip()
        session["episode_title"] = None if title_text == "-" else title_text
        session["step"] = "await_file"
        return await message.reply("📤 Now forward the episode video file here.")

    if session.get("cmd") == "add_content":
        step = session.get("step")

        if step == "await_title":
            title = message.text.strip()
            if not title:
                return await message.reply("Title can't be empty — send some text.")
            session["title"] = title
            session["step"] = "await_imdb"
            return await message.reply("🔗 Send the <b>IMDb id</b> (e.g. tt1234567), or send - to skip:")

        if step == "await_imdb":
            imdb_text = message.text.strip()
            session["imdb_id"] = None if imdb_text == "-" else imdb_text
            session["step"] = "await_poster"
            return await message.reply("🖼️ Now send the <b>poster</b> image (as a photo).")

        if step == "await_year":
            year_text = message.text.strip()
            if not year_text.isdigit() or not (1900 <= int(year_text) <= 2100):
                return await message.reply("Please send a plain 4-digit year (e.g. 2024).")
            session["year"] = int(year_text)
            session["step"] = "select_language"
            return await message.reply("🌐 Pick the <b>language</b>:", reply_markup=_language_keyboard("ac"))

        if step in ("await_poster", "await_banner"):
            return await message.reply(f"Please send the {'poster' if step == 'await_poster' else 'banner'} as a photo, not text.")

        raise ContinuePropagation

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
