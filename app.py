# ─────────────────────────────────────────────
#  app.py  –  Streamlit UI for Invoice Processor MVP
#  Run with:  streamlit run app.py
# ─────────────────────────────────────────────

import time
from datetime import datetime

import streamlit as st

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
from db import deduct_credits, get_user_credits
from realtime_processor import process_realtime
from utils import create_excel, send_email


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
}

batch_defaults = {
    "batch_id":        None,
    "batch_submitted": False,
    "file_count":      0,
    "processing":      False,
}

for k, v in {**auth_defaults, **batch_defaults}.items():
    if k not in st.session_state:
        st.session_state[k] = v


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
                        st.session_state["logged_in"]    = True
                        st.session_state["user_email"]   = st.session_state["otp_email"]
                        st.session_state["user_credits"] = result["credits"]
                        st.session_state["otp_sent"]     = False
                        st.session_state["otp_email"]    = ""
                        st.rerun()
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
        for k, v in {**auth_defaults, **batch_defaults}.items():
            st.session_state[k] = v
        st.rerun()

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

if uploaded_files:
    st.success(
        f"{len(uploaded_files)} file(s) selected: "
        f"{', '.join(f.name for f in uploaded_files)}"
    )


# ── Mode selection ────────────────────────────────────────────────────────────

st.subheader("Processing Mode")
mode     = st.radio(
    label   = "Choose how to process your invoices:",
    options = ["⚡ Real-time API", "📦 Batch API (50% cheaper — results by email)"],
    index   = 0,
)
is_batch = mode.startswith("📦")

if is_batch:
    st.info(
        f"📧 Results will be emailed to **{user_email}** when complete.\n\n"
        f"⏱️ Status checked every **{config.POLL_INTERVAL_SECONDS // 60} minute(s)** in background.\n\n"
        f"✅ You can safely close this tab — the job continues running.",
        icon="ℹ️",
    )
else:
    st.info("⚡ Results appear on this page immediately.", icon="ℹ️")

st.divider()


# ── Process button ────────────────────────────────────────────────────────────

btn_disabled = (
    not uploaded_files
    or st.session_state["batch_submitted"]
    or st.session_state["processing"]
)

process_btn = st.button(
    label               = "🚀 Process Invoices",
    disabled            = btn_disabled,
    use_container_width = True,
    type                = "primary",
)

if not uploaded_files:
    st.caption("⬆️ Upload at least one PDF to enable processing.")


# ══════════════════════════════════════════════════════════════════════════════
#  REAL-TIME FLOW
# ══════════════════════════════════════════════════════════════════════════════

if process_btn and not is_batch:
    st.session_state["processing"] = True

    with st.spinner("🔍 Extracting invoice data..."):
        result = process_realtime(uploaded_files)

    st.session_state["processing"] = False

    if result["success"]:
        items        = result["items"]
        dup_warnings = result.get("dup_warnings", [])
        fallbacks    = result.get("fallback_files", [])
        total_pages  = result.get("total_pages", len(uploaded_files))

        st.success(f"✅ Extracted **{len(items)}** line item(s) from {len(uploaded_files)} file(s).")

        # ── Deduct credits after successful extraction ──
        deduction = deduct_credits(user_email, total_pages)
        if deduction["success"]:
            st.session_state["user_credits"] = deduction["credits_after"]
            st.info(
                f"🪙 **{deduction['credits_deducted']} credit(s) used** "
                f"({total_pages} page(s) processed). "
                f"Remaining: **{deduction['credits_after']}**"
            )
        else:
            st.warning(f"⚠️ Could not deduct credits: {deduction.get('error')}")

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
            if st.button(f"Send to {user_email} + admin"):
                with st.spinner("Sending..."):
                    ok, msg = send_email(
                        excel_bytes       = excel_bytes,
                        cost              = None,
                        mode              = "Real-time API",
                        file_count        = len(uploaded_files),
                        item_count        = len(items),
                        user_email        = user_email,
                        dup_warnings      = dup_warnings or None,
                        tally_erp9_bytes  = tally_erp9_bytes,
                        tally_prime_bytes = tally_prime_bytes,
                    )
                if ok:
                    st.success(f"✅ Sent to {user_email} and admin")
                else:
                    st.error(f"❌ Email failed:\n{msg}")

    else:
        st.error("❌ Processing failed.")
        with st.expander("Error details"):
            st.code(result["error"])


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — SUBMIT
# ══════════════════════════════════════════════════════════════════════════════

if process_btn and is_batch and not st.session_state["batch_submitted"]:

    st.session_state["batch_submitted"] = True

    with st.spinner("📤 Submitting batch job..."):
        sub = submit_batch(uploaded_files, user_email=user_email)

    if sub["success"]:
        st.session_state["batch_id"]   = sub["batch_id"]
        st.session_state["file_count"] = len(uploaded_files)
        start_polling_thread(sub["batch_id"], len(uploaded_files), user_email=user_email)
    else:
        st.session_state["batch_submitted"] = False
        st.error(f"❌ Submission failed:\n{sub['error']}")


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
        total_pages  = status.get("total_pages", file_count)

        st.success(f"✅ **Complete** — {len(items)} line item(s) extracted from {file_count} file(s).")

        # ── Deduct credits ──
        deduction = deduct_credits(user_email, total_pages)
        if deduction["success"]:
            st.session_state["user_credits"] = deduction["credits_after"]
            st.info(
                f"🪙 **{deduction['credits_deducted']} credit(s) used** "
                f"({total_pages} page(s) processed). "
                f"Remaining: **{deduction['credits_after']}**"
            )

        # ── Duplicate warnings ──
        if dup_warnings:
            with st.expander(f"⚠️ {len(dup_warnings)} duplicate invoice(s) skipped", expanded=True):
                for w in dup_warnings:
                    st.warning(w)

        # ── Email status ──
        if status.get("email_sent"):
            st.success(f"📧 Files emailed to **{user_email}** and admin")
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
