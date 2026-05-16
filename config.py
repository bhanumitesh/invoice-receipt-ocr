# ─────────────────────────────────────────────
#  config.py  –  All settings for Invoice Processor MVP
#
#  DEPLOYMENT NOTE:
#  All sensitive values are read from environment variables.
#  For local development use a .env file (see .env.example).
#  For server deployment (Render etc.) set env vars directly.
# ─────────────────────────────────────────────

import os
import sys


def _require(var: str) -> str:
    """
    Reads a required environment variable.
    Exits with a clear error message if it is not set.
    This prevents the app from starting silently with missing config.
    """
    val = os.environ.get(var, "").strip()
    if not val:
        print(
            f"\n[ERROR] Required environment variable '{var}' is not set.\n"
            f"  Set it on your server or in a local .env file before running.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def _optional(var: str, default: str) -> str:
    """Reads an optional environment variable, returning default if not set."""
    return os.environ.get(var, "").strip() or default


# ── Anthropic (required) ───────────────────────────────────────────────────
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
MODEL             = _optional("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ── Pricing (optional — override via env if rates change) ─────────────────
PRICE_INPUT_PER_MTOK  = float(_optional("PRICE_INPUT_PER_MTOK",  "3.00"))
PRICE_OUTPUT_PER_MTOK = float(_optional("PRICE_OUTPUT_PER_MTOK", "15.00"))

# ── API output settings ────────────────────────────────────────────────────
MAX_TOKENS = int(_optional("MAX_TOKENS", "8192"))

# ── Email / Resend (required) ──────────────────────────────────────────────
# Sign up free at resend.com — 3,000 emails/month permanently free
# RESEND_API_KEY : API key from resend.com dashboard
# RESEND_SENDER  : verified sender address, e.g. "Invoice Processor <invoices@yourdomain.com>"
#                  On Resend free tier you can use "onboarding@resend.dev" for testing
#                  For production, verify your own domain at resend.com/domains
# ADMIN_EMAIL: supports multiple comma-separated admin addresses
#              e.g. "admin1@gmail.com,admin2@gmail.com"
RESEND_API_KEY  = _require("RESEND_API_KEY")
RESEND_SENDER   = _require("RESEND_SENDER")

# ── Email recipients ───────────────────────────────────────────────────────
# ADMIN_EMAIL: always receives the output files (Excel + Tally XML)
#              comma-separated for multiple admins
# Note: the logged-in user's email is also always added as a recipient
#       automatically — no need to list users here
ADMIN_EMAIL = _require("ADMIN_EMAIL")

# ── Supabase (required for auth + credits) ─────────────────────────────────
# Sign up free at supabase.com — 500MB database permanently free
# SUPABASE_URL : Project URL from Supabase dashboard → Settings → API
# SUPABASE_KEY : anon/public key from Supabase dashboard → Settings → API
SUPABASE_URL = _require("SUPABASE_URL")
SUPABASE_KEY = _require("SUPABASE_KEY")

# ── OTP settings ───────────────────────────────────────────────────────────
OTP_EXPIRY_MINUTES = int(_optional("OTP_EXPIRY_MINUTES", "10"))

# ── Tally XML settings ────────────────────────────────────────────────────
# Default ledger all purchase line items post to.
# CA reassigns to correct ledgers inside Tally after import.
# Set this to whatever your CA's standard purchases ledger is named in Tally.
TALLY_DEFAULT_LEDGER  = _optional("TALLY_DEFAULT_LEDGER",  "Purchase Account")

# Name of your company exactly as it appears in Tally
# Used in the XML CompanyName field
TALLY_COMPANY_NAME    = _optional("TALLY_COMPANY_NAME",    "My Company")

# ── Batch API settings ─────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(_optional("POLL_INTERVAL_SECONDS", "120"))

# ── Deduplication ──────────────────────────────────────────────────────────
SKIP_DUPLICATE_INVOICE_NUMBERS = _optional(
    "SKIP_DUPLICATE_INVOICE_NUMBERS", "true"
).lower() == "true"

# ── PDF text extraction ────────────────────────────────────────────────────
MIN_PAGE_TEXT_CHARS = int(_optional("MIN_PAGE_TEXT_CHARS", "50"))

# ── Extraction prompt ──────────────────────────────────────────────────────
# Uses abbreviated JSON keys to minimise output tokens.
# Key map (used in utils.py to expand back to full names for Excel):
#   s  = sr_no          pn = party_name      g  = gstin
#   in = invoice_no     id = invoice_date    d  = description
#   q  = qty            r  = rate            tv = taxable_value
#   cg = cgst           sg = sgst            ig = igst
#   h  = hsn_code       t  = total_value
#
# null is used for missing fields — shorter than "N/A" or any string.

EXTRACTION_PROMPT = """
You are an expert invoice data extraction assistant.

Extract ALL line items from the attached invoice text and return a JSON array.
Each line item must be its own object.

Return ONLY a valid JSON array. No preamble, no markdown, no explanation.

Each object must have exactly these abbreviated keys:
{
  "s":  <integer — sequential line number>,
  "pn": <vendor/supplier name as printed>,
  "g":  <vendor GSTIN or null>,
  "in": <invoice or document number>,
  "id": <date as printed, e.g. "26-Sep-2024">,
  "d":  <description of item or service>,
  "q":  <quantity as printed or null>,
  "r":  <unit rate with currency symbol or null>,
  "tv": <taxable value with currency symbol or null>,
  "cg": <CGST amount with currency symbol or null>,
  "sg": <SGST amount with currency symbol or null>,
  "ig": <IGST amount with currency symbol or null>,
  "h":  <HSN or SAC code or null>,
  "t":  <total line value with currency symbol as printed>
}

Rules:
- Detect currency from the document and use the correct symbol (Rs. $ etc.)
- Use null (not "N/A", not "") for any field not present in the document
- Do NOT calculate any values — extract exactly as printed
- For TDS challans or payment receipts treat the full payment as one line item
- Do NOT deduplicate — extract every line item from every page
- s must be a plain integer starting from 1
"""
