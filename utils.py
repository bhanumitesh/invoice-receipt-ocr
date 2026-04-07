# ─────────────────────────────────────────────
#  utils.py  –  Shared utilities
#  Used by realtime_processor.py and batch_processor.py
# ─────────────────────────────────────────────

import base64
import hashlib
import io
import json
import traceback
from datetime import datetime

import resend

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_text_from_pdf(file) -> dict:
    """
    Attempts to extract text from a PDF file using pdfplumber.

    For each page:
      - Extracts raw text
      - Extracts tables separately and appends as structured text
      - Deduplicates exact duplicate pages using MD5 hash
      - Flags scanned pages (text below MIN_PAGE_TEXT_CHARS threshold)

    Returns:
        {
            "success":       bool   — True if enough text was extracted
            "text":          str    — full extracted text (if success)
            "page_count":    int    — total pages in PDF
            "skipped_pages": int    — pages skipped as duplicates
            "scanned_pages": int    — pages that appear to be scanned/image
            "use_fallback":  bool   — True if majority of pages are scanned
        }
    """
    file.seek(0)
    try:
        seen_hashes   = set()
        pages_text    = []
        scanned_pages = 0
        skipped_pages = 0
        total_pages   = 0

        with pdfplumber.open(file) as pdf:
            total_pages = len(pdf.pages)

            for page in pdf.pages:
                # ── Extract raw text ──
                raw_text = page.extract_text() or ""

                # ── Extract tables and convert to readable text ──
                table_text = ""
                try:
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            cleaned = [str(cell).strip() if cell else "" for cell in row]
                            if any(cleaned):
                                table_text += "  |  ".join(cleaned) + "\n"
                except Exception:
                    pass

                combined = (raw_text + "\n" + table_text).strip()

                # ── Check if scanned/image page ──
                if len(combined) < config.MIN_PAGE_TEXT_CHARS:
                    scanned_pages += 1
                    continue

                # ── Deduplicate exact pages ──
                page_hash = hashlib.md5(combined.encode("utf-8")).hexdigest()
                if page_hash in seen_hashes:
                    skipped_pages += 1
                    continue
                seen_hashes.add(page_hash)

                pages_text.append(combined)

        # If majority of pages are scanned, fall back to PDF binary
        use_fallback = scanned_pages > (total_pages / 2)

        full_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text)

        return {
            "success":       bool(full_text.strip()) and not use_fallback,
            "text":          full_text,
            "page_count":    total_pages,
            "skipped_pages": skipped_pages,
            "scanned_pages": scanned_pages,
            "use_fallback":  use_fallback,
        }

    except Exception as e:
        return {
            "success":       False,
            "text":          "",
            "page_count":    0,
            "skipped_pages": 0,
            "scanned_pages": 0,
            "use_fallback":  True,
            "error":         str(e),
        }


# ── Duplicate invoice number detection ───────────────────────────────────────

def deduplicate_items(items: list) -> tuple:
    """
    Removes items with duplicate invoice numbers, keeping first occurrence.
    Only active when config.SKIP_DUPLICATE_INVOICE_NUMBERS is True.

    Returns:
        (deduplicated_items, list_of_warning_strings)
    """
    if not config.SKIP_DUPLICATE_INVOICE_NUMBERS:
        return items, []

    seen_invoice_nos = {}
    deduplicated    = []
    warnings        = []

    for item in items:
        inv_no = str(item.get("in", "") or "").strip()

        # Skip blank or null invoice numbers from dedup check
        if not inv_no or inv_no.lower() in ("null", "none", ""):
            deduplicated.append(item)
            continue

        if inv_no not in seen_invoice_nos:
            seen_invoice_nos[inv_no] = item.get("pn", "Unknown vendor")
            deduplicated.append(item)
        else:
            warn = (
                f"Duplicate invoice number '{inv_no}' "
                f"(vendor: {item.get('pn', 'Unknown')}) — skipped. "
                f"First occurrence kept from: {seen_invoice_nos[inv_no]}"
            )
            warnings.append(warn)

    return deduplicated, warnings


# ── Cost calculation ──────────────────────────────────────────────────────────

def calculate_cost(input_tokens: int, output_tokens: int) -> dict:
    input_cost  = (input_tokens  / 1_000_000) * config.PRICE_INPUT_PER_MTOK
    output_cost = (output_tokens / 1_000_000) * config.PRICE_OUTPUT_PER_MTOK
    total_cost  = input_cost + output_cost
    return {
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "input_cost_usd":  round(input_cost,  6),
        "output_cost_usd": round(output_cost, 6),
        "total_cost_usd":  round(total_cost,  6),
    }


def format_cost_summary(cost: dict, mode: str, realtime_cost: dict = None) -> str:
    lines = [
        f"Processing Mode   : {mode}",
        f"Input tokens      : {cost['input_tokens']:,}",
        f"Output tokens     : {cost['output_tokens']:,}",
        f"Input cost        : ${cost['input_cost_usd']:.4f}",
        f"Output cost       : ${cost['output_cost_usd']:.4f}",
        f"Total cost        : ${cost['total_cost_usd']:.4f}",
    ]
    if mode == "Batch API" and realtime_cost:
        saving = realtime_cost["total_cost_usd"] - cost["total_cost_usd"]
        lines.append(f"Saved vs Real-time: ${saving:.4f}  (50% Batch discount)")
    return "\n".join(lines)


# ── JSON parsing ──────────────────────────────────────────────────────────────

def parse_json_response(raw_text: str) -> list:
    """
    Parses the abbreviated JSON array returned by Claude.
    Expands abbreviated keys to full names for use in Excel.
    Strips markdown fences if present.
    """
    # Abbreviated key → full key mapping
    KEY_MAP = {
        "s":  "sr_no",
        "pn": "party_name",
        "g":  "gstin",
        "in": "invoice_no",
        "id": "invoice_date",
        "d":  "description",
        "q":  "qty",
        "r":  "rate",
        "tv": "taxable_value",
        "cg": "cgst",
        "sg": "sgst",
        "ig": "igst",
        "h":  "hsn_code",
        "t":  "total_value",
    }

    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text  = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array at top level.")

        # Expand abbreviated keys
        expanded = []
        for item in data:
            expanded.append({
                KEY_MAP.get(k, k): (v if v is not None else "")
                for k, v in item.items()
            })
        return expanded

    except json.JSONDecodeError as e:
        # Detect truncation — happens when Claude hits max_tokens mid-response
        truncated = (
            not text.rstrip().endswith("]")
            or text.count("{") != text.count("}")
        )
        if truncated:
            raise ValueError(
                f"Output truncated — Claude hit the max_tokens limit ({config.MAX_TOKENS} tokens). "
                f"The JSON was cut off mid-response. Try splitting your files into smaller batches."
            )
        raise ValueError(f"JSON parse error: {e}\n\nRaw text:\n{text[:500]}")


# ── Excel creation ────────────────────────────────────────────────────────────

HEADERS = [
    "Sr No", "Party Name", "GSTIN", "Invoice No", "Invoice Date",
    "Description of Item", "Qty", "Rate", "Taxable Value",
    "CGST", "SGST", "IGST", "HSN Code", "Total Value",
]

FIELD_KEYS = [
    "sr_no", "party_name", "gstin", "invoice_no", "invoice_date",
    "description", "qty", "rate", "taxable_value",
    "cgst", "sgst", "igst", "hsn_code", "total_value",
]

COL_WIDTHS = [6, 30, 22, 22, 13, 46, 6, 14, 15, 12, 12, 12, 11, 14]


def create_excel(items: list, dup_warnings: list = None) -> bytes:
    """
    Creates a formatted Excel file from extracted invoice items.
    Optionally adds a Warnings sheet if duplicate invoices were skipped.
    Returns file as bytes.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice Register"

    thin         = Side(style="thin", color="BBBBBB")
    border       = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill  = PatternFill("solid", start_color="1F4E79", end_color="1F4E79")
    header_font  = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    alt_fill     = PatternFill("solid", start_color="EBF3FB", end_color="EBF3FB")
    white_fill   = PatternFill("solid", start_color="FFFFFF", end_color="FFFFFF")
    warn_fill    = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

    # ── Header row ──
    for col, header in enumerate(HEADERS, 1):
        cell            = ws.cell(row=1, column=col, value=header)
        cell.font       = header_font
        cell.fill       = header_fill
        cell.alignment  = center_align
        cell.border     = border
    ws.row_dimensions[1].height = 30

    # ── Data rows ──
    for r_idx, item in enumerate(items, 2):
        fill = alt_fill if r_idx % 2 == 0 else white_fill
        for c_idx, key in enumerate(FIELD_KEYS, 1):
            val  = item.get(key, "")
            cell = ws.cell(row=r_idx, column=c_idx, value=str(val) if val else "")
            cell.font      = Font(name="Arial", size=9)
            cell.fill      = fill
            cell.border    = border
            cell.alignment = left_align if c_idx in (2, 6) else center_align

    # ── Column widths ──
    for i, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # ── Warnings sheet (if duplicates were skipped) ──
    if dup_warnings:
        ws2 = wb.create_sheet("Duplicate Warnings")
        ws2.column_dimensions["A"].width = 100
        ws2.cell(row=1, column=1, value="Duplicate Invoice Warnings").font = Font(
            name="Arial", bold=True, size=11, color="CC0000"
        )
        for i, warn in enumerate(dup_warnings, 2):
            cell      = ws2.cell(row=i, column=1, value=warn)
            cell.font = Font(name="Arial", size=9)
            cell.fill = warn_fill

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(
    excel_bytes:    bytes,
    cost:           dict,
    mode:           str,
    file_count:     int,
    item_count:     int,
    dup_warnings:   list = None,
    realtime_cost:  dict = None,
    batch_id:       str  = None,
) -> tuple:
    """
    Sends the Excel file as an email attachment via Resend API.
    Uses HTTPS (port 443) — works on all hosting platforms including Render free tier.
    Returns (success: bool, message: str)
    """
    resend.api_key = config.RESEND_API_KEY

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    filename  = f"Invoice_Register_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    subject   = f"Invoice Register Ready - {file_count} file(s) | {timestamp}"

    dup_section = ""
    if dup_warnings:
        dup_section = (
            "\n-- Duplicate Invoice Warnings --\n"
            + "\n".join(f"  * {w}" for w in dup_warnings)
            + "\n"
        )

    body = (
        f"Hi,\n\n"
        f"Your invoice extraction is complete.\n\n"
        f"-- Summary --\n"
        f"Files processed      : {file_count}\n"
        f"Line items extracted : {item_count}\n"
        f"Processed at         : {timestamp}\n"
        + (f"Batch ID             : {batch_id}\n" if batch_id else "")
        + f"\n-- Cost --\n"
        f"{format_cost_summary(cost, mode, realtime_cost)}\n"
        + dup_section
        + f"\n-- Note --\n"
        f"The Excel file is attached.\n"
        f"All values are extracted directly from source documents.\n"
        f"Missing fields are left blank.\n"
        + (f"See 'Duplicate Warnings' sheet in Excel for skipped invoices.\n" if dup_warnings else "")
        + "\nInvoice Processor MVP\n"
    )

    # Support multiple recipients — RECIPIENT_EMAIL can be comma-separated
    # e.g. "a@gmail.com,b@gmail.com"
    recipients = [r.strip() for r in config.RECIPIENT_EMAIL.split(",") if r.strip()]

    # Resend requires attachment as base64 string
    excel_b64 = base64.b64encode(excel_bytes).decode("utf-8")

    try:
        params = {
            "from":        config.RESEND_SENDER,
            "to":          recipients,
            "subject":     subject,
            "text":        body,
            "attachments": [
                {
                    "filename": filename,
                    "content":  excel_b64,
                }
            ],
        }
        response = resend.Emails.send(params)
        # Resend returns {"id": "..."} on success
        if response and response.get("id"):
            return True, filename
        else:
            return False, f"Resend returned unexpected response: {response}"
    except Exception:
        return False, traceback.format_exc()
