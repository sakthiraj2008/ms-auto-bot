import re
from os import environ

id_pattern = re.compile(r'^.\d+$')


def is_enabled(value, default):
    value = str(value)
    if value.lower() in ["true", "yes", "1", "enable", "y"]:
        return True
    elif value.lower() in ["false", "no", "0", "disable", "n"]:
        return False
    return default


# ------------------------------------------------------------
# Telegram bot credentials — from https://my.telegram.org (API_ID /
# API_HASH) and @BotFather (BOT_TOKEN). All three are required.
# ------------------------------------------------------------
API_ID = int(environ.get("API_ID", "0"))
API_HASH = environ.get("API_HASH", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")

# Telegram user IDs allowed to use /stats, /upload_episode, /upload_bulk,
# /edit_title. Space-separated if more than one, e.g. "111111 222222".
ADMINS = [int(admin) if id_pattern.search(admin) else admin for admin in environ.get('ADMINS', '').split()]

# A private Telegram channel/group the bot is an admin of. Every
# forwarded episode file gets silently re-uploaded here first — this
# is what the Fast Download links actually point back to.
LOG_CHANNEL = int(environ.get("LOG_CHANNEL", "0"))

# Port the web server (which serves the Fast Download links) binds to.
# Koyeb sets this automatically — leave PORT as-is unless Koyeb tells
# you to use a different one.
PORT = int(environ.get("PORT", "8080"))

# Your public Koyeb URL, MUST end with a trailing slash, e.g.
# https://your-app-name.koyeb.app/ — this is what gets prefixed onto
# every Fast Download link.
URL = environ.get("URL", "")

# Multi-client streaming (extra bot tokens to spread download load
# across). Not needed for a small admin bot — leave False.
MULTI_CLIENT = False
SLEEP_THRESHOLD = int(environ.get('SLEEP_THRESHOLD', '60'))

# Optional: if your Koyeb service tends to idle-sleep, set
# KEEP_ALIVE=True to have the bot self-ping every PING_INTERVAL
# seconds using its own URL above.
KEEP_ALIVE = is_enabled(environ.get('KEEP_ALIVE', 'False'), False)
PING_INTERVAL = int(environ.get("PING_INTERVAL", "1200"))  # 20 minutes

# ------------------------------------------------------------
# Supabase (your website's database) — used by /stats,
# /upload_episode, /upload_bulk, /edit_title to read and write the
# same tables your website's admin panel uses, and by the 6-hour
# admin login to verify email/password against Supabase Auth. Use the
# SERVICE ROLE key (not anon/public) — the bot needs to bypass RLS to
# write these tables.
# ------------------------------------------------------------
SUPABASE_URL = environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STORAGE_BUCKET = environ.get("SUPABASE_STORAGE_BUCKET", "animeverse-media")
