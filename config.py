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
# MAX_TOKENS       : used by the real-time (synchronous Messages API) path.
#                     Sonnet 4.6's standard sync cap is 128,000 — keep this at
#                     or below that.
# BATCH_MAX_TOKENS : used by the Batch API path, which submits the
#                     `output-300k-2026-03-24` beta header (see
#                     batch_processor.py) to raise the per-request cap to
#                     300,000 — far larger than any single invoice needs, so a
#                     lower default keeps typical jobs fast. Raise via env var
#                     if a single very dense invoice still truncates.
MAX_TOKENS       = int(_optional("MAX_TOKENS", "8192"))
BATCH_MAX_TOKENS = int(_optional("BATCH_MAX_TOKENS", "32000"))

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

# ── Local OCR (scanned pages without a text layer) ─────────────────────────
# For pages with no native text layer, a page is first checked for
# handwriting/stamps (colored, non-black ink — pen and rubber stamps are
# almost always blue/purple/red, unlike printed black text). Clean pages go
# through local Tesseract OCR (free, fast, no Claude cost); pages with
# handwriting/stamps skip OCR and are sent to Claude as an image instead,
# since OCR has been observed to silently mis-read handwritten numbers
# (e.g. "9000" read as "2000") without a low-confidence signal to catch it —
# too risky for financial figures.
# HANDWRITING_INK_THRESHOLD: fraction (0-1) of non-white, non-black/gray
# pixels on a page above which it's treated as annotated. Validated empirically:
# clean printed pages measure ~0%, pages with handwriting/stamps measure 4-8%+.
HANDWRITING_INK_THRESHOLD = float(_optional("HANDWRITING_INK_THRESHOLD", "0.005"))
# Same render is used both for the local OCR attempt and, if a page ends up
# needing the image-fallback path instead, as the image sent to Claude — so
# this shouldn't be pushed too low even though lower helps OCR speed, since
# accuracy on the fallback path is the whole reason that path exists.
OCR_RENDER_RESOLUTION_DPI = int(_optional("OCR_RENDER_RESOLUTION_DPI", "200"))

# Images sent to Claude are JPEG (not PNG) — PNG's lossless compression is a
# poor fit for noisy photographic scan content and can run 3x+ larger than a
# JPEG at this quality for the same page, which matters because the whole
# request (all images + text) must fit under Anthropic's 32MB request-size
# limit. Real 20-page fully-scanned files have been observed at 35MB+ as PNG
# vs ~10MB as JPEG at this quality.
IMAGE_JPEG_QUALITY = int(_optional("IMAGE_JPEG_QUALITY", "85"))

# Safety margin under Anthropic's 32MB hard request-size limit — a file whose
# combined content (text + all fallback images) still exceeds this after JPEG
# compression fails clearly with a "file too large" error instead of being
# submitted and rejected by the API with an opaque error.
MAX_REQUEST_PAYLOAD_MB = float(_optional("MAX_REQUEST_PAYLOAD_MB", "25"))

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
