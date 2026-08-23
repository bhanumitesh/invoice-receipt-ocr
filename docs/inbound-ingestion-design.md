# Automatic email/WhatsApp ingestion — design discussion

Status: **discussion only, nothing implemented**. Captured on 2026-08-23 so the
reasoning isn't lost before any of this gets built.

## Question

Once a user is done uploading/scanning, can invoices be picked up automatically
from email (forwarded invoices) or WhatsApp (photos of paper bills), without the
user opening the app at all?

## The one new architectural piece, common to both channels

`batch_processor.py` / `utils.py` are already UI-agnostic — `submit_batch()`,
`build_file_content_chunks()`, `retrieve_results()` don't import Streamlit, so
the processing engine is ready to be driven by more than one front door.

But both email and WhatsApp ingestion are **webhook-driven** — the provider
(Resend, Meta) POSTs to *our* URL when something arrives — and Streamlit has no
clean way to expose an arbitrary POST endpoint. Unlike the scan-capture feature,
where an all-Streamlit approach worked, there's no equivalent trick here: this
needs a small second service (a lightweight FastAPI app) that just receives the
webhook, pulls the attachment, and calls the same `submit_batch()` already
built. Much smaller than the earlier "separate API + native app" idea (no
client app, no auth flows, just one receiving endpoint) — but it is a second
deployable, not a Streamlit-only change.

**Why a webhook, not a polling loop:** Render's free tier spins the web service
down after 15 minutes of inactivity, and a continuously-running background
worker (e.g. an IMAP-polling loop) requires a paid Starter plan ($7/mo
minimum) — free-tier background threads only live as long as the
request-serving process, which dies when Render spins it down. A webhook works
*with* that model instead: the provider's POST request is itself what wakes
Render back up (~1 minute cold start), which is a non-issue for a
fire-and-forget "you'll get a results email later" flow — the app already
works this way.

## Email route (via Resend Inbound)

- Resend — already used for outbound email in this app — now has a native
  **Inbound** feature (webhook-based receiving), so no new vendor is needed.
- Set up a receiving domain/subdomain in Resend, point MX records at it,
  configure an `email.received` webhook to the new endpoint.
- Resend parses the email and attachments and POSTs structured JSON + metadata;
  attachment bytes are fetched via their Attachments API, then fed straight
  into `submit_batch()`.
- **New piece needed:** sender → registered-user mapping. Today every batch is
  tied to a logged-in, OTP-verified user; an inbound email only gives a "From"
  address, which needs to be matched against the Supabase `users` table
  (reject/quarantine mail from unrecognized addresses, so a stranger can't burn
  credits or Anthropic spend by emailing the ingestion address).

## WhatsApp route (Meta Cloud API)

- Needs a dedicated WhatsApp Business number (not a personal number), a Meta
  Business Manager account, and the same shape of webhook endpoint (Meta does
  a GET handshake to verify the URL, then POSTs incoming messages).
- Full business verification (legal name, tax ID, billing docs) is only
  required past **250 unique recipients per 24 hours** — for a small user base
  this may not be needed at all initially. When it is needed, expect roughly
  2-4 business days for Meta's review.
- Media handling is an extra step: an incoming photo arrives as a media *ID*,
  requiring a second API call to Meta for a short-lived download URL — more
  integration work than an email attachment, but not hard.
- Avoid unofficial/browser-automation WhatsApp libraries — they violate
  WhatsApp's ToS and risk the number getting banned; not something to build a
  real pipeline on.
- Same sender-identity problem as email, but keyed by phone number instead of
  address — same fix (match against a registered number in Supabase).

## Advantages

- Removes the "open the app" step entirely — forward an invoice, or WhatsApp a
  photo of a paper bill, done. WhatsApp especially is close to zero-friction
  since it's already most people's default photo-sharing app — arguably a
  bigger friction win than the scan-capture feature.
- Both channels are naturally async, which matches the existing Batch API +
  emailed-results pipeline — no architectural mismatch.

## Limitations / risks

- Needs a second small deployable (webhook receiver) — modest, but real new
  infrastructure and its own Render service/cost.
- Needs a new trust boundary: sender→user identity mapping and abuse/spam
  protection that doesn't exist today (currently the only gate is OTP login).
- WhatsApp has real external dependencies outside our control — a dedicated
  number, Meta's review process past 250 recipients/day, and their pricing
  model for business-initiated conversations (user-initiated ones — someone
  messaging first — are generally cheaper/free-tier-friendly, which fits this
  use case).
- Email is the lower-risk starting point: no new vendor, no external
  verification wait, reuses infrastructure already in place.

## Recommendation / sequencing

Start with email: build the small webhook-receiver service + sender
identity-mapping for email first, prove out the abuse/credit-guarding logic
there, then extend the same receiver to WhatsApp once that pattern is
validated — rather than building both channels at once.

## Sources consulted

- [Platforms with a real free tier for developers in 2026](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026)
- [Receiving Emails - Resend](https://resend.com/docs/dashboard/receiving/introduction)
- [Resend adds Inbound feature for webhooks-based email receiving and processing](https://alternativeto.net/news/2025/11/resend-adds-inbound-feature-for-webhooks-based-email-receiving-and-processing/)
- [WhatsApp API Prerequisites: Phone, Documents, and Verification](https://www.wati.io/en/blog/whatsapp-api-prerequisites/)
