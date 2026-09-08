# ============================================================
# supabase_client.py — tiny PostgREST wrapper so the bot can read/write
# the same Supabase database as the website's admin panel, without
# pulling in the full supabase-py SDK (avoids version-conflict risk
# with this bot's existing dependency pins). Uses the SERVICE ROLE
# key, so it bypasses RLS entirely — every function here is meant to
# be called only from admin-gated bot commands.
# ============================================================
import logging
import requests
from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger(__name__)


def _base_headers(prefer=None):
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def sb_select(table: str, params: dict = None):
    """GET rows. params are raw PostgREST query params, e.g. {'type': 'eq.anime', 'select': 'id,title'}."""
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    resp = requests.get(url, headers=_base_headers(), params=params or {}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def sb_insert(table: str, row: dict):
    """INSERT one row, returns the inserted row (with generated id, timestamps, etc.)."""
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    resp = requests.post(url, headers=_base_headers(prefer="return=representation"), json=row, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) and data else data


def sb_update(table: str, params: dict, patch: dict):
    """UPDATE rows matching params (PostgREST filter syntax), returns updated rows."""
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    resp = requests.patch(
        url, headers=_base_headers(prefer="return=representation"), params=params, json=patch, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def sb_count(table: str, params: dict = None):
    """Exact row count without transferring rows — uses PostgREST's count header.
    Pass a custom 'select' in params (e.g. for embedded-table filters); otherwise
    defaults to 'id'."""
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    headers = _base_headers(prefer="count=exact")
    query = dict(params or {})
    query.setdefault("select", "id")
    query["limit"] = "0"  # head-style: get the count header without row bodies
    resp = requests.get(url, headers=headers, params=query, timeout=15)
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "*/0")
    try:
        return int(content_range.split("/")[-1])
    except ValueError:
        return 0
