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
    read_status,
    start_polling_thread,
    submit_batch,
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
from utils import count_uploaded_pdf_pages, create_excel, detect_duplicate_uploads, send_email


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title = "Invoice Processor",
    page_icon  = "🧾",
    layout     = "centered",
)


# ── Session state initialisation ─────────────────────────────────────────────

auth_defaults = {
    "logged_in":    False,
    "user_email":   None,
    "user_credits": 0,
    "otp_sent":     False,
    "otp_email":    "",
    "process_requested": False,
}

batch_defaults = {
    "batch_id":          None,
    "batch_submitted":   False,
    "file_count":        0,
    "batch_total_pages": 0,
    "credit_job_id":     None,
    "processing":        False,
}

for k, v in {**auth_defaults, **batch_defaults}.items():
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

        if st.button("Send OTP", type="primary", use_container_width=True):
            if not email_input or "@" not in email_input:
                st.error("Please enter a valid email address.")
            else:
                with st.spinner("Sending OTP..."):
                    result = request_otp(email_input.strip())

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
            if st.button("Verify OTP", type="primary", use_container_width=True):
                if not otp_input or len(otp_input.strip()) != 6:
                    st.error("Please enter the 6-digit OTP.")
                else:
                    with st.spinner("Verifying..."):
                        result = validate_otp(
                            st.session_state["otp_email"],
                            otp_input.strip(),
                        )
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
            if st.button("← Use different email", use_container_width=True):
                st.session_state["otp_sent"]  = False
                st.session_state["otp_email"] = ""
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
        for k, v in {**auth_defaults, **batch_defaults}.items():
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

if process_requested and is_batch and not st.session_state["batch_submitted"]:

    st.session_state["batch_submitted"] = True
    credit_job_id = f"batch_{uuid.uuid4().hex}"
    total_pages_for_reservation = selected_total_pages or len(processing_files)

    reservation = _apply_credit_reservation(
        credit_job_id,
        total_pages_for_reservation,
        mode="batch",
    )

    if reservation["success"]:
        with st.spinner("📤 Submitting batch job..."):
            sub = submit_batch(processing_files, user_email=user_email)

        if sub["success"]:
            st.session_state["batch_id"]          = sub["batch_id"]
            st.session_state["file_count"]        = len(processing_files)
            st.session_state["batch_total_pages"] = sub.get("total_pages") or total_pages_for_reservation
            st.session_state["credit_job_id"]     = credit_job_id
            st.session_state["processing"]        = False
            st.session_state["process_requested"] = False
            start_polling_thread(
                sub["batch_id"],
                len(processing_files),
                user_email=user_email,
                total_pages=st.session_state["batch_total_pages"],
                credit_job_id=credit_job_id,
                upload_dup_warnings=duplicate_upload_warnings or None,
            )
        else:
            _refund_credit_reservation(
                credit_job_id,
                reason=sub.get("error") or "Batch submission failed",
            )
            st.session_state["batch_submitted"] = False
            st.session_state["processing"] = False
            st.session_state["process_requested"] = False
            st.error(f"❌ Submission failed:\n{sub['error']}")
    else:
        st.session_state["batch_submitted"] = False
        st.session_state["processing"] = False
        st.session_state["process_requested"] = False


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — STATUS DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

if st.session_state["batch_submitted"] and st.session_state["batch_id"]:

    batch_id   = st.session_state["batch_id"]
    file_count = st.session_state["file_count"]

    st.divider()
    st.subheader("📦 Batch Job")
    st.caption(f"Batch ID: `{batch_id}`")

    status  = read_status(batch_id)
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
            cleanup_batch_files(batch_id)
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
            cleanup_batch_files(batch_id)
            for k, v in batch_defaults.items():
                st.session_state[k] = v
            st.rerun()


# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("Invoice Processor MVP")
