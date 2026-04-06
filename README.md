# Invoice Processor MVP

Extract structured data from invoice PDFs using Claude AI.
Supports both real-time and batch processing with Excel output via email.

---

## File Structure

```
invoice_processor/
├── app.py                  # Streamlit UI — run this
├── realtime_processor.py   # Real-time API logic
├── batch_processor.py      # Batch API submit + polling + retrieval
├── utils.py                # Shared: Excel creation, email, cost calc
├── config.py               # Your API keys and settings ← fill this in first
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure config.py
Fill in these values in `config.py`:

| Setting | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `GMAIL_SENDER` | Your Gmail address |
| `GMAIL_APP_PASS` | myaccount.google.com → Security → 2-Step Verification → App Passwords → Generate |
| `RECIPIENT_EMAIL` | Where to send Excel reports |

### 3. Gmail App Password (important)
Google does not allow regular passwords for SMTP.
You must generate an App Password:
1. Go to myaccount.google.com
2. Security → 2-Step Verification (must be enabled)
3. Scroll down → App Passwords
4. Select "Mail" → Generate
5. Copy the 16-character password into config.py

### 4. Run the app
```bash
streamlit run app.py
```
Opens at http://localhost:8501

---

## How to Use

### Real-time mode
1. Upload one or more invoice PDFs
2. Select "Real-time API"
3. Click "Process Invoices"
4. Results appear instantly on screen
5. Download Excel directly from the page
6. Optionally send via email too

### Batch mode
1. Upload one or more invoice PDFs
2. Select "Batch API"
3. Click "Process Invoices"
4. Job is submitted in background
5. Status updates every 2 minutes on screen
6. When complete: Excel is emailed automatically with cost summary
7. You can close the browser — polling continues in background thread

---

## Cost Estimates (Claude Sonnet 4.6)

| Mode | ~Cost per 14-doc job |
|---|---|
| Real-time | ~$0.10 |
| Batch API | ~$0.05 |

Actual cost is shown in the UI (real-time) or email (batch).

---

## Notes for MVP

- No database — session state is in-memory only (lost on page refresh for real-time)
- Batch polling runs in a background thread — survives page interactions but not server restarts
- Recipient email is hardcoded in config.py
- All extracted values are as-printed in the document; missing fields show as N/A
