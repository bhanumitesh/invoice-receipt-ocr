# ─────────────────────────────────────────────
#  batch_processor.py  –  Batch API processing
#
#  Thread safety:
#    Background thread NEVER touches st.session_state.
#    All communication to UI is via two files in batch_logs/:
#      batch_<id>.log    → append-only human-readable log
#      batch_<id>.status → JSON written once when done; app.py polls this
# ─────────────────────────────────────────────

import base64
import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import anthropic

import config
from utils import (
    calculate_cost,
    create_excel,
    deduplicate_items,
    extract_text_from_pdf,
    parse_json_response,
    send_email,
)

# ── Log directory ─────────────────────────────────────────────────────────────
LOG_DIR = Path("batch_logs")
LOG_DIR.mkdir(exist_ok=True)


# ── Log / status file helpers ─────────────────────────────────────────────────

def _log_path(batch_id: str)    -> Path: return LOG_DIR / f"batch_{batch_id}.log"
def _status_path(batch_id: str) -> Path: return LOG_DIR / f"batch_{batch_id}.status"


def write_log(batch_id: str, msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"[{timestamp}] {msg}\n"
    print(line, end="")
    with open(_log_path(batch_id), "a", encoding="utf-8") as f:
        f.write(line)


def read_logs(batch_id: str) -> list:
    path = _log_path(batch_id)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f.readlines()]


def write_status(batch_id: str, result: dict):
    safe = {k: v for k, v in result.items() if k != "excel_bytes"}
    with open(_status_path(batch_id), "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2, default=str)


def read_status(batch_id: str) -> dict:
    path = _status_path(batch_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_batch_files(batch_id: str):
    for path in [_log_path(batch_id), _status_path(batch_id)]:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


# ── Build content blocks (shared by submit) ───────────────────────────────────

def _build_content(uploaded_files: list, batch_id: str = None) -> tuple:
    """
    Builds Claude content blocks from uploaded files.
    Uses text extraction where possible; falls back to PDF binary for scanned files.

    Returns: (content_blocks, fallback_files, extraction_notes)
    """
    content          = []
    fallback_files   = []
    extraction_notes = []

    for f in uploaded_files:
        extraction = extract_text_from_pdf(f)

        if extraction["success"] and not extraction["use_fallback"]:
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

            if batch_id:
                write_log(batch_id, f"{f.name} → text extraction "
                          f"({extraction['page_count']} pages"
                          + (f", {extraction['skipped_pages']} dup pages skipped" if extraction['skipped_pages'] else "")
                          + ")")
        else:
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
            if batch_id:
                write_log(batch_id, f"{f.name} → PDF fallback (scanned/image-based)")

    content.append({
        "type": "text",
        "text": config.EXTRACTION_PROMPT,
    })

    return content, fallback_files, extraction_notes


# ── Submit ────────────────────────────────────────────────────────────────────

def submit_batch(uploaded_files: list) -> dict:
    """
    Submits all uploaded PDFs as a single Batch API job.

    Returns:
        dict: success, batch_id, fallback_files, extraction_notes, error
    """
    client   = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    batch_id = None

    try:
        # Build content — no batch_id yet so no log writes during build
        content, fallback_files, extraction_notes = _build_content(uploaded_files)

        batch = client.messages.batches.create(
            requests=[
                {
                    "custom_id": f"invoice_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    "params": {
                        "model":      config.MODEL,
                        "max_tokens": config.MAX_TOKENS,
                        "messages":   [{"role": "user", "content": content}],
                    },
                }
            ]
        )
        batch_id = batch.id

        # Write initial log now that we have a batch_id
        write_log(batch_id, f"Batch submitted | files: {len(uploaded_files)}")
        if fallback_files:
            write_log(batch_id, f"PDF fallback used for: {', '.join(fallback_files)}")
        if extraction_notes:
            for note in extraction_notes:
                write_log(batch_id, f"Note: {note}")

        return {
            "success":          True,
            "batch_id":         batch_id,
            "fallback_files":   fallback_files,
            "extraction_notes": extraction_notes,
            "error":            None,
        }

    except Exception:
        return {
            "success":          False,
            "batch_id":         None,
            "fallback_files":   [],
            "extraction_notes": [],
            "error":            traceback.format_exc(),
        }


# ── Poll ──────────────────────────────────────────────────────────────────────

def poll_until_done(batch_id: str, file_count: int):
    """
    Background daemon thread.
    Polls batch status — writes ONLY to log/status files, never session_state.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    write_log(batch_id, f"Polling started | interval: {config.POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            batch  = client.messages.batches.retrieve(batch_id)
            counts = batch.request_counts

            write_log(
                batch_id,
                f"Status: {batch.processing_status} | "
                f"Processing: {counts.processing} | "
                f"Succeeded: {counts.succeeded} | "
                f"Errored: {counts.errored}"
            )

            if batch.processing_status == "ended":
                write_log(batch_id, "Batch ended — retrieving results...")
                result = retrieve_results(batch_id, file_count, client)
                write_status(batch_id, result)

                if result["success"]:
                    write_log(
                        batch_id,
                        f"Complete | {len(result.get('items', []))} items | "
                        f"Cost: ${result['cost']['total_cost_usd']:.4f} | "
                        f"Email: {'sent' if result.get('email_sent') else 'FAILED'}"
                    )
                else:
                    write_log(batch_id, f"FAILED: {result.get('error')}")
                return

        except Exception:
            write_log(batch_id, f"Poll error:\n{traceback.format_exc()}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


def start_polling_thread(batch_id: str, file_count: int) -> threading.Thread:
    t = threading.Thread(
        target=poll_until_done,
        args=(batch_id, file_count),
        daemon=True,
    )
    t.start()
    return t


# ── Retrieve results ──────────────────────────────────────────────────────────

def retrieve_results(batch_id: str, file_count: int, client=None) -> dict:
    if client is None:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        total_input_tokens  = 0
        total_output_tokens = 0
        all_items           = []
        errors              = []

        for result in client.messages.batches.results(batch_id):
            if result.result.type == "succeeded":
                message = result.result.message
                total_input_tokens  += message.usage.input_tokens
                total_output_tokens += message.usage.output_tokens

                raw_text    = message.content[0].text
                stop_reason = message.stop_reason

                if stop_reason == "max_tokens":
                    err = (
                        f"Output truncated for {result.custom_id} — Claude hit the "
                        f"max_tokens limit ({config.MAX_TOKENS}). "
                        f"Try submitting fewer files per batch."
                    )
                    errors.append(err)
                    write_log(batch_id, f"WARNING: {err}")
                    continue

                try:
                    items = parse_json_response(raw_text)
                    all_items.extend(items)
                    write_log(batch_id, f"Parsed {len(items)} items from {result.custom_id}")
                except ValueError as e:
                    err = f"Parse error {result.custom_id}: {e}"
                    errors.append(err)
                    write_log(batch_id, f"WARNING: {err}")

            elif result.result.type == "errored":
                err = f"Request {result.custom_id} errored: {result.result.error.type}"
                errors.append(err)
                write_log(batch_id, f"ERROR: {err}")

        if not all_items and errors:
            return {
                "success": False, "items": [], "cost": None,
                "realtime_cost": None, "dup_warnings": [],
                "email_sent": False, "email_error": None,
                "error": "\n".join(errors),
            }

        # Deduplicate by invoice number
        all_items, dup_warnings = deduplicate_items(all_items)
        if dup_warnings:
            for w in dup_warnings:
                write_log(batch_id, f"DUP WARNING: {w}")

        # Re-number sr_no
        for idx, item in enumerate(all_items, 1):
            item["sr_no"] = idx

        # Cost
        batch_cost    = calculate_cost(total_input_tokens, total_output_tokens)
        realtime_cost = {
            "total_cost_usd":  round(batch_cost["total_cost_usd"]  * 2, 6),
            "input_cost_usd":  round(batch_cost["input_cost_usd"]  * 2, 6),
            "output_cost_usd": round(batch_cost["output_cost_usd"] * 2, 6),
            "input_tokens":    total_input_tokens,
            "output_tokens":   total_output_tokens,
        }

        write_log(
            batch_id,
            f"Cost: ${batch_cost['total_cost_usd']:.4f} batch | "
            f"${realtime_cost['total_cost_usd']:.4f} real-time | "
            f"Saved: ${realtime_cost['total_cost_usd'] - batch_cost['total_cost_usd']:.4f}"
        )

        # Create Excel + send email
        excel_bytes     = create_excel(all_items, dup_warnings or None)
        email_ok, email_result = send_email(
            excel_bytes   = excel_bytes,
            cost          = batch_cost,
            mode          = "Batch API",
            file_count    = file_count,
            item_count    = len(all_items),
            dup_warnings  = dup_warnings or None,
            realtime_cost = realtime_cost,
            batch_id      = batch_id,
        )
        write_log(
            batch_id,
            f"Email: {'sent to ' + config.RECIPIENT_EMAIL if email_ok else 'FAILED — ' + str(email_result)}"
        )

        return {
            "success":       True,
            "items":         all_items,
            "cost":          batch_cost,
            "realtime_cost": realtime_cost,
            "dup_warnings":  dup_warnings,
            "email_sent":    email_ok,
            "email_error":   None if email_ok else email_result,
            "error":         "\n".join(errors) if errors else None,
        }

    except Exception:
        err = traceback.format_exc()
        write_log(batch_id, f"retrieve_results FATAL:\n{err}")
        return {
            "success": False, "items": [], "cost": None,
            "realtime_cost": None, "dup_warnings": [],
            "email_sent": False, "email_error": None, "error": err,
        }
