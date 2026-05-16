# ─────────────────────────────────────────────
#  auth.py  –  Authentication logic
#  OTP generation, validation, email sending
# ─────────────────────────────────────────────

import random
import string
import traceback
from datetime import datetime, timedelta, timezone

import resend

import config
from db import get_user, save_otp, verify_otp


def generate_otp(length: int = 6) -> str:
    """Generates a numeric OTP of given length."""
    return "".join(random.choices(string.digits, k=length))


def send_otp_email(email: str, otp: str) -> dict:
    """
    Sends OTP to the user's email via Resend.
    Returns {"success": bool, "error": str or None}
    """
    resend.api_key = config.RESEND_API_KEY

    body = (
        f"Hi,\n\n"
        f"Your Invoice Processor login OTP is:\n\n"
        f"    {otp}\n\n"
        f"This OTP is valid for {config.OTP_EXPIRY_MINUTES} minutes.\n"
        f"Do not share this with anyone.\n\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"Invoice Processor\n"
    )

    try:
        params = {
            "from":    config.RESEND_SENDER,
            "to":      [email],
            "subject": f"Your Invoice Processor OTP: {otp}",
            "text":    body,
        }
        response = resend.Emails.send(params)
        if response and response.get("id"):
            return {"success": True, "error": None}
        return {"success": False, "error": f"Unexpected response: {response}"}
    except Exception:
        return {"success": False, "error": traceback.format_exc()}


def request_otp(email: str) -> dict:
    """
    Full OTP request flow:
      1. Check email is registered
      2. Check account is active
      3. Check credits > 0
      4. Generate OTP
      5. Save to DB
      6. Send via email

    Returns:
        {
            "success":  bool
            "message":  str   — shown to user
            "blocked":  bool  — True if user should not proceed (no credits, inactive)
        }
    """
    email = email.lower().strip()

    # ── Check user exists ──
    user = get_user(email)
    if user is None:
        return {
            "success": False,
            "blocked": True,
            "message": "This email is not registered. Please contact the admin to get access.",
        }

    # ── Check account is active ──
    if not user.get("is_active", False):
        return {
            "success": False,
            "blocked": True,
            "message": "Your account is inactive. Please contact the admin.",
        }

    # ── Check credits ──
    credits = user.get("credits", 0)
    if credits <= 0:
        return {
            "success": False,
            "blocked": True,
            "message": (
                f"You have 0 credits remaining. "
                f"Please contact the admin to top up your credits."
            ),
        }

    # ── Generate + save OTP ──
    otp        = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=config.OTP_EXPIRY_MINUTES)

    if not save_otp(email, otp, expires_at):
        return {
            "success": False,
            "blocked": False,
            "message": "Failed to generate OTP. Please try again.",
        }

    # ── Send OTP email ──
    result = send_otp_email(email, otp)
    if not result["success"]:
        return {
            "success": False,
            "blocked": False,
            "message": "Failed to send OTP email. Please try again.",
        }

    return {
        "success": True,
        "blocked": False,
        "message": f"OTP sent to {email}. Valid for {config.OTP_EXPIRY_MINUTES} minutes.",
        "credits": credits,
    }


def validate_otp(email: str, otp: str) -> dict:
    """
    Validates an OTP for a given email.

    Returns:
        {
            "success": bool
            "message": str
            "credits": int  — remaining credits (if success)
        }
    """
    email  = email.lower().strip()
    result = verify_otp(email, otp)

    if not result["valid"]:
        return {"success": False, "message": result["reason"], "credits": 0}

    user = get_user(email)
    return {
        "success": True,
        "message": "Login successful.",
        "credits": user.get("credits", 0) if user else 0,
    }
