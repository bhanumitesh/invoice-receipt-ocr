# ─────────────────────────────────────────────
#  app.py  –  Streamlit UI for Invoice Processor MVP
#  Run with:  streamlit run app.py
# ─────────────────────────────────────────────

import time

import streamlit as st

import config
from batch_processor import (
    cleanup_batch_files,
    read_status,
    start_polling_thread,
    submit_batch,
)
from realtime_processor import process_realtime
from utils import create_excel, send_email


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title = "Invoice Processor",
    page_icon  = "🧾",
    layout     = "centered",
)

st.title("🧾 Invoice Processor")
st.caption("Extract structured data from invoice PDFs using Claude AI")
st.divider()


# ── Session state ─────────────────────────────────────────────────────────────
# IMPORTANT: batch background thread never writes to session_state.
# It only writes to log/status files. app.py reads those files on each rerun.

defaults = {
    "batch_id":         None,
    "batch_submitted":  False,  # True the instant submit button is clicked
    "file_count":       0,
    "processing":       False,  # True while real-time call is in flight
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── File uploader ─────────────────────────────────────────────────────────────

uploaded_files = st.file_uploader(
    "Upload invoice PDFs",
    type                  = ["pdf"],
    accept_multiple_files = True,
    help                  = "Select one or more PDF files to process.",
)

if uploaded_files:
    st.success(
        f"{len(uploaded_files)} file(s) selected: "
        f"{', '.join(f.name for f in uploaded_files)}"
    )


# ── Mode selection ────────────────────────────────────────────────────────────

st.subheader("Processing Mode")
mode = st.radio(
    label   = "Choose how to process your invoices:",
    options = ["⚡ Real-time API", "📦 Batch API (50% cheaper — results by email)"],
    index   = 0,
)
is_batch = mode.startswith("📦")

if is_batch:
    st.info(
        f"📧 Results will be emailed to **{config.RECIPIENT_EMAIL}** when complete.\n\n"
        f"⏱️ Status checked every **{config.POLL_INTERVAL_SECONDS // 60} minute(s)** in background.\n\n"
        f"✅ You can safely close this tab — the job continues running.",
        icon="ℹ️",
    )
else:
    st.info("⚡ Results appear on this page immediately.", icon="ℹ️")

st.divider()


# ── Process button ────────────────────────────────────────────────────────────
# Disabled if:
#   - No files uploaded
#   - A batch job is already submitted (session_state set immediately on click)
#   - Real-time processing is currently in flight

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
    # Disable button immediately by setting processing flag before API call
    st.session_state["processing"] = True

    with st.spinner("🔍 Extracting invoice data..."):
        result = process_realtime(uploaded_files)

    st.session_state["processing"] = False

    if result["success"]:
        items        = result["items"]
        cost         = result["cost"]
        dup_warnings = result.get("dup_warnings", [])
        fallbacks    = result.get("fallback_files", [])

        st.success(f"✅ Extracted **{len(items)}** line item(s) from {len(uploaded_files)} file(s).")

        # ── Fallback notice ──
        if fallbacks:
            st.warning(
                f"⚠️ The following file(s) are scanned/image-based and were sent as PDF "
                f"(higher token cost): {', '.join(fallbacks)}"
            )

        # ── Duplicate warnings ──
        if dup_warnings:
            with st.expander(f"⚠️ {len(dup_warnings)} duplicate invoice(s) skipped", expanded=True):
                for w in dup_warnings:
                    st.warning(w)

        # ── Cost ──
        st.subheader("💰 Processing Cost")
        c1, c2, c3 = st.columns(3)
        c1.metric("Input tokens",  f"{cost['input_tokens']:,}")
        c2.metric("Output tokens", f"{cost['output_tokens']:,}")
        c3.metric("Total cost",    f"${cost['total_cost_usd']:.4f}")
        st.caption(
            f"Input: ${cost['input_cost_usd']:.4f}  |  "
            f"Output: ${cost['output_cost_usd']:.4f}  |  "
            f"Model: {config.MODEL}"
        )

        # ── Data preview ──
        st.subheader("📋 Extracted Data")
        st.dataframe(items, use_container_width=True, hide_index=True)

        # ── Download ──
        excel_bytes = create_excel(items, dup_warnings or None)
        st.download_button(
            label               = "⬇️ Download Invoice Register (.xlsx)",
            data                = excel_bytes,
            file_name           = f"Invoice_Register_{len(items)}_items.xlsx",
            mime                = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width = True,
        )

        # ── Optional email ──
        with st.expander("📧 Also send via email?"):
            if st.button(f"Send to {config.RECIPIENT_EMAIL}"):
                with st.spinner("Sending..."):
                    ok, msg = send_email(
                        excel_bytes  = excel_bytes,
                        cost         = cost,
                        mode         = "Real-time API",
                        file_count   = len(uploaded_files),
                        item_count   = len(items),
                        dup_warnings = dup_warnings or None,
                    )
                if ok:
                    st.success(f"✅ Sent to {config.RECIPIENT_EMAIL}")
                else:
                    st.error(f"❌ Email failed:\n{msg}")

    else:
        st.error("❌ Processing failed.")
        with st.expander("Error details"):
            st.code(result["error"])


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — SUBMIT
#  Button disable happens immediately because session_state["batch_submitted"]
#  is set to True right here, before any sleep/rerun.
# ══════════════════════════════════════════════════════════════════════════════

if process_btn and is_batch and not st.session_state["batch_submitted"]:

    # Set flag IMMEDIATELY — this disables the button on the very next render
    st.session_state["batch_submitted"] = True

    with st.spinner("📤 Submitting batch job..."):
        sub = submit_batch(uploaded_files)

    if sub["success"]:
        st.session_state["batch_id"]    = sub["batch_id"]
        st.session_state["file_count"]  = len(uploaded_files)

        # Launch background polling thread (no callbacks — uses log files)
        start_polling_thread(sub["batch_id"], len(uploaded_files))

    else:
        # Submission failed — re-enable button
        st.session_state["batch_submitted"] = False
        st.error(f"❌ Submission failed:\n{sub['error']}")


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH FLOW — STATUS DISPLAY
#  Shows a clean status indicator only (no raw logs shown to user).
#  Reads status file written by background thread.
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
        # ── In progress ──
        st.info("⏳ **In Progress** — Processing your invoices in the background.")
        st.caption(
            f"Results will be emailed to **{config.RECIPIENT_EMAIL}** when complete. "
            f"You can safely close this tab."
        )
        # Auto-refresh every 30 seconds to pick up completion
        time.sleep(30)
        st.rerun()

    elif status.get("success"):
        # ── Complete ──
        items        = status.get("items", [])
        cost         = status.get("cost", {})
        realtime_cost = status.get("realtime_cost", {})
        dup_warnings = status.get("dup_warnings", [])

        st.success(f"✅ **Complete** — {len(items)} line item(s) extracted from {file_count} file(s).")

        # ── Duplicate warnings ──
        if dup_warnings:
            with st.expander(f"⚠️ {len(dup_warnings)} duplicate invoice(s) skipped", expanded=True):
                for w in dup_warnings:
                    st.warning(w)

        # ── Email status ──
        if status.get("email_sent"):
            st.success(f"📧 Excel report emailed to **{config.RECIPIENT_EMAIL}**")
        else:
            st.warning(
                f"⚠️ Email could not be sent: {status.get('email_error', 'Unknown error')}\n\n"
                f"Your data is shown below — please download manually."
            )

        # ── Cost ──
        st.subheader("💰 Cost Summary (Batch — 50% discount applied)")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Input tokens",  f"{cost.get('input_tokens', 0):,}")
        c2.metric("Output tokens", f"{cost.get('output_tokens', 0):,}")
        c3.metric("Batch cost",    f"${cost.get('total_cost_usd', 0):.4f}")
        if realtime_cost:
            saving = realtime_cost.get("total_cost_usd", 0) - cost.get("total_cost_usd", 0)
            c4.metric("Saved", f"${saving:.4f}", delta="-50%")

        # ── Data preview ──
        if items:
            st.subheader("📋 Extracted Data")
            st.dataframe(items, use_container_width=True, hide_index=True)

            # Manual download as fallback if email failed
            if not status.get("email_sent"):
                excel_bytes = create_excel(items, dup_warnings or None)
                st.download_button(
                    label               = "⬇️ Download Invoice Register (.xlsx)",
                    data                = excel_bytes,
                    file_name           = f"Invoice_Register_{len(items)}_items.xlsx",
                    mime                = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width = True,
                )

        # ── Non-fatal warnings ──
        if status.get("error"):
            with st.expander("⚠️ Non-fatal processing warnings"):
                st.code(status["error"])

        # ── Reset ──
        st.divider()
        if st.button("🔄 Process another batch", use_container_width=True, type="primary"):
            cleanup_batch_files(batch_id)
            for k, v in defaults.items():
                st.session_state[k] = v
            st.rerun()

    else:
        # ── Failed ──
        st.error("❌ **Failed** — Batch processing encountered an error.")
        with st.expander("Error details"):
            st.code(status.get("error", "Unknown error"))

        if st.button("🔄 Try again", use_container_width=True):
            cleanup_batch_files(batch_id)
            for k, v in defaults.items():
                st.session_state[k] = v
            st.rerun()


# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    f"Model: `{config.MODEL}` | "
    f"Real-time: ${config.PRICE_INPUT_PER_MTOK}/M input, "
    f"${config.PRICE_OUTPUT_PER_MTOK}/M output | "
    f"Batch: 50% off"
)
