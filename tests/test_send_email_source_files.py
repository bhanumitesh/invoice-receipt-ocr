"""
Regression tests for attaching the original uploaded file(s) to the
completion email alongside the Excel/Tally output.
"""
import base64
from unittest.mock import patch

import utils


def _send(source_files, **overrides):
    captured = {}

    def fake_send(params):
        captured.update(params)
        return {"id": "email_123"}

    kwargs = dict(
        excel_bytes=b"fake-excel-bytes",
        cost=None,
        mode="Batch API",
        file_count=1,
        item_count=1,
        user_email="user@example.com",
        source_files=source_files,
    )
    kwargs.update(overrides)

    with patch("resend.Emails.send", side_effect=fake_send):
        ok, msg = utils.send_email(**kwargs)

    return ok, msg, captured


def test_source_file_is_attached():
    source_files = [{"filename": "invoice.pdf", "content": b"%PDF-fake-content"}]

    ok, msg, params = _send(source_files)

    assert ok is True
    names = [a["filename"] for a in params["attachments"]]
    assert "invoice.pdf" in names
    attached = next(a for a in params["attachments"] if a["filename"] == "invoice.pdf")
    assert base64.b64decode(attached["content"]) == b"%PDF-fake-content"
    assert "attached for reference" in params["text"]


def test_multiple_source_files_all_attached():
    source_files = [
        {"filename": "a.pdf", "content": b"aaa"},
        {"filename": "b.pdf", "content": b"bbb"},
    ]

    ok, _, params = _send(source_files)

    names = [a["filename"] for a in params["attachments"]]
    assert "a.pdf" in names and "b.pdf" in names


def test_oversized_source_files_are_omitted_not_fatal():
    # One "file" alone larger than the attachment budget.
    huge = b"x" * (utils.MAX_EMAIL_ATTACHMENT_BYTES + 1)
    source_files = [{"filename": "huge.pdf", "content": huge}]

    ok, _, params = _send(source_files)

    assert ok is True
    names = [a["filename"] for a in params["attachments"]]
    assert "huge.pdf" not in names
    # The core deliverable (Excel) must still go out.
    assert any(a["content"] for a in params["attachments"])
    assert "Not Attached" in params["text"]


def test_no_source_files_behaves_as_before():
    ok, _, params = _send(source_files=None)

    assert ok is True
    assert "attached for reference" not in params["text"]
    assert "Not Attached" not in params["text"]
