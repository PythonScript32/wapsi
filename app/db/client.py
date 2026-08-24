"""
Supabase client factory.

Two keys, two very different privileges:
  - SERVICE key : backend only. Bypasses RLS. NEVER ship to the frontend.
  - ANON key    : the React dashboard. Read-only via RLS policies.

If SUPABASE_URL is unset we fail loudly rather than silently writing nowhere.
"""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY missing. Copy .env.example to "
            ".env and fill them in (Supabase → Project Settings → API)."
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
