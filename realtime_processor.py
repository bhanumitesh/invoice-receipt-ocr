# ─────────────────────────────────────────────
#  realtime_processor.py  –  Real-time API processing
# ─────────────────────────────────────────────

import base64
import traceback

import anthropic

import config
from utils import (
    calculate_cost,
    deduplicate_items,
    extract_text_from_pdf,
    parse_json_response,
)


def process_realtime(uploaded_files: list) -> dict:
    """
    Sends all uploaded PDFs to Claude in a single real-time API call.

    For each file:
      - Attempts text extraction via pdfplumber (cheaper, fewer tokens)
      - Falls back to raw PDF binary if file is scanned/image-based

    Returns:
        dict with keys:
            success      : bool
            items        : list of extracted line item dicts
            cost         : cost dict
            dup_warnings : list of duplicate invoice warnings
            fallback_files: list of filenames that used PDF fallback
            error        : str or None
    """
    client        = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    content       = []
    fallback_files = []
    extraction_notes = []

    for f in uploaded_files:
        extraction = extract_text_from_pdf(f)

        if extraction["success"] and not extraction["use_fallback"]:
            # ── Text extraction succeeded — send as text (fewer tokens) ──
            notes = []
            if extraction["skipped_pages"] > 0:
                notes.append(f"{extraction['skipped_pages']} duplicate page(s) skipped")
            if extraction["scanned_pages"] > 0:
                notes.append(f"{extraction['scanned_pages']} scanned page(s) skipped")

            header = f"=== FILE: {f.name} ==="
            if notes:
                header += f" [{', '.join(notes)}]"

            content.append({
                "type": "text",
                "text": header + "\n\n" + extraction["text"],
            })
            if notes:
                extraction_notes.append(f"{f.name}: {', '.join(notes)}")
        else:
            # ── Fallback — send raw PDF binary ──
            f.seek(0)
            pdf_bytes = f.read()
            b64_data  = base64.standard_b64encode(pdf_bytes).decode("utf-8")
            content.append({
                "type": "document",
                "source": {
                    "type":       "base64",
                    "media_type": "application/pdf",
                    "data":       b64_data,
                },
                "title": f.name,
            })
            fallback_files.append(f.name)

    content.append({
        "type": "text",
        "text": config.EXTRACTION_PROMPT,
    })

    try:
        response = client.messages.create(
            model      = config.MODEL,
            max_tokens = config.MAX_TOKENS,
            messages   = [{"role": "user", "content": content}],
        )

        raw_text    = response.content[0].text
        stop_reason = response.stop_reason

        # Detect truncation before attempting parse
        if stop_reason == "max_tokens":
            return {
                "success":          False,
                "items":            [],
                "cost":             calculate_cost(
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                ),
                "dup_warnings":     [],
                "fallback_files":   fallback_files,
                "extraction_notes": extraction_notes,
                "error": (
                    f"Output was truncated — Claude hit the max_tokens limit "
                    f"({config.MAX_TOKENS} tokens). The response was cut off mid-JSON. "
                    f"Try uploading fewer files at once, or switch to Batch API mode "
                    f"which handles larger outputs more reliably."
                ),
            }

        items = parse_json_response(raw_text)

        # Deduplicate by invoice number
        items, dup_warnings = deduplicate_items(items)

        # Re-number sr_no after dedup
        for idx, item in enumerate(items, 1):
            item["sr_no"] = idx

        cost = calculate_cost(
            input_tokens  = response.usage.input_tokens,
            output_tokens = response.usage.output_tokens,
        )

        return {
            "success":          True,
            "items":            items,
            "cost":             cost,
            "dup_warnings":     dup_warnings,
            "fallback_files":   fallback_files,
            "extraction_notes": extraction_notes,
            "error":            None,
        }

    except Exception:
        return {
            "success":          False,
            "items":            [],
            "cost":             None,
            "dup_warnings":     [],
            "fallback_files":   fallback_files,
            "extraction_notes": extraction_notes,
            "error":            traceback.format_exc(),
        }
