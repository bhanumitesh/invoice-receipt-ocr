# ─────────────────────────────────────────────
#  realtime_processor.py  –  Real-time API processing
# ─────────────────────────────────────────────

import traceback

import anthropic

import config
from utils import (
    build_file_content,
    calculate_cost,
    create_tally_xml,
    deduplicate_items,
    parse_json_response,
)


def process_realtime(uploaded_files: list) -> dict:
    """
    Sends all uploaded PDFs to Claude in a single real-time API call.

    For each file, per page:
      - Native text layer (cheapest) if present
      - Else local OCR if the page has no handwriting/stamps (see build_file_content)
      - Else sent to Claude as an image

    Returns:
        dict with keys:
            success      : bool
            items        : list of extracted line item dicts
            cost         : cost dict
            dup_warnings : list of duplicate invoice warnings
            fallback_files: list of filenames with at least one page sent as an image
            error        : str or None
    """
    client           = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    content          = []
    fallback_files   = []
    extraction_notes = []
    total_pages      = 0   # for credit deduction — 1 credit per page

    for f in uploaded_files:
        built = build_file_content(f)
        total_pages += built["page_count"]
        content.extend(built["content"])
        if built["fallback_pages"] > 0:
            fallback_files.append(f.name)
        if built["notes"]:
            extraction_notes.extend(built["notes"])

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
                    f"Output was truncated "
                    f"({config.MAX_TOKENS} tokens). The response was cut off mid-JSON. "
                    f"Try uploading fewer files at once, or switch to Batch API mode "
                    f"which handles larger outputs more reliably."
                ),
                "total_pages": total_pages,
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

        tally_erp9_bytes  = create_tally_xml(items, "erp9")
        tally_prime_bytes = create_tally_xml(items, "prime")

        return {
            "success":            True,
            "items":              items,
            "cost":               cost,
            "dup_warnings":       dup_warnings,
            "fallback_files":     fallback_files,
            "extraction_notes":   extraction_notes,
            "error":              None,
            "tally_erp9_bytes":   tally_erp9_bytes,
            "tally_prime_bytes":  tally_prime_bytes,
            "total_pages":        total_pages,
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
            "total_pages":      total_pages,
        }
