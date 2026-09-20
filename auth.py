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
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client
from supabase_auth.errors import AuthApiError

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


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> dict:
    """Reusable guard: extracts the Bearer token and verifies it with Supabase.

    Applied to every protected route. Returns the verified user's metadata
    plus the raw token (needed by /auth/logout to revoke the session).
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Access token required")

    token = str(credentials.credentials)
    try:
        response = supabase.auth.get_user(token)
    except AuthApiError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    except Exception:
        raise HTTPException(status_code=503, detail="Authentication service unavailable")

    if response is None or response.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {"token": token, "user": response.user.model_dump(mode="json")}


def logout_user(token: str) -> None:
    """Revoke the user's Supabase session (POST /auth/v1/logout)."""
    try:
        supabase.auth.admin.sign_out(token)
    except Exception:  # noqa: BLE001
        # Token already expired/revoked is fine: the client is now logged out.
        pass