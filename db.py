# ─────────────────────────────────────────────
#  db.py  –  Supabase database operations
#
#  Tables required (create in Supabase SQL editor):
#
#  users:
#    id          uuid primary key default gen_random_uuid()
#    email       text unique not null
#    credits     integer not null default 0
#    is_active   boolean not null default true
#    created_at  timestamptz default now()
#
#  otp_tokens:
#    id          uuid primary key default gen_random_uuid()
#    email       text not null
#    otp         text not null
#    expires_at  timestamptz not null
#    used        boolean not null default false
#    created_at  timestamptz default now()
# ─────────────────────────────────────────────

import traceback
from datetime import datetime, timezone

from supabase import create_client

import config

# ── Supabase client (lazy init) ───────────────────────────────────────────────
_client = None

def _db():
    global _client
    if _client is None:
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


# ── User operations ───────────────────────────────────────────────────────────

def get_user(email: str) -> dict:
    """
    Fetches a user record by email.
    Returns dict with keys: email, credits, is_active
    Returns None if user not found.
    """
    try:
        res = _db().table("users").select("*").eq("email", email.lower().strip()).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception:
        return None


def get_user_credits(email: str) -> int:
    """Returns remaining credits for a user. Returns -1 on error."""
    user = get_user(email)
    if user is None:
        return -1
    return user.get("credits", 0)


def deduct_credits(email: str, pages: int) -> dict:
    """
    Deducts `pages` credits from the user's account.
    Only deducts if user has enough credits.

    Returns:
        {
            "success":           bool
            "credits_before":    int
            "credits_after":     int
            "credits_deducted":  int
            "error":             str or None
        }
    """
    try:
        user = get_user(email)
        if user is None:
            return {"success": False, "error": "User not found", "credits_before": 0,
                    "credits_after": 0, "credits_deducted": 0}

        credits_before = user.get("credits", 0)
        to_deduct      = min(pages, credits_before)  # never go below 0
        credits_after  = credits_before - to_deduct

        _db().table("users").update({"credits": credits_after}).eq(
            "email", email.lower().strip()
        ).execute()

        return {
            "success":          True,
            "credits_before":   credits_before,
            "credits_after":    credits_after,
            "credits_deducted": to_deduct,
            "error":            None,
        }
    except Exception:
        return {
            "success":          False,
            "credits_before":   0,
            "credits_after":    0,
            "credits_deducted": 0,
            "error":            traceback.format_exc(),
        }


# ── OTP operations ────────────────────────────────────────────────────────────

def save_otp(email: str, otp: str, expires_at: datetime) -> bool:
    """
    Saves a new OTP record to the database.
    Invalidates any existing unused OTPs for this email first.
    Returns True on success.
    """
    try:
        email = email.lower().strip()
        # Mark all existing unused OTPs for this email as used
        _db().table("otp_tokens").update({"used": True}).eq(
            "email", email
        ).eq("used", False).execute()

        # Insert new OTP
        _db().table("otp_tokens").insert({
            "email":      email,
            "otp":        otp,
            "expires_at": expires_at.isoformat(),
            "used":       False,
        }).execute()
        return True
    except Exception:
        return False


def verify_otp(email: str, otp: str) -> dict:
    """
    Verifies an OTP for a given email.
    Marks it as used if valid.

    Returns:
        {
            "valid":   bool
            "reason":  str  — human-readable reason if invalid
        }
    """
    try:
        email = email.lower().strip()
        now   = datetime.now(timezone.utc).isoformat()

        res = (
            _db().table("otp_tokens")
            .select("*")
            .eq("email", email)
            .eq("otp",   otp.strip())
            .eq("used",  False)
            .gt("expires_at", now)
            .execute()
        )

        if not res.data:
            # Distinguish between wrong OTP and expired OTP for better UX
            any_res = (
                _db().table("otp_tokens")
                .select("*")
                .eq("email", email)
                .eq("otp",   otp.strip())
                .execute()
            )
            if any_res.data:
                return {"valid": False, "reason": "OTP has expired. Please request a new one."}
            return {"valid": False, "reason": "Incorrect OTP. Please check and try again."}

        # Mark OTP as used
        token_id = res.data[0]["id"]
        _db().table("otp_tokens").update({"used": True}).eq("id", token_id).execute()

        return {"valid": True, "reason": ""}

    except Exception:
        return {"valid": False, "reason": "Verification failed. Please try again."}
