"""
Regression test for the Tally-generation isolation fix: a bug in Tally file
generation (newer, less battle-tested than Excel creation) must not prevent
the Excel/email — the older, more reliable deliverable — from going out.
"""
from types import SimpleNamespace
from unittest.mock import patch

import batch_processor as bp


def _fake_result(custom_id, in_tok, out_tok, text):
    message = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        content=[SimpleNamespace(text=text)],
        stop_reason="end_turn",
    )
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message))


class _FakeClient:
    def __init__(self, results):
        self.messages = SimpleNamespace(batches=SimpleNamespace(results=lambda bid: iter(results)))


_ITEM_JSON = (
    '[{"invoice_no":"INV-001","party_name":"Acme Traders","invoice_date":"2026-01-15",'
    '"gstin":"","hsn_code":"","item_description":"Rice","quantity":1,"rate":5000,'
    '"taxable_value":5000,"cgst":0,"sgst":0,"igst":0,"total_value":5000}]'
)


def _run_retrieve_results(send_email_impl):
    fake_results = [_fake_result("invoice_run_1_t1_i0", 500, 100, _ITEM_JSON)]
    with patch("batch_processor.send_email", side_effect=send_email_impl):
        return bp.retrieve_results(
            job_id="test_job", batch_ids=["batch1"], file_count=1,
            client=_FakeClient(fake_results), user_email="test@example.com", total_pages=1,
        )


def test_tally_generation_failure_does_not_block_email():
    captured = {}

    def fake_send_email(**kwargs):
        captured.update(kwargs)
        return True, "sent"

    with patch("batch_processor.create_tally_xml", side_effect=RuntimeError("boom")):
        result = _run_retrieve_results(fake_send_email)

    assert result["success"] is True
    assert result["email_sent"] is True
    assert result["tally_erp9_bytes"] is None
    assert result["tally_prime_bytes"] is None
    assert result["tally_erp9_masters_bytes"] is None
    assert "Tally file generation failed" in result["error"]
    assert captured.get("excel_bytes")
    assert captured.get("tally_generation_error")


def test_ledger_masters_failure_does_not_block_email_either():
    captured = {}

    def fake_send_email(**kwargs):
        captured.update(kwargs)
        return True, "sent"

    with patch("batch_processor.create_tally_ledger_masters_xml", side_effect=RuntimeError("boom")):
        result = _run_retrieve_results(fake_send_email)

    assert result["success"] is True
    assert result["email_sent"] is True
    assert captured.get("excel_bytes")


def test_normal_path_still_produces_tally_files():
    captured = {}

    def fake_send_email(**kwargs):
        captured.update(kwargs)
        return True, "sent"

    result = _run_retrieve_results(fake_send_email)

    assert result["success"] is True
    assert result["tally_erp9_bytes"] is not None
    assert result["tally_erp9_masters_bytes"] is not None
    assert result["error"] is None
    assert captured.get("tally_generation_error") is None
