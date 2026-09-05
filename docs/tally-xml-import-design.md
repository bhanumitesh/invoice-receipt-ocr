# Tally import automation — discussion notes

Status: **discussion only, nothing implemented yet.** Captured on 2026-08-23 so
the reasoning and open questions aren't lost. Goal: reduce/eliminate the manual
step where accountants re-key each extracted invoice into Tally by hand, by
generating a Tally-importable file from the same data that already produces the
Excel register.

---

## 1. The accounting logic (verified)

Standard double-entry for a GST purchase, confirmed against a worked example
from margbooks.com and TallyHelp's own GST-purchase guide:

```
Purchase A/c         Dr   ₹10,000
Input CGST A/c        Dr    ₹900
Input SGST A/c         Dr    ₹900
   To Supplier A/c            Cr  ₹11,800
```

- **Debit side = multiple lines, never the party**: the expense/purchase
  ledger for the taxable value, plus CGST+SGST (intrastate) or IGST
  (interstate) for the tax — 2 or 3 debit lines depending on the transaction.
- **Credit side = exactly one line, always the party**: the vendor/supplier
  ledger, for the full total (taxable + tax). The party is credited (a
  liability increase — money now owed) not debited; debiting the party
  belongs to a *payment* voucher (settling the bill later), a separate
  transaction from the purchase entry itself.

This matches the logic already implemented in `_build_voucher_xml()` — verified
by running it against real numbers (CGST+SGST and IGST cases) and confirming
the voucher balances to zero either way.

## 2. Real Tally examples reviewed

Two real journal entries from the user's own Tally company ("S R Roadline
Firm 23-24") were reviewed against two rows of the app's own generated Excel
output. Findings:

**Confirms the debit/credit direction.** Journal 1: Dr "Diesel and Petrol"
₹25,979.00 / Cr "Sri Venkateswara Filling Station" ₹25,979.00 — matches the
Excel row for that vendor (taxable ₹25,979.40, no CGST/SGST/IGST).

**Voucher type is "Journal," not "Purchase."** Both real examples use a
Journal voucher, not the `VCHTYPE="Purchase"` the generator currently
hardcodes. **Decision: switch the generator to `VCHTYPE="Journal"`** to match
observed real-world usage.

**No GST tax-ledger split in either real example — and for fuel, that's
correct, not a gap.** Verified: petrol and diesel are outside India's GST
framework entirely (still taxed via central excise + state VAT, no input tax
credit claimable) — confirmed current as of 2026, including the March 2026
excise cut. So a diesel purchase legitimately has nothing to split into
CGST/SGST/IGST; one flat Dr/Cr is correct.

The Auto Tyre entry is less clear: its Excel row shows a real GSTIN
(`27AAZPS9226P1ZB`, meaning a GST-registered dealer) and tires are normally
GST-taxable, but the row's CGST/SGST/IGST columns are blank (taxable = total,
no tax added) — and the real Tally entry for Auto Tyre also has no tax split,
consistent with the Excel. **Leading theory: Auto Tyre may be registered under
the GST *Composition Scheme*** — composition dealers have a real GSTIN but are
legally barred from charging/showing GST separately on their invoices, and the
buyer can't claim input credit on such purchases either; their invoices
typically carry a "Composition Taxable Person" note instead of an itemized tax
breakdown. That would explain both the blank Excel columns and the untaxed
real Tally entry in one consistent story — but this needs confirming against
the actual source invoice before treating it as settled (see Open Questions).

**The debit ledger is not a single generic "Purchase Account" — it varies by
expense category.** "Repair and Maintenance" for the tire shop, "Diesel and
Petrol" for the fuel station. The generator currently uses one flat
`TALLY_DEFAULT_LEDGER` config value (default "Purchase Account") for every
invoice regardless of vendor — real usage clearly varies this by vendor/nature
of purchase. Whether this is a predictable/mappable rule (e.g. "this vendor
always maps to this ledger") or an inherent case-by-case accountant judgment
call is an open question the user is checking on.

## 3. Tally XML import format findings

From the sample-XML/XML-integration research (previous round) plus this
round's ledger-master research:

- **`TALLYMESSAGE`-per-voucher wrapping is correct** — matches Tally's own
  official docs for the `Import Data` / `Vouchers` request type.
- **Dropping `SVCURRENTCOMPANY` looks defensible for this request type** —
  Tally's own minimal official example for `Import Data`/`Vouchers` omits
  `STATICVARIABLES`/`SVCURRENTCOMPANY` entirely. Practical implication: import
  lands in whichever company is currently open in Tally at import time — a
  workflow habit to document, not something the XML forces.
- **`ISINVOICE` placement is questionable.** Available documentation suggests
  this is conventionally a *voucher-level* flag (As-Invoice vs As-Voucher
  display mode), not a per-ledger-entry tag — but the current WIP places it
  inside every `ALLLEDGERENTRIES.LIST` block instead. Could be silently
  ignored there, could cause a problem; only a real Tally test can confirm.
- **No bill-wise allocation (`BILLALLOCATIONS.LIST`) implemented anywhere** —
  neither committed code nor the WIP. Real AP workflows use bill-wise "New
  Ref" details (tagged with the invoice number) on the party credit line so
  Tally tracks per-invoice outstanding amounts rather than one lump running
  balance per vendor. Without it, an accountant still can't tell which portion
  of a vendor's balance corresponds to which invoice when it comes time to pay
  — a real limit on how much manual reconciliation this actually removes.
- **Masters must pre-exist before voucher import — confirmed from multiple
  sources.** A plain voucher import only *references* ledgers by name; it
  doesn't create them. Every referenced ledger (party, expense category, tax
  ledgers) must already exist in the target Tally company, or Tally will
  likely reject the voucher (or the whole batch, depending on settings).

## 4. Ledger master import — is it possible? Yes.

Confirmed via Tally's own developer docs: masters and vouchers are imported
as **separate request types** — `<ID>All Masters</ID>` (or the
`REQUESTDESC`/`REPORTNAME` equivalent) for masters, `<ID>Vouchers</ID>` for
vouchers, not combined in one file. Ledger creation XML is simple:

```xml
<TALLYMESSAGE>
  <LEDGER Action="Create">
    <NAME>Repair and Maintenance</NAME>
    <PARENT>Indirect Expenses</PARENT>
    <OPENINGBALANCE>0</OPENINGBALANCE>
  </LEDGER>
</TALLYMESSAGE>
```

**Proposed two-step import workflow:**
1. Generate a **Masters XML** — any party/vendor ledgers and expense-category
   ledgers not already in the target Tally company, each under the right
   `PARENT` group (e.g. "Sundry Creditors" for vendors, "Indirect Expenses"
   for expense categories, "Duties & Taxes" for CGST/SGST/IGST when
   applicable). Accountant imports this first.
2. Then import the existing **Vouchers XML** second, now that everything it
   references exists.

This should be safe to re-run across batches: Tally has documented duplicate
handling (`IMPORTDUPS` — combine/ignore/modify) so re-importing a Masters file
that includes vendors already created in a previous batch shouldn't error or
duplicate them, as long as it's configured to skip existing ones — important
since the same vendors will keep recurring across future invoice batches.

## 5. Decisions so far

- Use `VCHTYPE="Journal"`, not `"Purchase"`, matching real observed usage.
- Pursue the two-file (Masters, then Vouchers) import workflow rather than
  trying to bundle ledger creation and voucher creation into one file.

## 6. Open questions

1. **Auto Tyre's missing GST** — still unresolved. Need the actual source
   invoice PDF/image to confirm whether it's a Composition Scheme dealer (tax
   legitimately not shown) or an extraction miss on our end.
2. **Expense-category-per-vendor** — still deferred. Confirmed fine to keep
   the single flat "Purchase Account" expense ledger for now; revisit if/when
   a predictable vendor→ledger mapping emerges.
3. ~~`ISINVOICE` placement/value~~ — **resolved.** Real Tally import test
   (see §7) confirmed voucher-level `ISINVOICE=No` imports cleanly.
4. ~~Bill-wise allocation~~ — **implemented** (see §8).
5. **Ledger `PARENT` group assignment** — still using `Sundry Creditors` /
   `Indirect Expenses` / `Duties & Taxes` as implemented; no real-world
   problem surfaced with these yet.

## 7. Follow-up: real Tally test + fixes applied (2026-08-24)

**The two-file import was tested against a real Tally company and worked
cleanly** — ledger masters imported first, then the vouchers file, no
errors. This resolves open question #3 above and validates the overall
two-step design.

Three additional issues were identified and fixed in the same pass:

- **Vendor-ledger de-duplication now normalizes names.** `create_tally_
  ledger_masters_xml()`'s original exact-string-match dedup would have
  treated e.g. `"Sri Venkateswara Filling Station"` and `"SRI VENKATESWARA
  FILLING STATION"` as two different vendors — a likely outcome given LLM
  extraction isn't guaranteed byte-identical across invoices from the same
  vendor. Fixed with a new `canonicalize_party_names()` in `utils.py`,
  applied once to a copy of the batch's items before *both* Tally file
  generators run, so the masters file and the vouchers file always agree on
  one canonical name per vendor. The Excel register is unaffected — it still
  reflects exactly what was extracted, uncanonicalized.
- **`REMOTEID`/`GUID` no longer collide across vendors.** They previously
  used the invoice number alone, which isn't globally unique (two different
  vendors can each send an "INV-001") — a collision would make Tally treat
  an unrelated invoice as an alteration of the first one. Now built from a
  hash of vendor + invoice number + date, unique per real invoice and stable
  across re-imports of the *same* invoice (the same invoice always
  reproduces the same key — confirmed by test).
- **Zero/negative-total items (credit notes, corrections) no longer vanish
  silently.** Both Tally generators still exclude anything with
  `total_value <= 0` — auto-generating a correct reversing entry was judged
  too risky without more confidence in how reliably the extraction
  identifies genuine credit notes vs. an extraction error — but a new
  `tally_excluded_items()` now surfaces exactly what was excluded (vendor,
  invoice number, amount) as an explicit warning section in the results
  email, so nothing is lost without a trace. Checked the two real
  `Invoice_Register_*.xlsx` files already on hand from past runs: zero rows
  with `total_value <= 0` in either (small sample, so this hasn't been a
  problem in practice yet, not proof it won't come up).

**Deferred, not fixed:** cross-batch re-import protection (the same invoice
processed again in a later batch, weeks apart). Current reasoning: since
`REMOTEID` is now stable per real invoice, and the underlying extracted data
for the same source document should be the same each time, Tally's
create-or-alter behavior on a repeat `REMOTEID` should just re-write the same
values — no double-booking. The residual risk is narrower than originally
framed: not "will it double-count," but "could a second import *silently
overwrite* a correct voucher with slightly different numbers" if extraction
drifts between the two runs (a prompt/model change between processing dates,
or ordinary LLM non-determinism on a borderline field). Low-probability,
acceptable to leave for a later pass — would need the app to remember which
invoices it's already sent to Tally, across sessions, to close fully.

**Bill-wise allocation clarified with a concrete example and agreed to build
as a follow-up** (not yet implemented). Worth restating why it matters: today,
three invoices from the same vendor each post as their own *voucher*
(transaction), correctly, against the *same* single vendor *ledger* (account)
— that's normal double-entry, not a bug, and multiple vouchers referencing one
shared ledger is exactly right. But without bill-wise allocation, that shared
ledger's outstanding balance shows as one lump number with no way, from inside
Tally, to tell which portion belongs to which invoice — so when it's time to
pay one specific bill, the accountant still has to reconcile against the
Excel/email outside of Tally. Adding `BILLALLOCATIONS.LIST` (`BILLTYPE="New
Ref"`, `NAME`=invoice number, matching `AMOUNT`) on the party's credit line
gives each invoice its own trackable "bill" inside Tally, letting a later
payment be recorded against a specific one (`BILLTYPE="Agst Ref"`) — this is
the piece that actually removes the "which invoice was this for" manual
reconciliation step.

The most reliable way to resolve open question #5 (`PARENT` group choices)
definitively, if it ever needs it (per Tally's own guidance): record one
realistic voucher by hand in TallyPrime exactly as wanted, then export just
that one voucher to XML and diff it against what this generator produces.

## 8. Bill-wise allocation — implemented (2026-09-06)

`_build_voucher_xml()`'s party credit line now carries a
`<BILLALLOCATIONS.LIST>`:

```xml
<ALLLEDGERENTRIES.LIST>
    <LEDGERNAME>Acme Traders</LEDGERNAME>
    <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
    <AMOUNT>5250.00</AMOUNT>
    <BILLALLOCATIONS.LIST>
        <NAME>INV-001</NAME>
        <BILLTYPE>New Ref</BILLTYPE>
        <AMOUNT>5250.00</AMOUNT>
    </BILLALLOCATIONS.LIST>
    <GODOWNENTRIES.LIST/>
    <CATEGORYENTRIES.LIST/>
</ALLLEDGERENTRIES.LIST>
```

`BILLTYPE` is always `"New Ref"` — this app only records purchases, never
payments, so every bill it creates is a fresh one, not a settlement against
an existing bill (`"Agst Ref"`, which would come from a separate payment-
voucher feature this app doesn't have).

**Bill name uniqueness**: per Tally's documented behavior, a bill name only
needs to be unique *within one party's ledger*, not globally — so using the
invoice number directly is safe even across different vendors sharing an
invoice number. When the invoice number wasn't extracted at all, falls back
to a `{date}-{voucher_key prefix}` reference rather than risking two bills
on the same vendor both named `""`.

Verified with a direct test: two different invoices from the same vendor
produce two distinct bill names (no collision), and the missing-invoice-
number fallback produces a valid, unique reference.

**Updates the cross-batch re-import risk noted in §7.** Previously the
concern was a silent overwrite if the same invoice's extracted data drifted
between two import runs. With bill-wise allocation now creating a `"New
Ref"` bill each time, Tally's own duplicate-bill-name handling becomes the
relevant behavior instead — per Tally's documentation, a bill name is meant
to be unique per party, so a second `"New Ref"` for a name that already
exists may surface as an explicit error rather than a silent overwrite,
which would actually be a better outcome (forces the accountant to notice)
than the risk originally described. This hasn't been verified against a real
re-import yet — worth confirming next time a duplicate scenario comes up.

## Sources consulted

- [How Does A Purchase Journal Entry With GST Affect Input Tax Credit?](https://margbooks.com/blogs/how-does-a-purchase-journal-entry-with-gst-affect-input-tax-credit/)
- [Record Purchases under GST - Local, Interstate, and Fixed Assets](https://help.tallysolutions.com/article/Tally.ERP9/Tax_India/gst/recording_purchases_gst.htm)
- [GST on Petrol and Diesel in 2026](https://razorpay.com/learn/gst-on-petrol/)
- [Sample XML | TallyHelp](https://help.tallysolutions.com/sample-xml/)
- [XML Integration - TallyHelp](https://help.tallysolutions.com/xml-integration/)
- [Case Study 1 - XML Request and Response Formats | TallyHelp](https://help.tallysolutions.com/article/DeveloperReference/integration-capabilities/case_study_1.htm)
- [excel-to-tally-templates: Master-LedgerMaster-xml-tags.xml](https://github.com/ShwetaSoftwares/excel-to-tally-templates/blob/master/Master-LedgerMaster-xml-tags.xml)
- [Integrating with Tally: Getting Data In and Out with TDL and the XML Gateway](https://appycodes.dev/blog/tally-integration-tdl-guide-2026/)
- [Tally XML Import Guide — AccuRaik](https://accuraik.com/tally-xml-import-guide)
- [How to Record Purchases Under GST - Local & Interstate | TallyHelp](https://help.tallysolutions.com/gst-purchases-tally/)
