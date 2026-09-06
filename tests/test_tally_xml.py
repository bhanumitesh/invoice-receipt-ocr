"""
Tests for Tally XML generation — the highest-stakes, most fragile logic in
this app, since it produces financial data an accountant imports directly
into their books. Covers the correctness fixes made this session: vendor-
name canonicalization, REMOTEID/GUID vendor+invoice+date scoping, bill-wise
allocation, and zero/negative-total exclusion — see
docs/tally-xml-import-design.md for the reasoning behind each.
"""
import xml.etree.ElementTree as ET

import utils


def _item(**overrides):
    base = {
        "invoice_no": "INV-001",
        "invoice_date": "2026-01-15",
        "party_name": "Acme Traders",
        "gstin": "27ABCDE1234F1Z5",
        "hsn_code": "1006",
        "description": "Rice bags",
        "quantity": 10,
        "rate": 500,
        "taxable_value": 5000,
        "cgst": 125,
        "sgst": 125,
        "igst": 0,
        "total_value": 5250,
    }
    base.update(overrides)
    return base


# ── canonicalize_party_names ────────────────────────────────────────────────

def test_canonicalize_party_names_merges_case_and_whitespace_variants():
    items = [
        _item(party_name="Sri Venkateswara Filling Station"),
        _item(party_name="  SRI VENKATESWARA  FILLING STATION "),
    ]
    result = utils.canonicalize_party_names(items)
    assert result[0]["party_name"] == result[1]["party_name"]
    assert result[0]["party_name"] == "Sri Venkateswara Filling Station"  # first-seen wins


def test_canonicalize_party_names_leaves_distinct_vendors_alone():
    items = [_item(party_name="Vendor A"), _item(party_name="Vendor B")]
    result = utils.canonicalize_party_names(items)
    assert result[0]["party_name"] != result[1]["party_name"]


def test_canonicalize_party_names_does_not_mutate_input():
    items = [_item(party_name="  Vendor  ")]
    utils.canonicalize_party_names(items)
    assert items[0]["party_name"] == "  Vendor  "


# ── tally_excluded_items ─────────────────────────────────────────────────────

def test_tally_excluded_items_flags_zero_and_negative_totals():
    items = [
        _item(invoice_no="INV-001", total_value=500),
        _item(invoice_no="CN-001", total_value=-200),
        _item(invoice_no="INV-002", total_value=0),
    ]
    excluded = utils.tally_excluded_items(items)
    assert {x["invoice_no"] for x in excluded} == {"CN-001", "INV-002"}


def test_tally_excluded_items_empty_when_all_positive():
    items = [_item(total_value=100), _item(total_value=200)]
    assert utils.tally_excluded_items(items) == []


# ── create_tally_xml: voucher structure ──────────────────────────────────────

def test_voucher_type_is_journal():
    root = ET.fromstring(utils.create_tally_xml([_item()], "erp9"))
    voucher = root.find(".//VOUCHER")
    assert voucher.get("VCHTYPE") == "Journal"
    assert voucher.findtext("VOUCHERTYPENAME") == "Journal"


def test_isinvoice_is_voucher_level_only():
    root = ET.fromstring(utils.create_tally_xml([_item()], "erp9"))
    voucher = root.find(".//VOUCHER")
    assert voucher.findtext("ISINVOICE") == "No"
    assert root.findall(".//ALLLEDGERENTRIES.LIST/ISINVOICE") == []


def test_invoice_mode_fields_absent():
    xml_str = utils.create_tally_xml([_item()], "erp9").decode("utf-8")
    assert "PARTYGSTIN" not in xml_str
    assert "BASICBASEPARTYNAME" not in xml_str
    assert "PERSISTEDVIEW" not in xml_str


def test_voucher_balances_intrastate_cgst_sgst():
    item = _item(taxable_value=5000, cgst=125, sgst=125, igst=0, total_value=5250)
    root = ET.fromstring(utils.create_tally_xml([item], "erp9"))
    entries = root.findall(".//ALLLEDGERENTRIES.LIST")
    debits  = sum(float(e.findtext("AMOUNT")) for e in entries if e.findtext("ISDEEMEDPOSITIVE") == "Yes")
    credits = sum(float(e.findtext("AMOUNT")) for e in entries if e.findtext("ISDEEMEDPOSITIVE") == "No")
    assert debits + credits == 0  # debits negative, credits positive — must net to zero


def test_voucher_balances_interstate_igst():
    item = _item(taxable_value=1000, cgst=0, sgst=0, igst=50, total_value=1050)
    root = ET.fromstring(utils.create_tally_xml([item], "erp9"))
    entries = root.findall(".//ALLLEDGERENTRIES.LIST")
    debits  = sum(float(e.findtext("AMOUNT")) for e in entries if e.findtext("ISDEEMEDPOSITIVE") == "Yes")
    credits = sum(float(e.findtext("AMOUNT")) for e in entries if e.findtext("ISDEEMEDPOSITIVE") == "No")
    assert debits + credits == 0
    ledger_names = [e.findtext("LEDGERNAME") for e in entries]
    assert "IGST" in ledger_names
    assert "CGST" not in ledger_names
    assert "SGST/UTGST" not in ledger_names


def test_no_gst_purchase_has_only_expense_and_party_lines():
    # e.g. petrol/diesel — outside India's GST framework, no tax split
    item = _item(party_name="Fuel Station", gstin="", hsn_code="",
                 taxable_value=1000, cgst=0, sgst=0, igst=0, total_value=1000)
    root = ET.fromstring(utils.create_tally_xml([item], "erp9"))
    assert len(root.findall(".//ALLLEDGERENTRIES.LIST")) == 2


def test_zero_or_negative_total_items_excluded_from_voucher_output():
    items = [_item(invoice_no="INV-001", total_value=500),
             _item(invoice_no="CN-001", total_value=-200)]
    root = ET.fromstring(utils.create_tally_xml(items, "erp9"))
    assert len(root.findall(".//VOUCHER")) == 1


def test_special_characters_are_escaped_and_well_formed():
    item = _item(party_name='Beta & <Co> "Traders"')
    root = ET.fromstring(utils.create_tally_xml([item], "erp9"))  # raises ParseError if malformed
    assert root.find(".//PARTYLEDGERNAME").text == 'Beta & <Co> "Traders"'


def test_erp9_and_prime_both_well_formed_and_journal_type():
    for version in ("erp9", "prime"):
        root = ET.fromstring(utils.create_tally_xml([_item()], version))
        assert root.find(".//VOUCHER").get("VCHTYPE") == "Journal"


# ── REMOTEID / GUID uniqueness and stability ────────────────────────────────

def test_remoteid_differs_across_vendors_sharing_invoice_number():
    items = [_item(party_name="Vendor A", invoice_no="INV-001"),
             _item(party_name="Vendor B", invoice_no="INV-001")]
    root = ET.fromstring(utils.create_tally_xml(items, "erp9"))
    remoteids = [v.get("REMOTEID") for v in root.findall(".//VOUCHER")]
    assert remoteids[0] != remoteids[1]
    assert all(remoteids)


def test_remoteid_is_stable_for_the_same_invoice():
    item = _item(party_name="Vendor A", invoice_no="INV-001", invoice_date="2026-01-15")
    id1 = ET.fromstring(utils.create_tally_xml([item], "erp9")).find(".//VOUCHER").get("REMOTEID")
    id2 = ET.fromstring(utils.create_tally_xml([item], "erp9")).find(".//VOUCHER").get("REMOTEID")
    assert id1 == id2  # re-importing the same invoice must reproduce the same key


def test_guid_matches_remoteid():
    root = ET.fromstring(utils.create_tally_xml([_item()], "erp9"))
    voucher = root.find(".//VOUCHER")
    assert voucher.get("REMOTEID") == voucher.findtext("GUID")


# ── Bill-wise allocation ─────────────────────────────────────────────────────

def test_bill_allocation_present_with_invoice_number():
    root = ET.fromstring(utils.create_tally_xml([_item(invoice_no="INV-001", total_value=5250)], "erp9"))
    bill = root.find(".//BILLALLOCATIONS.LIST")
    assert bill.findtext("NAME") == "INV-001"
    assert bill.findtext("BILLTYPE") == "New Ref"
    assert bill.findtext("AMOUNT") == "5250.00"


def test_bill_names_distinct_for_same_vendor_different_invoices():
    items = [_item(party_name="Acme Traders", invoice_no="INV-001"),
             _item(party_name="Acme Traders", invoice_no="INV-045")]
    root = ET.fromstring(utils.create_tally_xml(items, "erp9"))
    names = [b.findtext("NAME") for b in root.findall(".//BILLALLOCATIONS.LIST")]
    assert len(set(names)) == 2


def test_bill_name_fallback_when_invoice_number_missing():
    root = ET.fromstring(utils.create_tally_xml([_item(invoice_no="")], "erp9"))
    bill_name = root.find(".//BILLALLOCATIONS.LIST").findtext("NAME")
    assert bill_name  # must be non-empty, not risk colliding on ""


# ── create_tally_ledger_masters_xml ──────────────────────────────────────────

def test_masters_dedupes_vendor_across_multiple_invoices():
    items = [_item(party_name="Acme Traders", invoice_no="INV-001"),
             _item(party_name="Acme Traders", invoice_no="INV-045")]
    root = ET.fromstring(utils.create_tally_ledger_masters_xml(items, "erp9"))
    vendor_ledgers = [l for l in root.findall(".//LEDGER") if l.findtext("PARENT") == "Sundry Creditors"]
    assert len(vendor_ledgers) == 1


def test_masters_only_creates_tax_ledgers_actually_used():
    items = [_item(cgst=125, sgst=125, igst=0)]  # intrastate only
    root = ET.fromstring(utils.create_tally_ledger_masters_xml(items, "erp9"))
    names = [l.findtext("NAME") for l in root.findall(".//LEDGER")]
    assert "CGST" in names
    assert "SGST/UTGST" in names
    assert "IGST" not in names


def test_masters_always_includes_default_expense_ledger():
    root = ET.fromstring(utils.create_tally_ledger_masters_xml([_item()], "erp9"))
    names = [l.findtext("NAME") for l in root.findall(".//LEDGER")]
    assert utils.config.TALLY_DEFAULT_LEDGER in names


def test_masters_and_vouchers_agree_on_canonicalized_vendor_name():
    items = [_item(party_name="Sri Venkateswara Filling Station", invoice_no="INV-001"),
             _item(party_name="SRI VENKATESWARA FILLING STATION", invoice_no="INV-002")]
    canon = utils.canonicalize_party_names(items)

    master_root  = ET.fromstring(utils.create_tally_ledger_masters_xml(canon, "erp9"))
    voucher_root = ET.fromstring(utils.create_tally_xml(canon, "erp9"))

    master_names = {l.findtext("NAME") for l in master_root.findall(".//LEDGER")
                     if l.findtext("PARENT") == "Sundry Creditors"}
    voucher_party_names = {v.findtext("PARTYLEDGERNAME") for v in voucher_root.findall(".//VOUCHER")}

    # Every ledger name a voucher references must exist in the masters file,
    # or Tally will reject the voucher import.
    assert voucher_party_names <= master_names
