# ─────────────────────────────────────────────
#  batch_processor.py  –  Batch API processing
#
#  Thread safety:
#    Background thread NEVER touches st.session_state.
#    All communication to UI is via files in batch_logs/, keyed throughout by
#    job_id (= credit_job_id, generated before any Anthropic batch exists and
#    constant for the whole job's life):
#      <job_id>.log         → append-only human-readable log
#      <job_id>.status      → JSON written once all batch jobs end; app.py polls this
#      submit_<job_id>.status → JSON written once submission itself finishes; app.py polls this
#
#  Submission runs in a background thread (start_submission_thread) rather
#  than blocking the main Streamlit script, for the same reason polling does:
#  it does real CPU-bound local work (rendering scanned pages, encoding
#  images) that can take a while on CPU-constrained hosts. Running it
#  synchronously ties up Streamlit's single worker long enough that the
#  worker stops responding to the frontend's own keep-alive requests, so the
#  browser shows a "Connection error" — regardless of whether submission
#  itself would have succeeded.
#
#  A file with many fallback-image pages is further split across MULTIPLE
#  separate Batch API jobs (see build_file_content_chunks /
#  MAX_FALLBACK_PAGES_PER_REQUEST) rather than one job with everything
#  bundled in. Per-page in-process cooperative yields alone weren't reliable
#  enough on very CPU-constrained hosts (confirmed: a 15-page real annotated
#  file still intermittently starved health-checks with those yields alone)
#  — each chunk's own network call is a genuine I/O wait, which is a more
#  reliable way to give the health-check thread real breathing room between
#  bursts of local CPU-bound work than an in-process sleep. This means one
#  file can now produce several Anthropic batch_ids; poll_until_done and
#  retrieve_results operate on a list of them and merge results together.
# ─────────────────────────────────────────────

import json
import re
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import anthropic

import config
from db import finalize_credit_reservation, refund_credit_reservation
from utils import (
    build_captured_pages_content,
    build_file_content_chunks,
    calculate_cost,
    create_excel,
    create_tally_ledger_masters_xml,
    create_tally_xml,
    deduplicate_items,
    parse_json_response,
    send_email,
)

# ── Log directory ─────────────────────────────────────────────────────────────
LOG_DIR = Path("batch_logs")
LOG_DIR.mkdir(exist_ok=True)

# Matches the page-composition suffix submit_batch() encodes into every
# custom_id (e.g. "..._t3_i5" or "..._t12_i0_wf") — see submit_batch for why
# it's embedded there rather than in a separately persisted file.
CUSTOM_ID_ROUTE_RE = re.compile(r"_t(\d+)_i(\d+)(_wf)?$")


# ── Log / status file helpers ─────────────────────────────────────────────────

def _log_path(job_id: str)    -> Path: return LOG_DIR / f"{job_id}.log"
def _status_path(job_id: str) -> Path: return LOG_DIR / f"{job_id}.status"


def write_log(job_id: str, msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"[{timestamp}] {msg}\n"
    print(line, end="")
    with open(_log_path(job_id), "a", encoding="utf-8") as f:
        f.write(line)


def read_logs(job_id: str) -> list:
    path = _log_path(job_id)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f.readlines()]


def _status_safe_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def write_status(job_id: str, result: dict):
    safe = {
        k: _status_safe_value(v)
        for k, v in result.items()
        if k != "excel_bytes"
    }
    with open(_status_path(job_id), "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2, default=str)


def read_status(job_id: str) -> dict:
    path = _status_path(job_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_batch_files(job_id: str):
    for path in [_log_path(job_id), _status_path(job_id)]:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


# ── Submission status file helpers ─────────────────────────────────────────────
#
#  Keyed by credit_job_id (generated before submission starts, unlike
#  batch_id which only exists once Anthropic has actually created the
#  batch) — this is what lets app.py poll for "has submission itself
#  finished" the same way it already polls for "has the batch finished".

def _submit_status_path(job_id: str) -> Path: return LOG_DIR / f"submit_{job_id}.status"


def write_submit_status(job_id: str, result: dict):
    with open(_submit_status_path(job_id), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)


def read_submit_status(job_id: str) -> dict:
    path = _submit_status_path(job_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_submit_status(job_id: str):
    path = _submit_status_path(job_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


# ── "Ended" status file helpers ─────────────────────────────────────────────────
#
#  Separate from the full status file above: this signals only "Anthropic has
#  finished processing these batch_ids" — no items, no Tally XML, nothing
#  large — so a scan session with many batches can wait for all of them to
#  reach this point without holding each one's actual results in memory.
#  Only the session-level finalize step (see finalize_scan_session) retrieves
#  and combines the real results, once, for the whole session at once.

def _ended_status_path(job_id: str) -> Path: return LOG_DIR / f"ended_{job_id}.status"


def write_ended_status(job_id: str, result: dict):
    with open(_ended_status_path(job_id), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)


def read_ended_status(job_id: str) -> dict:
    path = _ended_status_path(job_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup_ended_status(job_id: str):
    path = _ended_status_path(job_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


# ── Submit ────────────────────────────────────────────────────────────────────

def submit_batch(sources: list, user_email: str = None, job_id: str = None) -> dict:
    """
    Submits each source's content as one or more separate Batch API jobs —
    one job per chunk (see build_file_content_chunks / build_captured_pages_content
    and MAX_FALLBACK_PAGES_PER_REQUEST), rather than bundling everything into
    a single job with multiple requests. Each chunk's
    client.beta.messages.batches.create() call is a genuine network I/O
    wait, giving real breathing room between bursts of local CPU-bound work
    (page rendering, image encoding) on CPU-constrained hosts — more
    reliable than an in-process sleep between chunks.

    sources: list where each item is either:
      - a Streamlit UploadedFile (an uploaded PDF) — built via
        build_file_content_chunks(), or
      - a dict {"images": [bytes, ...], "name": "..."} (pages captured via
        st.camera_input(), not yet assembled into a PDF) — built via
        build_captured_pages_content().
    Submission logic below doesn't care which — it only consumes the
    common {"chunks", "page_count", "fallback_pages", "notes"} shape both
    builders return.

    job_id: if given, logs progress as submission happens (each chunk as
    it's built and submitted), not only after the fact.

    Returns:
        dict: success, batch_ids (list), fallback_files, extraction_notes, error, user_email, total_pages
    """
    client    = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    batch_ids = []

    def log(msg):
        if job_id:
            write_log(job_id, msg)

    try:
        ts               = datetime.now().strftime('%Y%m%d_%H%M%S')
        fallback_files   = []
        extraction_notes = []
        total_pages      = 0
        req_idx          = 0

        for source in sources:
            if isinstance(source, dict) and "images" in source:
                name  = source.get("name") or "Scanned pages"
                built = build_captured_pages_content(source["images"], name=name, log_fn=log)
            else:
                name  = source.name
                built = build_file_content_chunks(source, log_fn=log)

            total_pages += built["page_count"]
            if built["fallback_pages"] > 0:
                fallback_files.append(name)
            if built["notes"]:
                extraction_notes.extend(built["notes"])

            n_chunks = len(built["chunks"])
            for chunk_idx, chunk_content in enumerate(built["chunks"]):
                req_idx += 1
                content = chunk_content + [{"type": "text", "text": config.EXTRACTION_PROMPT}]

                # Page-route composition for this one request, encoded straight
                # into custom_id rather than a separately persisted file — this
                # is the only place that survives to retrieval time regardless
                # of Render restarts (batch_logs/ is ephemeral, but Anthropic
                # echoes custom_id back verbatim on every result). Only chunk 0
                # of a source carries its text block (native-extracted + local
                # OCR pages, combined — see build_file_content_chunks), so text
                # pages are attributed there; every chunk carries only its own
                # share of image-fallback pages.
                is_whole_file_fallback = any(b.get("type") == "document" for b in chunk_content)
                image_pages_here = sum(1 for b in chunk_content if b.get("type") == "image")
                if is_whole_file_fallback:
                    # The whole file is sent as one raw PDF block, not per-page
                    # text/images — "text_pages_here" here just carries the
                    # page count for cost-bucketing purposes, tagged _wf below
                    # so retrieval doesn't confuse it with the OCR/native-text route.
                    text_pages_here = built["page_count"]
                elif chunk_idx == 0:
                    text_pages_here = max(
                        0,
                        built["page_count"] - built.get("skipped_pages", 0) - built["fallback_pages"],
                    )
                else:
                    text_pages_here = 0
                route_suffix = "_wf" if is_whole_file_fallback else ""

                # The output-300k beta raises the per-request max_tokens cap
                # on the Batch API from a model's standard cap up to 300k —
                # but only for models Anthropic has confirmed support it (see
                # config.OUTPUT_300K_BETA_MODELS). Sending an unsupported
                # beta isn't something to assume is safely ignored, so it's
                # only included when the configured model is on that list.
                # Retrieval doesn't need the beta header either way, only
                # submission does.
                use_beta = config.MODEL in config.OUTPUT_300K_BETA_MODELS
                max_tokens = config.BATCH_MAX_TOKENS
                if not use_beta:
                    # Without the beta, exceeding a model's real standard cap
                    # gets the whole request rejected by the API outright
                    # (not silently capped) — 64k is the lowest standard cap
                    # among current models, so it's a safe ceiling for any
                    # model not on the confirmed-beta list.
                    max_tokens = min(max_tokens, 64_000)

                create_kwargs = {
                    "requests": [{
                        "custom_id": f"invoice_run_{ts}_{req_idx}_t{text_pages_here}_i{image_pages_here}{route_suffix}",
                        "params": {
                            "model":      config.MODEL,
                            "max_tokens": max_tokens,
                            "messages":   [{"role": "user", "content": content}],
                        },
                    }],
                }
                if use_beta:
                    create_kwargs["betas"] = ["output-300k-2026-03-24"]

                batch = client.beta.messages.batches.create(**create_kwargs)
                batch_ids.append(batch.id)
                log(f"{name} chunk {chunk_idx + 1}/{n_chunks} → submitted as {batch.id}")

        log(f"All chunks submitted | sources: {len(sources)} | batch jobs: {len(batch_ids)}")
        if fallback_files:
            log(f"Image fallback used for at least one page in: {', '.join(fallback_files)}")
        if extraction_notes:
            for note in extraction_notes:
                log(f"Note: {note}")
        log(f"User: {user_email or 'unknown'}")

        return {
            "success":          True,
            "batch_ids":        batch_ids,
            "fallback_files":   fallback_files,
            "extraction_notes": extraction_notes,
            "error":            None,
            "user_email":       user_email,
            "total_pages":      total_pages,
        }

    except Exception:
        # Any batch_ids already submitted before the failure are left as-is
        # (not polled/retrieved) — they'll simply expire per Anthropic's
        # retention policy. The credit reservation gets refunded regardless.
        return {
            "success":          False,
            "batch_ids":        batch_ids,
            "fallback_files":   [],
            "extraction_notes": [],
            "error":            traceback.format_exc(),
        }


def _submit_batch_worker(job_id: str, sources: list, user_email: str = None):
    """Background thread target — runs submit_batch() and writes the result
    to a submit_<job_id>.status file for app.py to poll."""
    result = submit_batch(sources, user_email=user_email, job_id=job_id)
    write_submit_status(job_id, result)


def start_submission_thread(job_id: str, sources: list, user_email: str = None) -> threading.Thread:
    """
    Runs submit_batch() in a background thread instead of the main script.
    See the module docstring for why this matters — submission does the same
    kind of CPU-bound local work (page rendering, image encoding) that
    poll_until_done() already avoids running synchronously.

    sources: see submit_batch() — uploaded PDF files, or captured-page dicts
    ({"images": [...], "name": "..."}), or a mix of both.
    """
    t = threading.Thread(
        target=_submit_batch_worker,
        args=(job_id, sources, user_email),
        daemon=True,
    )
    t.start()
    return t


# ── Poll ──────────────────────────────────────────────────────────────────────

def poll_until_done(
    job_id: str,
    batch_ids: list,
    file_count: int,
    user_email: str = None,
    total_pages: int = None,
    credit_job_id: str = None,
    upload_dup_warnings: list = None,
):
    """
    Background daemon thread.
    Polls every batch job in batch_ids (a file with many fallback pages can
    produce several — see submit_batch) until all have ended, then retrieves
    and merges their results. Writes ONLY to log/status files, never
    session_state. Keyed throughout by job_id (credit_job_id), not by any
    single Anthropic batch_id — job_id exists before submission even starts
    and stays constant regardless of how many batch jobs this file needed.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    write_log(job_id, f"Polling started for {len(batch_ids)} batch job(s) | interval: {config.POLL_INTERVAL_SECONDS}s")

    pending = set(batch_ids)
    while pending:
        for bid in list(pending):
            try:
                batch  = client.messages.batches.retrieve(bid)
                counts = batch.request_counts
                write_log(
                    job_id,
                    f"{bid}: {batch.processing_status} | "
                    f"Processing: {counts.processing} | "
                    f"Succeeded: {counts.succeeded} | "
                    f"Errored: {counts.errored}"
                )
                if batch.processing_status == "ended":
                    pending.discard(bid)
            except Exception:
                write_log(job_id, f"Poll error for {bid}:\n{traceback.format_exc()}")

        if pending:
            time.sleep(config.POLL_INTERVAL_SECONDS)

    write_log(job_id, "All batch jobs ended — retrieving results...")
    result = retrieve_results(
        job_id,
        batch_ids,
        file_count,
        client,
        user_email=user_email,
        total_pages=total_pages,
        upload_dup_warnings=upload_dup_warnings,
    )

    if credit_job_id:
        if result["success"]:
            credit_result = finalize_credit_reservation(credit_job_id)
            result["credit_finalized"] = credit_result["success"]
            result["credit_error"] = None if credit_result["success"] else credit_result.get("error")
        else:
            credit_result = refund_credit_reservation(
                credit_job_id,
                reason=result.get("error") or "Batch processing failed",
            )
            result["credit_refunded"] = credit_result["success"]
            result["credit_error"] = None if credit_result["success"] else credit_result.get("error")

    write_status(job_id, result)

    if result["success"]:
        write_log(
            job_id,
            f"Complete | {len(result.get('items', []))} items | "
            f"Cost: ${result['cost']['total_cost_usd']:.4f} | "
            f"Email: {'sent' if result.get('email_sent') else 'FAILED'}"
        )
    else:
        write_log(job_id, f"FAILED: {result.get('error')}")


def start_polling_thread(
    job_id: str,
    batch_ids: list,
    file_count: int,
    user_email: str = None,
    total_pages: int = None,
    credit_job_id: str = None,
    upload_dup_warnings: list = None,
) -> threading.Thread:
    t = threading.Thread(
        target=poll_until_done,
        args=(job_id, batch_ids, file_count, user_email, total_pages, credit_job_id, upload_dup_warnings),
        daemon=True,
    )
    t.start()
    return t


# ── Lightweight "wait for Anthropic to finish" (no retrieval) ──────────────────
#
#  Used by the scan-capture flow: a scan session can hold several batches
#  before the user is done scanning, and emailing per-batch would mean many
#  emails for one sitting. Each batch just needs to know when Anthropic has
#  finished with it — not its actual results — so multiple batches can wait
#  in parallel without holding any real data. The session-level finalize step
#  (finalize_scan_session) does the one actual retrieval, combining every
#  batch in the session into a single Excel/email, once all of them reach
#  this point.

def poll_batches_until_ended(job_id: str, batch_ids: list):
    """
    Background daemon thread. Polls batch_ids until every one has ended,
    then writes a minimal ended_<job_id>.status (no items/cost/attachments —
    just enough for the caller to know retrieval can happen). Does not call
    retrieve_results and does not touch credits — the session-level
    finalize step owns both of those once the whole session is ready.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    write_log(job_id, f"Waiting for {len(batch_ids)} batch job(s) to finish | interval: {config.POLL_INTERVAL_SECONDS}s")

    pending = set(batch_ids)
    while pending:
        for bid in list(pending):
            try:
                batch = client.messages.batches.retrieve(bid)
                if batch.processing_status == "ended":
                    pending.discard(bid)
                    write_log(job_id, f"{bid}: ended")
            except Exception:
                write_log(job_id, f"Poll error for {bid}:\n{traceback.format_exc()}")

        if pending:
            time.sleep(config.POLL_INTERVAL_SECONDS)

    write_ended_status(job_id, {"success": True, "batch_ids": batch_ids})


def start_ended_wait_thread(job_id: str, batch_ids: list) -> threading.Thread:
    t = threading.Thread(
        target=poll_batches_until_ended,
        args=(job_id, batch_ids),
        daemon=True,
    )
    t.start()
    return t


# ── Scan session finalize (one retrieval + one email for the whole session) ────

def finalize_scan_session(
    session_job_id: str,
    batch_ids: list,
    credit_job_ids: list,
    batch_count: int,
    user_email: str = None,
    total_pages: int = None,
):
    """
    Background daemon thread. Runs once, when every batch in a scan session
    has reached "ended". Retrieves and merges results across every batch_id
    from every batch in the session in a single retrieve_results() call —
    the exact same merge logic already used across a single batch's chunks,
    just applied one level up — producing one Excel/Tally XML/email for the
    whole session instead of one per batch.

    credit_job_ids: every scan batch's credit_job_id in this session. All get
    finalized together on success, or all refunded together on failure —
    they were reserved independently but the session is retrieved as a unit.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    write_log(session_job_id, f"Finalizing scan session | batches: {batch_count} | batch jobs: {len(batch_ids)}")

    result = retrieve_results(
        session_job_id,
        batch_ids,
        batch_count,
        client,
        user_email=user_email,
        total_pages=total_pages,
        upload_dup_warnings=None,
    )

    for credit_job_id in credit_job_ids:
        if result["success"]:
            finalize_credit_reservation(credit_job_id)
        else:
            refund_credit_reservation(
                credit_job_id,
                reason=result.get("error") or "Scan session processing failed",
            )
    result["credit_job_ids"] = credit_job_ids

    write_status(session_job_id, result)

    if result["success"]:
        write_log(
            session_job_id,
            f"Session complete | {len(result.get('items', []))} items | "
            f"Cost: ${result['cost']['total_cost_usd']:.4f} | "
            f"Email: {'sent' if result.get('email_sent') else 'FAILED'}"
        )
    else:
        write_log(session_job_id, f"Session FAILED: {result.get('error')}")


def start_session_finalize_thread(
    session_job_id: str,
    batch_ids: list,
    credit_job_ids: list,
    batch_count: int,
    user_email: str = None,
    total_pages: int = None,
) -> threading.Thread:
    t = threading.Thread(
        target=finalize_scan_session,
        args=(session_job_id, batch_ids, credit_job_ids, batch_count, user_email, total_pages),
        daemon=True,
    )
    t.start()
    return t


# ── Retrieve results ──────────────────────────────────────────────────────────

def retrieve_results(
    job_id: str,
    batch_ids: list,
    file_count: int,
    client=None,
    user_email: str = None,
    total_pages: int = None,
    upload_dup_warnings: list = None,
) -> dict:
    """
    Retrieves and merges results across every batch job in batch_ids (a file
    with many fallback pages can produce several — see submit_batch). Each
    batch's results are just custom_id-keyed requests either way, so merging
    across batch jobs is no different from merging across the requests
    within a single one — this function already looped over multiple
    requests before chunking existed, just now across more than one
    underlying batch_id too.
    """
    if client is None:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        total_input_tokens  = 0
        total_output_tokens = 0
        all_items           = []
        errors              = []

        # Per-route cost breakdown. "text" = pages read via native PDF
        # extraction and/or local OCR (same cost either way — both become a
        # text block, not image tokens); "image" = pages sent as images
        # (handwriting/stamps, OCR unavailable, or OCR too sparse); "mixed" =
        # a request that carried both in one call (only chunk 0 of a source
        # can, when it has both a text block and its own share of fallback
        # images) — its cost can't be split further between the two without
        # guessing, so it's reported as its own bucket rather than forcing an
        # inaccurate per-page split; "whole_file_pdf" = the rare fallback
        # where extraction failed entirely and the raw PDF was sent as-is.
        route_totals = {
            "text":           {"pages": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            "image":          {"pages": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            "mixed":          {"text_pages": 0, "image_pages": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            "whole_file_pdf": {"pages": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        }

        for batch_id in batch_ids:
            for result in client.messages.batches.results(batch_id):
                if result.result.type == "succeeded":
                    message = result.result.message
                    total_input_tokens  += message.usage.input_tokens
                    total_output_tokens += message.usage.output_tokens

                    request_cost = calculate_cost(message.usage.input_tokens, message.usage.output_tokens)
                    route_match  = CUSTOM_ID_ROUTE_RE.search(result.custom_id)
                    if route_match:
                        text_pages, image_pages, is_wf = (
                            int(route_match.group(1)), int(route_match.group(2)), bool(route_match.group(3))
                        )
                        if is_wf:
                            bucket, desc = "whole_file_pdf", f"{text_pages} page(s), whole-file PDF fallback"
                        elif text_pages > 0 and image_pages > 0:
                            bucket, desc = "mixed", f"{text_pages} text + {image_pages} image page(s), mixed"
                        elif image_pages > 0:
                            bucket, desc = "image", f"{image_pages} image page(s)"
                        else:
                            bucket, desc = "text", f"{text_pages} text page(s)"

                        rt = route_totals[bucket]
                        rt["requests"]      += 1
                        rt["input_tokens"]  += message.usage.input_tokens
                        rt["output_tokens"] += message.usage.output_tokens
                        rt["cost_usd"]      += request_cost["total_cost_usd"]
                        if bucket == "mixed":
                            rt["text_pages"]  += text_pages
                            rt["image_pages"] += image_pages
                        else:
                            rt["pages"] += text_pages if bucket in ("text", "whole_file_pdf") else image_pages

                        write_log(
                            job_id,
                            f"Cost {result.custom_id}: {desc} | "
                            f"{message.usage.input_tokens:,} in / {message.usage.output_tokens:,} out tok | "
                            f"${request_cost['total_cost_usd']:.4f} ({config.MODEL})"
                        )

                    raw_text    = message.content[0].text
                    stop_reason = message.stop_reason

                    if stop_reason == "max_tokens":
                        beta_ceiling = config.MODEL in config.OUTPUT_300K_BETA_MODELS
                        err = (
                            f"Output truncated for {result.custom_id} — Claude hit the "
                            f"max_tokens limit ({config.BATCH_MAX_TOKENS}) for this chunk. "
                            + (
                                f"Raise BATCH_MAX_TOKENS (up to 300000 on {config.MODEL})"
                                if beta_ceiling else
                                f"Raise BATCH_MAX_TOKENS (up to 64000 — {config.MODEL} isn't on "
                                f"Anthropic's supported list for the higher 300000 cap)"
                            )
                            + f", or lower MAX_FALLBACK_PAGES_PER_REQUEST if this chunk had an "
                            f"unusually large number of line items."
                        )
                        errors.append(err)
                        write_log(job_id, f"WARNING: {err}")
                        continue

                    try:
                        items = parse_json_response(raw_text, token_limit=config.BATCH_MAX_TOKENS)
                        all_items.extend(items)
                        write_log(job_id, f"Parsed {len(items)} items from {result.custom_id}")
                    except ValueError as e:
                        err = f"Parse error {result.custom_id}: {e}"
                        errors.append(err)
                        write_log(job_id, f"WARNING: {err}")

                elif result.result.type == "errored":
                    err = f"Request {result.custom_id} errored: {result.result.error.type}"
                    errors.append(err)
                    write_log(job_id, f"ERROR: {err}")

        if not all_items:
            return {
                "success": False, "items": [], "cost": None,
                "realtime_cost": None, "dup_warnings": [],
                "email_sent": False, "email_error": None,
                "error": "\n".join(errors) if errors else "No invoice line items were extracted.",
                "total_pages": total_pages or file_count,
            }

        # Deduplicate by invoice number — also the safety net for the rare
        # case where the same page's items ever appeared in more than one
        # chunk (shouldn't happen: build_file_content_chunks puts each page
        # in exactly one chunk, but exact-duplicate line items are skipped
        # either way if it ever does).
        all_items, dup_warnings = deduplicate_items(all_items)
        if dup_warnings:
            for w in dup_warnings:
                write_log(job_id, f"DUP WARNING: {w}")

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
            job_id,
            f"Cost: ${batch_cost['total_cost_usd']:.4f} batch | "
            f"${realtime_cost['total_cost_usd']:.4f} real-time | "
            f"Saved: ${realtime_cost['total_cost_usd'] - batch_cost['total_cost_usd']:.4f}"
        )

        # Route breakdown — avg $/page is only meaningful for the pure
        # buckets (text-only or image-only requests), where a request's
        # whole cost maps cleanly to one route; "mixed" requests can't be
        # split further per page, so only their totals are reported.
        breakdown_parts = []
        for bucket_name, label in (("text", "Text/OCR"), ("image", "Image"), ("whole_file_pdf", "Whole-file PDF")):
            rt = route_totals[bucket_name]
            if rt["requests"] == 0:
                continue
            avg = f" (${rt['cost_usd'] / rt['pages']:.4f}/page)" if rt["pages"] else ""
            breakdown_parts.append(
                f"{label}: {rt['pages']} page(s), {rt['requests']} req, ${rt['cost_usd']:.4f}{avg}"
            )
        mixed = route_totals["mixed"]
        if mixed["requests"] > 0:
            breakdown_parts.append(
                f"Mixed: {mixed['text_pages']} text + {mixed['image_pages']} image page(s), "
                f"{mixed['requests']} req, ${mixed['cost_usd']:.4f}"
            )
        if breakdown_parts:
            write_log(job_id, f"Cost by route ({config.MODEL}) | " + " | ".join(breakdown_parts))

        # Create Excel + Tally XMLs — ledger masters must be imported before
        # the vouchers that reference them (see create_tally_ledger_masters_xml).
        excel_bytes      = create_excel(all_items, dup_warnings or None)
        tally_erp9_masters_bytes  = create_tally_ledger_masters_xml(all_items, "erp9")
        tally_prime_masters_bytes = create_tally_ledger_masters_xml(all_items, "prime")
        tally_erp9_bytes = create_tally_xml(all_items, "erp9")
        tally_prime_bytes = create_tally_xml(all_items, "prime")
        write_log(job_id, "Excel and Tally XML files created")

        email_ok, email_result = send_email(
            excel_bytes       = excel_bytes,
            cost              = batch_cost,
            mode              = "Batch API",
            file_count        = file_count,
            item_count        = len(all_items),
            user_email        = user_email,
            dup_warnings      = dup_warnings or None,
            upload_dup_warnings = upload_dup_warnings or None,
            realtime_cost     = realtime_cost,
            batch_id          = ", ".join(batch_ids),
            tally_erp9_bytes  = tally_erp9_bytes,
            tally_prime_bytes = tally_prime_bytes,
            tally_erp9_masters_bytes  = tally_erp9_masters_bytes,
            tally_prime_masters_bytes = tally_prime_masters_bytes,
        )
        write_log(
            job_id,
            f"Email: {'email sent ' if email_ok else 'FAILED — ' + str(email_result)}"
        )

        return {
            "success":            True,
            "items":              all_items,
            "cost":               batch_cost,
            "realtime_cost":      realtime_cost,
            "dup_warnings":       dup_warnings,
            "upload_dup_warnings": upload_dup_warnings or [],
            "email_sent":         email_ok,
            "email_error":        None if email_ok else email_result,
            "error":              "\n".join(errors) if errors else None,
            "tally_erp9_bytes":   tally_erp9_bytes,
            "tally_prime_bytes":  tally_prime_bytes,
            "tally_erp9_masters_bytes":  tally_erp9_masters_bytes,
            "tally_prime_masters_bytes": tally_prime_masters_bytes,
            "total_pages":        total_pages or file_count,
        }

    except Exception:
        err = traceback.format_exc()
        write_log(job_id, f"retrieve_results FATAL:\n{err}")
        return {
            "success": False, "items": [], "cost": None,
            "realtime_cost": None, "dup_warnings": [],
            "email_sent": False, "email_error": None, "error": err,
            "total_pages": total_pages or file_count,
        }
