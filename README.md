# Invoice Processor MVP

Extract structured data from invoice PDFs using Claude AI.
Supports real-time and batch processing with Excel + Tally XML output via email.

---

## File Structure

```
invoice_processor/
├── app.py                  # Streamlit UI — run this
├── realtime_processor.py   # Real-time API logic
├── batch_processor.py      # Batch API submit + polling + retrieval
├── utils.py                # Shared: PDF extraction, Excel, Tally XML, email, cost calc
├── config.py               # All settings — reads from environment variables
├── requirements.txt        # Python dependencies
├── .env.example            # Template for local development
└── README.md               # This file
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Environment variables

All sensitive settings are read from environment variables — nothing is hardcoded.

**For local development**, copy `.env.example` to `.env` and fill in your values:
```bash
cp .env.example .env
```

**For server deployment (Render etc.)**, set these directly as environment variables on your server.

#### Required variables

| Variable | Description | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | console.anthropic.com → API Keys |
| `RESEND_API_KEY` | Resend email API key | resend.com → API Keys |
| `RESEND_SENDER` | Verified sender address | resend.com → Domains (or use `onboarding@resend.dev` for testing) |
| `ADMIN_EMAIL` | Admin report recipients, sent as BCC — comma-separated for multiple | e.g. `admin1@gmail.com,admin2@gmail.com` |
| `SUPABASE_URL` | Supabase project URL | Supabase dashboard → Settings → API |
| `SUPABASE_KEY` | Supabase anon/public key | Supabase dashboard → Settings → API |

#### Optional variables (defaults shown)

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model to use |
| `MAX_TOKENS` | `8192` | Max output tokens per API call |
| `PRICE_INPUT_PER_MTOK` | `3.00` | Input token price (USD per million) |
| `PRICE_OUTPUT_PER_MTOK` | `15.00` | Output token price (USD per million) |
| `POLL_INTERVAL_SECONDS` | `120` | How often to check batch status (seconds) |
| `SKIP_DUPLICATE_INVOICE_NUMBERS` | `true` | Skip duplicate invoice numbers across files |
| `MIN_PAGE_TEXT_CHARS` | `50` | Min chars to consider a page text-based (below = scanned fallback) |
| `TALLY_DEFAULT_LEDGER` | `Purchase Account` | Default ledger for all Tally XML imports — set to exact ledger name in your Tally company |
| `TALLY_COMPANY_NAME` | `My Company` | Your company name exactly as it appears in Tally |

### 3. Resend setup (email)

Resend is used instead of Gmail SMTP because cloud platforms (Render, Railway etc.)
block outbound SMTP ports. Resend uses HTTPS and works everywhere.

1. Sign up free at [resend.com](https://resend.com) — 3,000 emails/month permanently free, no credit card
2. Go to API Keys → Create API Key → copy to `RESEND_API_KEY`
3. For sender address:
   - **Testing/MVP**: use `onboarding@resend.dev` as `RESEND_SENDER` (works immediately, recipients must be your own verified email)
   - **Production**: go to Domains → Add Domain → follow DNS instructions → use `invoices@yourdomain.com`

### 4. Create Supabase tables
Run `supabase_setup.sql` in the Supabase SQL editor. The app requires `users`, `otp_tokens`, `auth_sessions`, `credit_transactions`, and the credit reservation RPC functions in that SQL file.

### 5. Run the app
```bash
streamlit run app.py
```
Opens at http://localhost:8501

---

## How to Use

### Real-time mode
1. Upload one or more invoice PDFs
2. Select **Real-time API**
3. Click **Process Invoices**
4. Results appear on screen immediately
5. Download Invoice Register (.xlsx), Tally ERP 9 (.xml), or TallyPrime (.xml) directly
6. Optionally send all files via email

### Batch mode
1. Upload one or more invoice PDFs
2. Select **Batch API (50% cheaper)**
3. Click **Process Invoices** — button disables immediately
4. Job submitted to Anthropic in background
5. Status shown as In Progress → Complete / Failed
6. When complete: Excel + both Tally XML files emailed automatically with cost summary
7. You can safely close the browser — polling continues in background

---

## Output Files

Every run produces three files:

| File | Description |
|---|---|
| `Invoice_Register.xlsx` | Full register for CA review — all line items, GST breakdown, HSN codes |
| `Tally_ERP9_Import.xml` | Import into Tally ERP 9 via Gateway → Import Data → Vouchers |
| `Tally_Prime_Import.xml` | Import into TallyPrime 3.x via Gateway → Import → Data |

### Tally import notes
- All line items post to the default ledger set in `TALLY_DEFAULT_LEDGER`
- Reassign to correct ledgers inside Tally after import
- GST ledgers (CGST, SGST/UTGST, IGST) are created as separate entries automatically
- Party (vendor) is set as the creditor ledger
- Both ERP 9 and TallyPrime files are always generated — use whichever applies to your version

### Duplicate invoice handling
- Upload-time precheck skips high-confidence duplicate PDFs before Claude processing
- A PDF is auto-skipped only when both vendor GSTIN and invoice number are readable locally and match an earlier uploaded PDF
- Ambiguous or scanned PDFs are still processed, then the post-Claude duplicate check runs as a fallback
- Post-Claude duplicate handling prefers GSTIN + invoice number, and falls back to invoice number when GSTIN is missing
- Skipped duplicates are shown as warnings in the UI and post-Claude skips also appear in a separate Excel sheet
- Controlled by `SKIP_DUPLICATE_INVOICE_NUMBERS` env var for post-Claude deduplication

---

## PDF handling

- Uploaded PDFs are page-counted before processing, so users see required credits upfront
- Processing is blocked when selected PDF pages exceed available credits
- Credits are reserved atomically when processing starts, finalized on extraction success, and refunded on extraction failure
- **Text-based PDFs** (most invoices): text extracted via pdfplumber — cheaper, fewer tokens
- **Scanned/image PDFs**: automatically detected and sent as PDF binary (fallback) — works but costs more
- **Duplicate pages**: exact duplicate pages within a PDF are detected via MD5 hash and skipped

---

## Cost Estimates (Claude Sonnet 4.6)

| Mode | ~Cost per job | Notes |
|---|---|---|
| Real-time | ~$0.03–$0.10 | Depends on number and density of invoices |
| Batch API | ~$0.015–$0.05 | 50% cheaper — same output, processed async |

Cost breakdown (input/output tokens + total) is shown in the UI for real-time
and included in the email for batch jobs, along with savings vs real-time.

Dense invoices (e.g. Meta Ads with many line items) cost more than simple tax invoices.

---

## Architecture

```
app.py  (Streamlit UI)
    │
    ├── realtime_processor.py
    │       └── Sends PDFs/text to Claude → parses JSON → creates Excel + XML
    │
    ├── batch_processor.py
    │       ├── submit_batch()       — submits to Anthropic Batch API
    │       ├── poll_until_done()    — background thread, writes to batch_logs/
    │       └── retrieve_results()  — downloads results, creates Excel + XML, sends email
    │
    └── utils.py  (shared)
            ├── extract_text_from_pdf()  — pdfplumber extraction + dedup + fallback detection
            ├── parse_json_response()    — parses abbreviated JSON, expands keys, detects truncation
            ├── detect_duplicate_uploads() — skips high-confidence duplicate PDFs before Claude
            ├── deduplicate_items()      — fallback duplicate removal after extraction
            ├── create_excel()           — formatted Excel with optional warnings sheet
            ├── create_tally_xml()       — TallyXML for ERP 9 and TallyPrime
            ├── calculate_cost()         — token-based cost calculation
            └── send_email()             — Resend API, multiple recipients, multiple attachments
```

### Batch thread safety
The background polling thread **never writes to Streamlit session_state** (this causes crashes).
All thread-to-UI communication uses files in `batch_logs/`:
- `batch_<id>.log` — append-only status log (for debugging)
- `batch_<id>.status` — JSON written once when done; app.py polls this on each rerun

---

## Known limitations (MVP)

- Batch polling thread does not survive server restarts — resubmit if this happens
- If a server restart interrupts an in-progress job, a reserved credit transaction may need admin review/refund in Supabase
- Real-time extraction results are not persisted after leaving the completed page
- Ledger mapping is manual inside Tally — automated mapping planned for future
- Tally XML uses Purchase voucher type only — Sales vouchers planned for future