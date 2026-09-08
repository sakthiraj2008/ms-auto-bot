# bot-upload-command

A small, standalone Telegram bot with exactly 4 admin commands. It does **not**
depend on Ani-File-Store at all — this is its own deployable bot with its own
copy of the file-streaming server, so Fast Download links actually work.

## Commands
- `/stats` — episode + title counts per type (anime/series/movie/cartoon/fandub)
- `/upload_episode` — pick type → pick title → pick season → send episode
  title → forward ONE file → the bot re-uploads it to your log channel,
  builds the Fast Download URL, and inserts it as the next episode of that
  season straight into Supabase
- `/upload_bulk` — same picking flow, then forward files one after another;
  each gets its own "✅ Upload" button so nothing saves until you tap it.
  `/done` to finish.
- `/edit_title` — pick type → pick title → send new text → renames it in Supabase

## Admin login (every 6 hours)
The first admin command any admin runs in a 6-hour window will ask for an
email, then a password, and check them against **Supabase Auth** (the same
login your website's admin panel uses). The password message is deleted from
the chat right after it's read. This is on top of the Telegram `ADMINS` id
allowlist below — both have to check out.

## How the Fast Download link works
Forwarding a file re-uploads it into `LOG_CHANNEL`, then builds a URL as
`{URL}{message_id}/{filename}?hash={file_unique_id[:6]}`. This bot runs its
own copy of the same streaming web server Ani-File-Store uses (`TechVJ/`,
untouched) — that server is what actually serves the file bytes when someone
opens the link, so it has to be reachable at your `URL` for links to work.

## Environment variables (set these on Koyeb)

| Variable | Required | Notes |
|---|---|---|
| `API_ID` | yes | from https://my.telegram.org |
| `API_HASH` | yes | from https://my.telegram.org |
| `BOT_TOKEN` | yes | from @BotFather |
| `ADMINS` | yes | space-separated Telegram user IDs, e.g. `111111 222222` |
| `LOG_CHANNEL` | yes | numeric id of a private channel the bot is admin of |
| `URL` | yes | your Koyeb public URL, **must end with `/`**, e.g. `https://your-app.koyeb.app/` |
| `SUPABASE_URL` | yes | e.g. `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | the **service role** key, not anon/public |
| `PORT` | no | Koyeb sets this automatically |
| `KEEP_ALIVE` | no | `True` to self-ping every `PING_INTERVAL` seconds if your Koyeb instance idles |
| `PING_INTERVAL` | no | seconds, default `1200` |

## Deploying on Koyeb
1. Push this folder to a GitHub repo (or use Koyeb's Docker deploy from a repo).
2. Create a new Koyeb service from that repo — it will build the included
   `Dockerfile` automatically.
3. Set all the environment variables above in Koyeb's service settings.
4. Deploy. Watch the logs for `Bot started.` — if `LOG_CHANNEL` is set
   correctly, you'll also see a "Bot Restarted" message posted there.

## Before you go live — please confirm
This assumes your Supabase tables look like:
- `content` → `id`, `title`, `type`
- `episodes` → `id`, `content_id`, `season_number`, `episode_number`, `title`, `video_url`

`season_number` and `title` on `episodes` are new as of the season/episode-title
step in `/upload_episode`. If your real column names differ (or these columns
don't exist yet on your `episodes` table), tell me and I'll adjust the queries
in `plugins/anime_upload.py`.
