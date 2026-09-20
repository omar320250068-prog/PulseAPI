"""Supabase-backed auth wiring for the FastAPI app.

This module owns the Supabase client and a startup connectivity check.
The Bearer-token guard (middleware/dependency) is added in later stages.

Environment variables (from .env):

    SUPABASE_URL  (Project Settings -> API -> Project URL)
    SUPABASE_KEY  (Project Settings -> API -> anon public key)
"""

import os

import httpx
from dotenv import load_dotenv
from fastapi.security import HTTPBearer
from supabase import Client, create_client

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

# FastAPI security scheme -> renders the "Authorize" lock in Swagger UI.
bearer_scheme = HTTPBearer(auto_error=False)


def build_client() -> Client:
    """Create a stateless Supabase client (no session persisted on the server).

    Note: an explicit ClientOptions is intentionally avoided because the
    installed supabase/supabase-auth versions crash when one is passed.
    """
    return create_client(SUPABASE_URL, SUPABASE_KEY)


supabase: Client = build_client()


def check_supabase_connection() -> tuple[bool, str]:
    """Ping Supabase Auth so the server can log its readiness on startup."""
    try:
        response = httpx.get(
            f"{SUPABASE_URL.rstrip('/')}/auth/v1/settings",
            headers={"apikey": SUPABASE_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            return True, "Server running and connected to Supabase"
        return False, f"Supabase responded with status {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not reach Supabase ({SUPABASE_URL}): {exc}"