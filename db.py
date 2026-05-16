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

import hashlib
import secrets
import traceback
from datetime import datetime, timedelta, timezone

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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(email: str, days: int = 30) -> dict:
    """Creates a persisted browser session token for a validated user."""
    try:
        email = email.lower().strip()
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        _db().table("auth_sessions").insert({
            "email": email,
            "token_hash": _hash_token(token),
            "expires_at": expires_at.isoformat(),
            "revoked": False,
        }).execute()
        return {"success": True, "token": token, "expires_at": expires_at}
    except Exception:
        return {"success": False, "token": None, "error": traceback.format_exc()}


def get_session_user(token: str) -> dict:
    """Returns the active user for a persisted session token, or None."""
    if not token:
        return None
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = (
            _db().table("auth_sessions")
            .select("*")
            .eq("token_hash", _hash_token(token))
            .eq("revoked", False)
            .gt("expires_at", now)
            .execute()
        )
        if not res.data:
            return None
        user = get_user(res.data[0]["email"])
        if user and user.get("is_active", False):
            return user
        return None
    except Exception:
        return None


def revoke_session(token: str) -> bool:
    """Revokes a persisted session token."""
    if not token:
        return True
    try:
        _db().table("auth_sessions").update({"revoked": True}).eq(
            "token_hash", _hash_token(token)
        ).execute()
        return True
    except Exception:
        return False


def _rpc_data(response):
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return data[0] if data else None
    return data


def get_credit_transaction(job_id: str) -> dict:
    if not job_id:
        return None
    try:
        res = _db().table("credit_transactions").select("*").eq("job_id", job_id).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def reserve_credits(email: str, pages: int, job_id: str, mode: str) -> dict:
    """Atomically reserves credits before processing starts."""
    try:
        response = _db().rpc("reserve_credits", {
            "p_email": email.lower().strip(),
            "p_job_id": job_id,
            "p_pages": max(int(pages or 0), 0),
            "p_mode": mode,
        }).execute()
        data = _rpc_data(response) or {}
        return {
            "success": bool(data.get("success")),
            "already_reserved": bool(data.get("already_reserved", False)),
            "credits_before": data.get("credits_before", 0),
            "credits_after": data.get("credits_after", 0),
            "credits_reserved": data.get("credits_reserved", data.get("credits_deducted", 0)),
            "error": data.get("error"),
        }
    except Exception:
        return {
            "success": False,
            "already_reserved": False,
            "credits_before": 0,
            "credits_after": 0,
            "credits_reserved": 0,
            "error": traceback.format_exc(),
        }


def finalize_credit_reservation(job_id: str) -> dict:
    """Marks a reserved credit transaction as completed. No extra credits move."""
    try:
        response = _db().rpc("finalize_credit_reservation", {
            "p_job_id": job_id,
        }).execute()
        data = _rpc_data(response) or {}
        return {
            "success": bool(data.get("success")),
            "status": data.get("status"),
            "error": data.get("error"),
        }
    except Exception:
        return {"success": False, "status": None, "error": traceback.format_exc()}


def refund_credit_reservation(job_id: str, reason: str = None) -> dict:
    """Refunds a reserved transaction once if processing fails."""
    try:
        response = _db().rpc("refund_credit_reservation", {
            "p_job_id": job_id,
            "p_reason": reason or "Processing failed",
        }).execute()
        data = _rpc_data(response) or {}
        return {
            "success": bool(data.get("success")),
            "already_refunded": bool(data.get("already_refunded", False)),
            "credits_before": data.get("credits_before", 0),
            "credits_after": data.get("credits_after", 0),
            "credits_refunded": data.get("credits_refunded", 0),
            "error": data.get("error"),
        }
    except Exception:
        return {
            "success": False,
            "already_refunded": False,
            "credits_before": 0,
            "credits_after": 0,
            "credits_refunded": 0,
            "error": traceback.format_exc(),
        }


# Backward-compatible wrapper for any older call sites. Prefer reserve/finalize/refund.
def deduct_credits(email: str, pages: int, job_id: str = None) -> dict:
    job_id = job_id or f"legacy_{secrets.token_urlsafe(12)}"
    reserved = reserve_credits(email, pages, job_id, mode="legacy")
    if reserved["success"]:
        finalize_credit_reservation(job_id)
        return {
            "success": True,
            "already_deducted": reserved.get("already_reserved", False),
            "credits_before": reserved.get("credits_before", 0),
            "credits_after": reserved.get("credits_after", 0),
            "credits_deducted": reserved.get("credits_reserved", 0),
            "error": None,
        }
    return {
        "success": False,
        "already_deducted": False,
        "credits_before": reserved.get("credits_before", 0),
        "credits_after": reserved.get("credits_before", 0),
        "credits_deducted": 0,
        "error": reserved.get("error"),
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
