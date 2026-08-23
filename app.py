# ─────────────────────────────────────────────
#  app.py  –  Streamlit UI for Invoice Processor MVP
#  Run with:  streamlit run app.py
# ─────────────────────────────────────────────

import json
import time
import uuid
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

# ── Load .env file for local development ──────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
from auth import request_otp, validate_otp
from batch_processor import (
    cleanup_batch_files,
    cleanup_ended_status,
    cleanup_submit_status,
    read_ended_status,
    read_status,
    read_submit_status,
    start_ended_wait_thread,
    start_polling_thread,
    start_session_finalize_thread,
    start_submission_thread,
)
from db import (
    create_session,
    finalize_credit_reservation,
    get_session_user,
    get_user_credits,
    refund_credit_reservation,
    reserve_credits,
    revoke_session,
)
from realtime_processor import process_realtime
from utils import count_uploaded_pdf_pages, create_excel, detect_duplicate_uploads, process_captured_page, send_email


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title = "Invoice Processor",
    page_icon  = "🧾",
    layout     = "centered",
)

st.markdown(
    """
    <style>
    header[data-testid="stHeader"],
    div[data-testid="stToolbar"],
    div[data-testid="stDecoration"],
    div[data-testid="stStatusWidget"],
    #MainMenu {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Session state initialisation ─────────────────────────────────────────────

auth_defaults = {
    "logged_in":    False,
    "user_email":   None,
    "user_credits": 0,
    "otp_sent":     False,
    "otp_email":    "",
    "otp_request_pending": False,
    "otp_verify_pending": False,
    "process_requested": False,
}

batch_defaults = {
    "batch_ids":                   None,
    "batch_submitted":             False,
    "submission_started":          False,
    "file_count":                  0,
    "batch_total_pages":           0,
    "credit_job_id":               None,
    "pending_upload_dup_warnings": None,
    "processing":                  False,
}

# Scan-capture flow is entirely independent of the upload flow above — its
# own session-state, its own job list, so scanning and uploading can be used
# together without interfering with each other.
scan_defaults = {
    "scan_buffer":              [],     # captured pages not yet finalized into a batch — each a dict from process_captured_page()
    "scan_capture_count":       0,      # bumped to force a fresh camera_input widget after each capture/finalize
    "scan_local_build_active":  False,  # serialization gate — only one batch's local build+submit runs at a time
    "scan_jobs":                [],     # every scan batch created this session, in any state
    "scan_session_id":          None,   # set on the first batch of a new scanning session
    "scan_session_credit_ids":  [],     # every batch's credit_job_id in the current session
    "scan_session_finalizing":  False,  # True once the one combined retrieve+email has been kicked off
    "scan_finalize_requested":  False,  # set by the "Done Scanning" button
    "scan_last_activity":       None,   # time.time() of the last capture/batch — drives the idle timeout
}

for k, v in {**auth_defaults, **batch_defaults, **scan_defaults}.items():
    if k not in st.session_state:
        st.session_state[k] = v


SESSION_COOKIE_NAME = "invoice_processor_session"


def _legacy_query_session_token() -> str:
    token = st.query_params.get("session", "")
    if isinstance(token, list):
        token = token[0] if token else ""
    return str(token or "").strip()


def _cookie_session_token() -> str:
    return str(st.context.cookies.get(SESSION_COOKIE_NAME, "") or "").strip()


def _clear_legacy_query_session():
    if "session" in st.query_params:
        del st.query_params["session"]


def _set_cookie_session(token: str, reload_page: bool = False):
    if not token:
        return
    reload_js = "window.parent.location.reload();" if reload_page else ""
    components.html(
        f"""
        <script>
        document.cookie = {json.dumps(SESSION_COOKIE_NAME)} + "=" + encodeURIComponent({json.dumps(token)}) + "; Max-Age=2592000; Path=/; SameSite=Lax";
        {reload_js}
        </script>
        """,
        height=0,
        width=0,
    )


def _clear_cookie_session(reload_page: bool = False):
    reload_js = "window.parent.location.reload();" if reload_page else ""
    components.html(
        f"""
        <script>
        document.cookie = {json.dumps(SESSION_COOKIE_NAME)} + "=; Max-Age=0; Path=/; SameSite=Lax";
        {reload_js}
        </script>
        """,
        height=0,
        width=0,
    )


def _restore_persisted_session():
    if st.session_state["logged_in"]:
        return
    _clear_legacy_query_session()
    token = _cookie_session_token()
    user = get_session_user(token) if token else None
    if not user:
        return
    st.session_state["logged_in"] = True
    st.session_state["user_email"] = user["email"]
    st.session_state["user_credits"] = user.get("credits", 0)


def _apply_credit_reservation(job_id: str, total_pages: int, mode: str):
    reservation = reserve_credits(user_email, total_pages, job_id=job_id, mode=mode)
    if reservation["success"]:
        st.session_state["user_credits"] = reservation["credits_after"]
        verb = "already reserved" if reservation.get("already_reserved") else "reserved"
        st.info(
            f"🪙 **{reservation['credits_reserved']} credit(s) {verb}** "
            f"({total_pages} page(s) to process). "
            f"Remaining available: **{reservation['credits_after']}**"
        )
    else:
        st.error(f"🚫 Could not reserve credits: {reservation.get('error')}")
    return reservation


def _finalize_credit_reservation(job_id: str):
    result = finalize_credit_reservation(job_id)
    if not result["success"]:
        st.warning(f"⚠️ Could not finalize credit reservation: {result.get('error')}")
    return result


def _refund_credit_reservation(job_id: str, reason: str):
    refund = refund_credit_reservation(job_id, reason=reason)
    if refund["success"]:
        st.session_state["user_credits"] = refund["credits_after"]
        st.info(
            f"↩️ **{refund['credits_refunded']} reserved credit(s) refunded**. "
            f"Available: **{refund['credits_after']}**"
        )
    else:
        st.warning(f"⚠️ Could not refund reserved credits: {refund.get('error')}")
    return refund


_restore_persisted_session()


def _request_otp_action():
    st.session_state["otp_request_pending"] = True


def _verify_otp_action():
    st.session_state["otp_verify_pending"] = True


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH GATE
# ══════════════════════════════════════════════════════════════════════════════

if not st.session_state["logged_in"]:

    st.title("🧾 Invoice Processor")
    st.caption("Please sign in to continue.")
    st.divider()

    if not st.session_state["otp_sent"]:
        # ── Step 1: Enter email ──
        st.subheader("Sign In")
        email_input = st.text_input(
            "Enter your registered email address",
            placeholder = "you@example.com",
            key         = "login_email_input",
        )

        st.button(
            "Send OTP",
            type="primary",
            use_container_width=True,
            disabled=st.session_state["otp_request_pending"],
            on_click=_request_otp_action,
        )

        if st.session_state["otp_request_pending"]:
            if not email_input or "@" not in email_input:
                st.session_state["otp_request_pending"] = False
                st.error("Please enter a valid email address.")
            else:
                with st.spinner("Preparing OTP..."):
                    result = request_otp(email_input.strip())

                st.session_state["otp_request_pending"] = False
                if result["success"]:
                    st.session_state["otp_sent"]  = True
                    st.session_state["otp_email"] = email_input.strip().lower()
                    st.rerun()
                else:
                    if result.get("blocked"):
                        st.error(f"🚫 {result['message']}")
                    else:
                        st.error(f"❌ {result['message']}")

    else:
        # ── Step 2: Enter OTP ──
        st.subheader("Enter OTP")
        st.info(
            f"An OTP has been sent to **{st.session_state['otp_email']}**. "
            f"Valid for {config.OTP_EXPIRY_MINUTES} minutes.",
            icon="📧",
        )

        otp_input = st.text_input(
            "Enter the 6-digit OTP",
            max_chars   = 6,
            placeholder = "123456",
            key         = "otp_input",
        )

        col1, col2 = st.columns(2)
        with col1:
            st.button(
                "Verify OTP",
                type="primary",
                use_container_width=True,
                disabled=st.session_state["otp_verify_pending"],
                on_click=_verify_otp_action,
            )

            if st.session_state["otp_verify_pending"]:
                if not otp_input or len(otp_input.strip()) != 6:
                    st.session_state["otp_verify_pending"] = False
                    st.error("Please enter the 6-digit OTP.")
                else:
                    with st.spinner("Verifying..."):
                        result = validate_otp(
                            st.session_state["otp_email"],
                            otp_input.strip(),
                        )
                    st.session_state["otp_verify_pending"] = False
                    if result["success"]:
                        user_email_for_session = st.session_state["otp_email"]
                        session = create_session(user_email_for_session)
                        if session["success"]:
                            _set_cookie_session(session["token"], reload_page=True)

                        st.session_state["logged_in"]    = True
                        st.session_state["user_email"]   = user_email_for_session
                        st.session_state["user_credits"] = result["credits"]
                        st.session_state["otp_sent"]     = False
                        st.session_state["otp_email"]    = ""
                        st.success("Login successful. Opening your session...")
                        st.stop()
                    else:
                        st.error(f"❌ {result['message']}")

        with col2:
            if st.button("← Use different email", use_container_width=True, disabled=st.session_state["otp_verify_pending"]):
                st.session_state["otp_sent"]  = False
                st.session_state["otp_email"] = ""
                st.session_state["otp_request_pending"] = False
                st.session_state["otp_verify_pending"] = False
                st.rerun()

    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN APP — only reached if logged in
# ══════════════════════════════════════════════════════════════════════════════

user_email   = st.session_state["user_email"]
user_credits = st.session_state["user_credits"]

# ── Header ────────────────────────────────────────────────────────────────────
col_title, col_user = st.columns([3, 1])
with col_title:
    st.title("🧾 Invoice Processor")
    st.caption("Extract structured data from invoice PDFs")
with col_user:
    st.markdown(
        f"<div style='text-align:right; padding-top:12px;'>👤 {user_email}</div>",
        unsafe_allow_html=True,
    )
    credits_color = "green" if user_credits > 10 else "orange" if user_credits > 0 else "red"
    st.markdown(
        f"<div style='text-align:right;'>"
        f"<span style='color:{credits_color}; font-weight:600;'>Credits: {user_credits}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.button("Sign out", use_container_width=True):
        revoke_session(_cookie_session_token())
        for k, v in {**auth_defaults, **batch_defaults, **scan_defaults}.items():
            st.session_state[k] = v
        _clear_cookie_session(reload_page=True)
        st.stop()

st.divider()

# ── Credits gate ──────────────────────────────────────────────────────────────
if user_credits <= 0:
    st.error(
        "🚫 You have no credits remaining. "
        "Please contact the admin to top up your account."
    )
    st.stop()

if user_credits <= 5:
    st.warning(f"⚠️ Low credits: **{user_credits}** remaining. Contact admin to top up soon.")


# ── File uploader ─────────────────────────────────────────────────────────────

uploaded_files = st.file_uploader(
    "Upload invoice PDFs",
    type                  = ["pdf"],
    accept_multiple_files = True,
    help                  = "1 credit = 1 PDF page processed.",
)

processing_files = uploaded_files or []
selected_total_pages = 0
page_count_error = False
insufficient_credits = False
duplicate_upload_warnings = []

if uploaded_files:
    st.success(
        f"{len(uploaded_files)} file(s) selected: "
        f"{', '.join(f.name for f in uploaded_files)}"
    )

    duplicate_summary = detect_duplicate_uploads(uploaded_files)
    processing_files = duplicate_summary["unique_files"]
    duplicate_upload_warnings = duplicate_summary["duplicates"]

    if duplicate_upload_warnings:
        with st.expander(
            f"⚠️ {len(duplicate_upload_warnings)} duplicate uploaded PDF(s) will be skipped",
            expanded=True,
        ):
            for dup in duplicate_upload_warnings:
                st.warning(
                    f"{dup['name']} duplicates {dup['duplicate_of']} "
                    f"(GSTIN: {dup['gstin']}, Invoice: {dup['invoice_no']})."
                )

    if duplicate_summary["unidentified"]:
        st.caption(
            "Duplicate precheck only skips files where both GSTIN and invoice number "
            "are readable locally. Ambiguous/scanned files will still be processed."
        )

    page_summary = count_uploaded_pdf_pages(processing_files)
    selected_total_pages = page_summary["total_pages"]

    latest_credits = get_user_credits(user_email)
    if latest_credits >= 0 and latest_credits != st.session_state["user_credits"]:
        st.session_state["user_credits"] = latest_credits
        user_credits = latest_credits

    if page_summary["success"]:
        st.info(
            f"📄 PDFs to process contain **{selected_total_pages} page(s)**. "
            f"This job requires **{selected_total_pages} credit(s)**. "
            f"Available: **{user_credits}**."
        )
        if selected_total_pages > user_credits:
            insufficient_credits = True
            st.error(
                f"🚫 Not enough credits to process these PDFs. "
                f"You need **{selected_total_pages}** credit(s) for the non-duplicate PDFs, but only have **{user_credits}**. "
                f"Please contact the admin to top up your account."
            )
    else:
        page_count_error = True
        st.error("Could not read page count for one or more PDFs. Please remove the invalid file and upload again.")
        with st.expander("Page count error details"):
            for err in page_summary["errors"]:
                st.code(err)


# ── Mode selection ────────────────────────────────────────────────────────────
# Temporarily disabled: for now all jobs use Batch API by default.
#
# st.subheader("Processing Mode")
# mode = st.radio(
#     label="Choose how to process your invoices:",
#     options=["⚡ Real-time API", "📦 Batch API (50% cheaper — results by email)"],
#     index=1,
# )
# is_batch = mode.startswith("📦")

is_batch = True
st.info(
    f"📧 Results will be emailed to **{user_email}** when complete.\n\n"
    f"⏱️ Status checked every **{config.POLL_INTERVAL_SECONDS // 60} minute(s)** in background.\n\n"
    f"✅ You can safely close this tab — the job continues running.",
    icon="ℹ️",
)

st.divider()


# ── Process button ────────────────────────────────────────────────────────────

def _request_processing():
    st.session_state["processing"] = True
    st.session_state["process_requested"] = True


btn_disabled = (
    not processing_files
    or page_count_error
    or insufficient_credits
    or st.session_state["batch_submitted"]
    or st.session_state["processing"]
)

st.button(
    label               = "🚀 Process Invoices",
    disabled            = btn_disabled,
    use_container_width = True,
    type                = "primary",
    on_click            = _request_processing,
)
process_requested = st.session_state.get("process_requested", False)

if not uploaded_files:
    st.caption("⬆️ Upload at least one PDF to enable processing.")
elif not processing_files:
    st.caption("All uploaded PDFs were identified as duplicates, so there is nothing new to process.")
elif insufficient_credits:
    st.caption("Add credits or remove PDFs until the required page count fits your balance.")
elif page_count_error:
    st.caption("Fix the unreadable PDF upload before processing.")


# ══════════════════════════════════════════════════════════════════════════════
#  SCAN PAGES — capture, auto-chunk, background-submit, session-combined email
# ══════════════════════════════════════════════════════════════════════════════
#
#  Independent of the upload flow above — its own session-state, its own job
#  list — so scanning and uploading can both be used without interfering.
#
#  Resource-aware serialization: capturing a photo is free (just bytes into
#  memory), so it's never blocked. But only ONE batch's local build+submit
#  phase (rendering/detecting/encoding pages, then the network call) runs at
#  a time — that's the CPU-bound part that starved this app's own
#  health-check handling earlier this session even with per-page cooperative
#  yields. Once a batch's local build is submitted, it drops into a cheap
#  wait for Anthropic to finish, which is safe to run in parallel across many
#  batches — so the gate only serializes the expensive part, not the whole job.
#
#  Batches don't retrieve or email their own results — they only wait for
#  Anthropic to finish ("ended"), so nothing large (items, Tally XML) is held
#  anywhere while a session is still in progress, however many batches it
#  has. Once every batch in the session has ended — session closed via the
#  "Done Scanning" button, or automatically after
#  SCAN_SESSION_IDLE_TIMEOUT_SECONDS of inactivity — one retrieve_results()
#  call combines every batch's results into a single Excel/Tally XML/email,
#  reusing the exact merge logic already used across one batch's own chunks,
#  just applied one level up.

def _scan_finalize_batch():
    """Reserves credits and queues the current scan_buffer as a new batch in the session."""
    images = [page["submit"] for page in st.session_state["scan_buffer"]]
    page_count = len(images)
    credit_job_id = f"scan_{uuid.uuid4().hex}"

    reservation = _apply_credit_reservation(credit_job_id, page_count, mode="scan")
    if not reservation["success"]:
        return  # error already shown; leave scan_buffer intact so the user can retry

    if st.session_state["scan_session_id"] is None:
        st.session_state["scan_session_id"] = f"scansession_{uuid.uuid4().hex}"

    st.session_state["scan_jobs"].append({
        "credit_job_id": credit_job_id,
        "page_count":    page_count,
        "images":        images,
        "state":         "queued",
        "batch_ids":     None,
        "error":         None,
        "created_at":    datetime.now().strftime("%H:%M:%S"),
    })
    st.session_state["scan_session_credit_ids"].append(credit_job_id)
    st.session_state["scan_buffer"] = []
    st.session_state["scan_capture_count"] += 1
    st.session_state["scan_last_activity"] = time.time()
    st.rerun()


def _scan_advance_queue():
    """Promotes the next queued scan batch to 'submitting' if the gate is free."""
    if st.session_state["scan_local_build_active"]:
        return
    for job in st.session_state["scan_jobs"]:
        if job["state"] == "queued":
            job["state"] = "submitting"
            st.session_state["scan_local_build_active"] = True
            start_submission_thread(
                job["credit_job_id"],
                [{"images": job["images"], "name": f"Scan batch {job['created_at']}"}],
                user_email=user_email,
            )
            job["images"] = None  # the background thread has its own reference now
            break  # promote at most one per rerun — the gate is exclusive


def _scan_poll_jobs():
    """
    Advances every in-flight scan job by one step (submitting -> awaiting
    Anthropic -> ended). Returns True if any batch still needs polling.
    """
    for job in st.session_state["scan_jobs"]:
        if job["state"] == "submitting":
            submit_status = read_submit_status(job["credit_job_id"])
            if submit_status is not None:
                cleanup_submit_status(job["credit_job_id"])
                st.session_state["scan_local_build_active"] = False
                if submit_status["success"]:
                    job["batch_ids"] = submit_status["batch_ids"]
                    job["state"] = "awaiting_anthropic"
                    start_ended_wait_thread(job["credit_job_id"], submit_status["batch_ids"])
                else:
                    _refund_credit_reservation(
                        job["credit_job_id"],
                        reason=submit_status.get("error") or "Scan batch submission failed",
                    )
                    job["state"] = "failed"
                    job["error"] = submit_status.get("error")

        elif job["state"] == "awaiting_anthropic":
            ended_status = read_ended_status(job["credit_job_id"])
            if ended_status is not None:
                cleanup_ended_status(job["credit_job_id"])
                job["state"] = "ended"

    return any(j["state"] in ("queued", "submitting", "awaiting_anthropic") for j in st.session_state["scan_jobs"])


def _scan_request_finalize():
    st.session_state["scan_finalize_requested"] = True


def _scan_reset_session():
    for k, v in scan_defaults.items():
        st.session_state[k] = v


st.subheader("📷 Scan Pages")
st.caption(
    f"Capture pages with your camera — every {config.SCAN_AUTO_SUBMIT_THRESHOLD} pages "
    f"auto-submits as a batch in the background, so you can keep scanning without waiting. "
    f"All batches from one sitting are combined into a single email when you're done."
)

scan_jobs_still_polling = _scan_poll_jobs()
_scan_advance_queue()

scan_has_batches = bool(st.session_state["scan_jobs"])
scan_all_ended = scan_has_batches and all(
    j["state"] in ("ended", "failed") for j in st.session_state["scan_jobs"]
)
scan_idle_timed_out = (
    st.session_state["scan_last_activity"] is not None
    and not scan_jobs_still_polling
    and (time.time() - st.session_state["scan_last_activity"]) > config.SCAN_SESSION_IDLE_TIMEOUT_SECONDS
)

# ── Session finalization trigger ──
if (
    st.session_state["scan_session_id"]
    and not st.session_state["scan_session_finalizing"]
    and scan_all_ended
    and (scan_idle_timed_out or st.session_state["scan_finalize_requested"])
):
    st.session_state["scan_finalize_requested"] = False
    ended_batch_ids = [
        bid for j in st.session_state["scan_jobs"] if j["state"] == "ended"
        for bid in (j["batch_ids"] or [])
    ]
    total_scan_pages = sum(j["page_count"] for j in st.session_state["scan_jobs"] if j["state"] == "ended")

    if ended_batch_ids:
        st.session_state["scan_session_finalizing"] = True
        start_session_finalize_thread(
            st.session_state["scan_session_id"],
            ended_batch_ids,
            st.session_state["scan_session_credit_ids"],
            len(st.session_state["scan_jobs"]),
            user_email=user_email,
            total_pages=total_scan_pages,
        )
    else:
        # Every batch failed at submission — nothing succeeded to retrieve.
        _scan_reset_session()
    st.rerun()

# ── Capture UI — hidden once the session is finalizing, so a fresh capture
#    never lands mid-way through an already-closing session ──
if not st.session_state["scan_session_finalizing"]:
    scan_capture = st.camera_input(
        "Take a photo of the next page",
        key=f"scan_cam_{st.session_state['scan_capture_count']}",
    )
    if scan_capture is not None:
        processed = process_captured_page(scan_capture.getvalue())
        st.session_state["scan_buffer"].append(processed)
        st.session_state["scan_capture_count"] += 1
        st.session_state["scan_last_activity"] = time.time()
        st.rerun()

    if st.session_state["scan_buffer"]:
        buffered = st.session_state["scan_buffer"]
        st.write(f"**{len(buffered)} page(s) captured, not yet submitted** "
                 f"(auto-submits at {config.SCAN_AUTO_SUBMIT_THRESHOLD}):")

        thumb_cols = st.columns(min(len(buffered), 5))
        for i, page in enumerate(buffered):
            with thumb_cols[i % len(thumb_cols)]:
                st.image(page["preview"], use_container_width=True)
                st.caption("✂️ Edges detected" if page["cropped"] else "Full photo used")
                if st.button("✕ Remove", key=f"scan_remove_{i}"):
                    st.session_state["scan_buffer"].pop(i)
                    st.rerun()

        scan_pages_now = len(st.session_state["scan_buffer"])
        if user_credits < scan_pages_now:
            st.error(
                f"🚫 Not enough credits to process these {scan_pages_now} page(s). "
                f"Available: {user_credits}."
            )
        elif st.button(f"🚀 Process These {scan_pages_now} Page(s) Now", use_container_width=True):
            _scan_finalize_batch()

        if len(st.session_state["scan_buffer"]) >= config.SCAN_AUTO_SUBMIT_THRESHOLD:
            _scan_finalize_batch()

# ── Per-batch status cards ──
if scan_has_batches:
    st.divider()
    st.subheader("📦 Scan Batches")

    for job in st.session_state["scan_jobs"]:
        with st.container(border=True):
            st.caption(f"Captured {job['created_at']} — {job['page_count']} page(s)")

            if job["state"] == "queued":
                st.info("⏳ Queued — waiting for the current batch's submission to finish.")
            elif job["state"] == "submitting":
                st.info("📤 Submitting in the background...")
            elif job["state"] == "awaiting_anthropic":
                st.info("⏳ Processing on Anthropic's side...")
            elif job["state"] == "ended":
                st.success("✅ Done — included in the session's combined results once you finish scanning.")
            elif job["state"] == "failed":
                st.error(f"❌ Failed: {job.get('error') or 'Unknown error'}")

    if not st.session_state["scan_session_finalizing"]:
        if scan_all_ended:
            st.button(
                "✅ Done Scanning — Send Results",
                use_container_width=True,
                type="primary",
                on_click=_scan_request_finalize,
            )
        else:
            st.caption("Waiting for all batches to finish before results can be sent.")

# ── Session finalize status ──
if st.session_state["scan_session_finalizing"]:
    st.divider()
    st.subheader("📧 Finalizing Scan Session")

    session_status = read_status(st.session_state["scan_session_id"])
    if session_status is None:
        st.info("⏳ Combining all batches into one Excel/email...")
        time.sleep(5)
        st.rerun()
    else:
        cleanup_batch_files(st.session_state["scan_session_id"])
        if session_status.get("success"):
            items = session_status.get("items", [])
            st.success(
                f"✅ Session complete — {len(items)} line item(s) extracted across "
                f"{len(st.session_state['scan_jobs'])} batch(es)."
            )
            if session_status.get("email_sent"):
                st.success(f"📧 Emailed to {user_email}")
            else:
                st.warning(
                    f"⚠️ Email could not be sent: {session_status.get('email_error', 'Unknown error')}\n\n"
                    f"Please download files below."
                )

            if items:
                st.subheader("📋 Extracted Data")
                st.dataframe(items, use_container_width=True, hide_index=True)

                if not session_status.get("email_sent"):
                    scan_excel_bytes       = create_excel(items, session_status.get("dup_warnings") or None)
                    scan_tally_erp9_bytes  = session_status.get("tally_erp9_bytes")
                    scan_tally_prime_bytes = session_status.get("tally_prime_bytes")
                    scan_ts                = datetime.now().strftime("%Y%m%d_%H%M%S")

                    st.subheader("📥 Download Files")
                    scan_dl1, scan_dl2, scan_dl3 = st.columns(3)
                    with scan_dl1:
                        st.download_button(
                            label     = "⬇️ Invoice Register (.xlsx)",
                            data      = scan_excel_bytes,
                            file_name = f"Invoice_Register_{len(items)}_items.xlsx",
                            mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width = True,
                        )
                    with scan_dl2:
                        if scan_tally_erp9_bytes:
                            st.download_button(
                                label     = "⬇️ Tally ERP 9 (.xml)",
                                data      = scan_tally_erp9_bytes.encode() if isinstance(scan_tally_erp9_bytes, str) else scan_tally_erp9_bytes,
                                file_name = f"Tally_ERP9_{scan_ts}.xml",
                                mime      = "application/xml",
                                use_container_width = True,
                            )
                    with scan_dl3:
                        if scan_tally_prime_bytes:
                            st.download_button(
                                label     = "⬇️ TallyPrime (.xml)",
                                data      = scan_tally_prime_bytes.encode() if isinstance(scan_tally_prime_bytes, str) else scan_tally_prime_bytes,
                                file_name = f"Tally_Prime_{scan_ts}.xml",
                                mime      = "application/xml",
                                use_container_width = True,
                            )
        else:
            st.error("❌ Session processing failed.")
            with st.expander("Error details"):
                st.code(session_status.get("error", "Unknown error"))

        if st.button("🔄 Start New Scan Session", use_container_width=True, type="primary"):
            _scan_reset_session()
            st.rerun()

elif scan_jobs_still_polling:
    time.sleep(3)
    st.rerun()
elif scan_has_batches and scan_all_ended and not scan_idle_timed_out:
    # Nothing left to poll, but the user hasn't clicked "Done Scanning" yet —
    # keep a slow heartbeat going so the idle timeout can still fire even
    # with no other reruns pending.
    time.sleep(30)
    st.rerun()

st.divider()


# ══════════════════════════════════════════════════════════════════════════════
#  REAL-TIME FLOW
# ══════════════════════════════════════════════════════════════════════════════

if process_requested and not is_batch:
    realtime_job_id = f"realtime_{uuid.uuid4().hex}"
    total_pages_for_reservation = selected_total_pages or len(processing_files)

    reservation = _apply_credit_reservation(
        realtime_job_id,
        total_pages_for_reservation,
        mode="realtime",
    )

    if reservation["success"]:
        st.session_state["processing"] = True

        with st.spinner("🔍 Extracting invoice data..."):
            result = process_realtime(processing_files)

        st.session_state["processing"] = False
        st.session_state["process_requested"] = False

        if result["success"]:
            _finalize_credit_reservation(realtime_job_id)
            items        = result["items"]
            dup_warnings = result.get("dup_warnings", [])
            fallbacks    = result.get("fallback_files", [])
            total_pages  = result.get("total_pages", total_pages_for_reservation)

            st.success(f"✅ Extracted **{len(items)}** line item(s) from {len(processing_files)} file(s).")

            # ── Fallback notice ──
            if fallbacks:
                st.warning(
                    f"⚠️ Scanned/image-based files sent as PDF: "
                    f"{', '.join(fallbacks)}"
                )

            # ── Duplicate warnings ──
            if dup_warnings:
                with st.expander(f"⚠️ {len(dup_warnings)} duplicate invoice(s) skipped", expanded=True):
                    for w in dup_warnings:
                        st.warning(w)

            # ── Data preview ──
            st.subheader("📋 Extracted Data")
            st.dataframe(items, use_container_width=True, hide_index=True)

            # ── Downloads ──
            st.subheader("📥 Downloads")
            excel_bytes       = create_excel(items, dup_warnings or None)
            tally_erp9_bytes  = result.get("tally_erp9_bytes")
            tally_prime_bytes = result.get("tally_prime_bytes")
            ts                = datetime.now().strftime("%Y%m%d_%H%M%S")

            dl1, dl2, dl3 = st.columns(3)
            with dl1:
                st.download_button(
                    label     = "⬇️ Invoice Register (.xlsx)",
                    data      = excel_bytes,
                    file_name = f"Invoice_Register_{len(items)}_items.xlsx",
                    mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width = True,
                )
            with dl2:
                if tally_erp9_bytes:
                    st.download_button(
                        label     = "⬇️ Tally ERP 9 (.xml)",
                        data      = tally_erp9_bytes,
                        file_name = f"Tally_ERP9_{ts}.xml",
                        mime      = "application/xml",
                        use_container_width = True,
                    )
            with dl3:
                if tally_prime_bytes:
                    st.download_button(
                        label     = "⬇️ TallyPrime (.xml)",
                        data      = tally_prime_bytes,
                        file_name = f"Tally_Prime_{ts}.xml",
                        mime      = "application/xml",
                        use_container_width = True,
                    )
            st.caption(
                f"Tally XML uses default ledger: **{config.TALLY_DEFAULT_LEDGER}** "
                f"— reassign ledgers inside Tally after import. "
                f"Both ERP 9 and TallyPrime files are always generated."
            )

            # ── Optional email ──
            with st.expander("📧 Also send via email?"):
                if st.button(f"Send to {user_email}"):
                    with st.spinner("Sending..."):
                        ok, msg = send_email(
                            excel_bytes       = excel_bytes,
                            cost              = None,
                            mode              = "Real-time API",
                            file_count        = len(processing_files),
                            item_count        = len(items),
                            user_email        = user_email,
                            dup_warnings      = dup_warnings or None,
                            upload_dup_warnings = duplicate_upload_warnings or None,
                            tally_erp9_bytes  = tally_erp9_bytes,
                            tally_prime_bytes = tally_prime_bytes,
                        )
                    if ok:
                        st.success(f"✅ Sent to {user_email}")
                    else:
                        st.error(f"❌ Email failed:\n{msg}")

        else:
            _refund_credit_reservation(
                realtime_job_id,
                reason=result.get("error") or "Real-time extraction failed",
            )
            st.error("❌ Processing failed.")
            with st.expander("Error details"):
                st.code(result["error"])
    else:
        st.session_state["processing"] = False
        st.session_state["process_requested"] = False


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — SUBMIT
# ══════════════════════════════════════════════════════════════════════════════
#
#  Submission runs in a background thread (start_submission_thread), not
#  inline here. submit_batch() does real CPU-bound local work — rendering
#  scanned pages, encoding images — that can take a while on CPU-constrained
#  hosts. Running it synchronously ties up Streamlit's single worker long
#  enough that it stops responding to the frontend's own keep-alive checks,
#  so the browser shows a "Connection error" regardless of whether
#  submission itself would have succeeded. Polling this way (short reruns,
#  matching the existing pattern just below for batch *processing* status)
#  keeps each rerun brief instead of one long blocking call.

if (
    process_requested
    and is_batch
    and not st.session_state["batch_submitted"]
    and not st.session_state["submission_started"]
):
    credit_job_id = f"batch_{uuid.uuid4().hex}"
    total_pages_for_reservation = selected_total_pages or len(processing_files)

    reservation = _apply_credit_reservation(
        credit_job_id,
        total_pages_for_reservation,
        mode="batch",
    )

    if reservation["success"]:
        st.session_state["submission_started"]          = True
        st.session_state["credit_job_id"]                = credit_job_id
        st.session_state["file_count"]                   = len(processing_files)
        st.session_state["batch_total_pages"]            = total_pages_for_reservation
        st.session_state["pending_upload_dup_warnings"]  = duplicate_upload_warnings or None
        start_submission_thread(credit_job_id, processing_files, user_email=user_email)
        st.rerun()
    else:
        st.session_state["processing"] = False
        st.session_state["process_requested"] = False


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — WAITING FOR SUBMISSION (background thread)
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state["submission_started"] and not st.session_state["batch_submitted"]:

    credit_job_id = st.session_state["credit_job_id"]

    st.divider()
    st.info(
        "📤 **Submitting batch job...** Running in the background — "
        "this page stays responsive while it works."
    )

    submit_status = read_submit_status(credit_job_id)

    if submit_status is None:
        time.sleep(3)
        st.rerun()

    else:
        cleanup_submit_status(credit_job_id)

        if submit_status["success"]:
            st.session_state["batch_ids"]          = submit_status["batch_ids"]
            st.session_state["batch_submitted"]    = True
            st.session_state["submission_started"] = False
            st.session_state["batch_total_pages"]  = (
                submit_status.get("total_pages") or st.session_state["batch_total_pages"]
            )
            st.session_state["processing"]        = False
            st.session_state["process_requested"] = False
            start_polling_thread(
                credit_job_id,
                submit_status["batch_ids"],
                st.session_state["file_count"],
                user_email=user_email,
                total_pages=st.session_state["batch_total_pages"],
                credit_job_id=credit_job_id,
                upload_dup_warnings=st.session_state.get("pending_upload_dup_warnings"),
            )
            st.rerun()
        else:
            _refund_credit_reservation(
                credit_job_id,
                reason=submit_status.get("error") or "Batch submission failed",
            )
            st.session_state["submission_started"] = False
            st.session_state["processing"]         = False
            st.session_state["process_requested"]  = False
            st.error(f"❌ Submission failed:\n{submit_status['error']}")


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — STATUS DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state["batch_submitted"] and st.session_state["batch_ids"]:

    credit_job_id = st.session_state["credit_job_id"]
    batch_ids     = st.session_state["batch_ids"]
    file_count    = st.session_state["file_count"]

    st.divider()
    st.subheader("📦 Batch Job")
    if len(batch_ids) == 1:
        st.caption(f"Batch ID: `{batch_ids[0]}`")
    else:
        st.caption(f"Batch IDs ({len(batch_ids)} parts): `{'`, `'.join(batch_ids)}`")

    status  = read_status(credit_job_id)
    is_done = status is not None

    if not is_done:
        st.info("⏳ **In Progress** — Processing your invoices in the background.")
        st.caption(
            f"Results will be emailed to **{user_email}** when complete. "
            f"You can safely close this tab."
        )
        time.sleep(30)
        st.rerun()

    elif status.get("success"):
        items        = status.get("items", [])
        dup_warnings = status.get("dup_warnings", [])
        total_pages  = status.get("total_pages") or st.session_state.get("batch_total_pages") or file_count

        st.success(f"✅ **Complete** — {len(items)} line item(s) extracted from {file_count} file(s).")

        if status.get("credit_finalized"):
            st.info(
                f"🪙 Reserved credits finalized "
                f"({total_pages} page(s) processed)."
            )
        elif status.get("credit_error"):
            st.warning(f"⚠️ Credit reservation update issue: {status['credit_error']}")

        # ── Duplicate warnings ──
        if dup_warnings:
            with st.expander(f"⚠️ {len(dup_warnings)} duplicate invoice(s) skipped", expanded=True):
                for w in dup_warnings:
                    st.warning(w)

        # ── Email status ──
        if status.get("email_sent"):
            st.success(f"📧 Files emailed to **{user_email}**")
        else:
            st.warning(
                f"⚠️ Email could not be sent: {status.get('email_error', 'Unknown error')}\n\n"
                f"Please download files below."
            )

        # ── Data + downloads ──
        if items:
            st.subheader("📋 Extracted Data")
            st.dataframe(items, use_container_width=True, hide_index=True)

            if not status.get("email_sent"):
                excel_bytes       = create_excel(items, dup_warnings or None)
                tally_erp9_bytes  = status.get("tally_erp9_bytes")
                tally_prime_bytes = status.get("tally_prime_bytes")
                ts                = datetime.now().strftime("%Y%m%d_%H%M%S")

                st.subheader("📥 Download Files")
                dl1, dl2, dl3 = st.columns(3)
                with dl1:
                    st.download_button(
                        label     = "⬇️ Invoice Register (.xlsx)",
                        data      = excel_bytes,
                        file_name = f"Invoice_Register_{len(items)}_items.xlsx",
                        mime      = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width = True,
                    )
                with dl2:
                    if tally_erp9_bytes:
                        st.download_button(
                            label     = "⬇️ Tally ERP 9 (.xml)",
                            data      = tally_erp9_bytes.encode() if isinstance(tally_erp9_bytes, str) else tally_erp9_bytes,
                            file_name = f"Tally_ERP9_{ts}.xml",
                            mime      = "application/xml",
                            use_container_width = True,
                        )
                with dl3:
                    if tally_prime_bytes:
                        st.download_button(
                            label     = "⬇️ TallyPrime (.xml)",
                            data      = tally_prime_bytes.encode() if isinstance(tally_prime_bytes, str) else tally_prime_bytes,
                            file_name = f"Tally_Prime_{ts}.xml",
                            mime      = "application/xml",
                            use_container_width = True,
                        )
                st.caption(
                    f"Tally XML uses default ledger: **{config.TALLY_DEFAULT_LEDGER}** "
                    f"— reassign ledgers inside Tally after import."
                )

        if status.get("error"):
            with st.expander("⚠️ Non-fatal processing warnings"):
                st.code(status["error"])

        st.divider()
        if st.button("🔄 Process another batch", use_container_width=True, type="primary"):
            cleanup_batch_files(credit_job_id)
            for k, v in batch_defaults.items():
                st.session_state[k] = v
            st.rerun()

    else:
        st.error("❌ **Failed** — Batch processing encountered an error.")
        if status.get("credit_refunded"):
            st.info("↩️ Reserved credits were refunded for this failed batch.")
        elif status.get("credit_error"):
            st.warning(f"⚠️ Credit refund issue: {status['credit_error']}")
        with st.expander("Error details"):
            st.code(status.get("error", "Unknown error"))

        if st.button("🔄 Try again", use_container_width=True):
            cleanup_batch_files(credit_job_id)
            for k, v in batch_defaults.items():
                st.session_state[k] = v
            st.rerun()


# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("Invoice Processor MVP")
