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

**Also required: the Tesseract OCR binary** (used for scanned pages with no
text layer — see [PDF handling](#pdf-handling) below). `pytesseract` in
`requirements.txt` is just a Python wrapper; the actual OCR engine must be
installed separately on the host:
- Local dev (macOS): `brew install tesseract`
- Local dev (Debian/Ubuntu): `apt-get install tesseract-ocr`
- Render: add `tesseract-ocr` via a system package (e.g. an `apt.txt` /
  Aptfile-style buildpack, or a Docker-based deploy with `apt-get install
  tesseract-ocr` in the build step) — check Render's current docs for
  installing apt packages on your plan.

If Tesseract isn't installed, the app still works correctly — it just always
falls back to sending scanned pages to Claude as images instead of using
local OCR (the pre-OCR behavior).

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
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Claude model to use — defaults to Haiku 4.5 for cost (~3x cheaper than Sonnet); switch to `claude-sonnet-4-6` if extraction accuracy needs it |
| `MAX_TOKENS` | `8192` | Max output tokens for real-time (synchronous) API calls |
| `BATCH_MAX_TOKENS` | `32000` | Max output tokens per file for Batch API calls. For models in `OUTPUT_300K_BETA_MODELS` (config.py — Sonnet/Opus family), this is submitted with the `output-300k-2026-03-24` beta header and can go up to `300000`. Haiku 4.5 isn't on Anthropic's supported list for that beta, so on Haiku the real ceiling is its standard `64000` batch cap regardless of this value |
| `PRICE_INPUT_PER_MTOK` | `1.00` | Input token price (USD per million) — matches Haiku 4.5; update if you change `ANTHROPIC_MODEL` |
| `PRICE_OUTPUT_PER_MTOK` | `5.00` | Output token price (USD per million) — matches Haiku 4.5; update if you change `ANTHROPIC_MODEL` |
| `POLL_INTERVAL_SECONDS` | `120` | How often to check batch status (seconds) |
| `SKIP_DUPLICATE_INVOICE_NUMBERS` | `true` | Skip duplicate invoice numbers across files |
| `MIN_PAGE_TEXT_CHARS` | `50` | Min chars to consider a page text-based (below = try OCR, else image fallback) |
| `HANDWRITING_INK_THRESHOLD` | `0.005` | Fraction of colored (non-black) ink pixels above which a page is treated as having handwriting/stamps and skips local OCR |
| `OCR_RENDER_RESOLUTION_DPI` | `150` | Resolution used to render scanned pages, both for the local OCR attempt and as the image sent to Claude if OCR is skipped |
| `ANNOTATION_CHECK_MAX_DIM` | `400` | Max pixel dimension of the thumbnail used for the handwriting/stamp check — doesn't affect the image actually sent to Claude, only this detection step |
| `IMAGE_JPEG_QUALITY` | `85` | JPEG quality for fallback-page images sent to Claude |
| `MAX_REQUEST_PAYLOAD_MB` | `25` | Safety margin under Anthropic's 32MB request-size limit — a chunk still over this after JPEG compression fails clearly instead of being rejected by the API |
| `CPU_YIELD_SECONDS` | `0.05` | Cooperative pause after each page needing the expensive render/detect/encode path, so a multi-page file doesn't starve the app's own health-check handling on CPU-constrained hosts |
| `MAX_FALLBACK_PAGES_PER_REQUEST` | `5` | A file with more fallback-image pages than this is split across multiple separate Batch API jobs instead of one, so local processing happens in smaller bursts with real network I/O gaps between them |
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

### Batch mode only
1. Upload one or more invoice PDFs
2. The app counts pages, checks credits, and skips high-confidence duplicate uploaded PDFs
3. Click **Process Invoices** — the button disables immediately
4. Credits are reserved and the job is submitted to Anthropic Batch API in the background
5. Status is shown as In Progress → Complete / Failed
6. When complete: Excel + both Tally XML files are emailed automatically with duplicate summaries
7. You can safely close the browser — polling continues in background

Real-time processing code remains in the project, but the UI is temporarily disabled and all jobs use Batch API by default.

---


### Session handling
- Login sessions are stored in a browser cookie backed by hashed Supabase session records
- Session IDs are not placed in the URL

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
- Post-Claude duplicate handling skips only duplicate line items, so multiple different line items from the same invoice are preserved
- Skipped duplicates are shown as warnings in the UI and post-Claude skips also appear in a separate Excel sheet
- Controlled by `SKIP_DUPLICATE_INVOICE_NUMBERS` env var for post-Claude deduplication

---

## PDF handling

- Uploaded PDFs are page-counted before processing, so users see required credits upfront
- Processing is blocked when selected PDF pages exceed available credits
- Credits are reserved atomically when processing starts, finalized on extraction success, and refunded on extraction failure
- **Duplicate pages**: exact duplicate pages within a PDF are detected via MD5 hash and skipped
- Per page, in order:
  1. **Text-based pages** (most digitally-generated invoices): text extracted via pdfplumber — cheapest, most reliable
  2. **Scanned pages with no handwriting/stamps**: read via local Tesseract OCR (free, no Claude cost) if Tesseract is installed on the host
  3. **Scanned pages with handwriting or rubber stamps, or where OCR is unavailable/produced too little text**: sent to Claude as an image instead of guessing locally — costs more (image tokens), but local OCR has been observed to silently misread handwritten numbers without any low-confidence signal to catch it, which is too risky for financial figures
- A file can mix all three per page — e.g. a mostly-clean scan with one stamped page only sends that one page as an image, not the whole file
- Handwriting/stamp detection looks for colored (non-black) ink pixels above `HANDWRITING_INK_THRESHOLD` — pen and stamp ink is almost always blue/purple/red, unlike printed black text

---

## Cost Estimates (Claude Haiku 4.5, the default model)

Real per-job costs observed in production (Sonnet 4.6, before the default
model switch to Haiku) ranged roughly $0.01–$0.55 depending on file size and
whether pages were text-based or scanned images — scanned/image-heavy files
cost several times more than text-based ones per page (see PDF handling
below). Haiku 4.5 prices at roughly a third of Sonnet's per-token rate, so
expect proportionally lower costs, but this hasn't been validated with real
production jobs yet — treat any specific number here as a rough guide, not
a guarantee, until it has.

**Before relying on Haiku 4.5 in production:** its extraction accuracy on
your real documents hasn't been validated against Sonnet's — run a side-by-side
comparison on a batch of real invoices and check the extracted line items
match before trusting it for financial data. Switch back to Sonnet via
`ANTHROPIC_MODEL=claude-sonnet-4-6` (and update `PRICE_INPUT_PER_MTOK` /
`PRICE_OUTPUT_PER_MTOK` back to `3.00` / `15.00`) if accuracy regresses.

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
            ├── extract_text_from_pdf()  — pdfplumber extraction, per-page OCR/image routing, dedup
            ├── build_file_content()     — builds Claude content blocks (text + image) for one file
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