# ─────────────────────────────────────────────
#  config.py  –  All settings for Invoice Processor MVP
#
#  DEPLOYMENT NOTE:
#  Sensitive values are read from environment variables.
#  Set these on your server before running:
#
#    ANTHROPIC_API_KEY   → your Anthropic API key
#    GMAIL_SENDER        → Gmail address used to send emails
#    GMAIL_APP_PASS      → Gmail App Password (16 chars)
#    RECIPIENT_EMAIL     → email address to receive Excel reports
#
#  For local development, create a .env file and load it with:
#    pip install python-dotenv
#    and add: from dotenv import load_dotenv; load_dotenv()
#    at the top of app.py
#
#  Non-sensitive settings are hardcoded below and can be
#  overridden via env vars too if needed.
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

# ── Email (required) ───────────────────────────────────────────────────────
GMAIL_SENDER    = _require("GMAIL_SENDER")
GMAIL_APP_PASS  = _require("GMAIL_APP_PASS")
# Supports multiple recipients — separate with commas:
# e.g. "a@gmail.com,b@gmail.com"
RECIPIENT_EMAIL = _require("RECIPIENT_EMAIL")

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
