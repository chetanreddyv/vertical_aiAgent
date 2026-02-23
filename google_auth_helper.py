"""
google_auth_helper.py — Shared Google OAuth credential loader.

Production strategy (configure once, forget):
  1. Run `python auth_google.py` locally to generate token.json.
  2. Base64-encode it: python -c "import base64; print(base64.b64encode(open('token.json','rb').read()).decode())"
  3. Set GOOGLE_TOKEN_JSON=<that base64 string> as an Azure secret.
  4. The app reads from the env var in production, or falls back to the local file.
  5. Refreshed tokens are written back to the env-loaded credentials in-memory
     (the refresh_token never expires, so auth persists indefinitely).

Local dev:
  - Keep token.json on disk. get_google_creds() will find and use it.
"""

import os
import base64
import json
import logging
import tempfile
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# All scopes used across the application — token.json must have been generated with these
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

_cached_creds: Credentials | None = None


def get_google_creds() -> Credentials:
    """
    Returns valid Google OAuth credentials.

    Priority:
      1. GOOGLE_TOKEN_JSON env var (base64-encoded token.json) — used in production
      2. Local token.json file — used in development

    Raises RuntimeError if no credentials are found or if they can't be refreshed.
    """
    global _cached_creds

    # Return cached valid creds without re-loading
    if _cached_creds and _cached_creds.valid:
        return _cached_creds

    creds: Credentials | None = None

    # ── 1. Try env var (production) ─────────────────────────────────────────────
    token_b64 = os.getenv("GOOGLE_TOKEN_JSON")
    if token_b64:
        try:
            token_json = base64.b64decode(token_b64).decode("utf-8")
            token_data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            logger.info("🔑 Loaded Google credentials from GOOGLE_TOKEN_JSON env var")
        except Exception as e:
            logger.error(f"❌ Failed to load credentials from GOOGLE_TOKEN_JSON: {e}")
            creds = None

    # ── 2. Fallback to local file (dev) ─────────────────────────────────────────
    if creds is None:
        local_path = os.path.join(os.path.dirname(__file__), "token.json")
        if os.path.exists(local_path):
            creds = Credentials.from_authorized_user_file(local_path, SCOPES)
            logger.info(f"🔑 Loaded Google credentials from local {local_path}")
        else:
            raise RuntimeError(
                "No Google credentials found. Either:\n"
                "  • Set GOOGLE_TOKEN_JSON env var (base64-encoded token.json), or\n"
                "  • Run `python auth_google.py` to create a local token.json"
            )

    # ── 3. Refresh if expired ────────────────────────────────────────────────────
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            logger.info("🔄 Refreshing expired Google credentials...")
            creds.refresh(Request())
            logger.info("✅ Google credentials refreshed successfully")
            # Persist back to local file if it exists (dev convenience)
            local_path = os.path.join(os.path.dirname(__file__), "token.json")
            if os.path.exists(local_path):
                with open(local_path, "w") as f:
                    f.write(creds.to_json())
        else:
            raise RuntimeError(
                "Google credentials are invalid and cannot be refreshed. "
                "Please re-run `python auth_google.py` and update GOOGLE_TOKEN_JSON."
            )

    _cached_creds = creds
    return creds
