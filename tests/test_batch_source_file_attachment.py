"""
Regression tests for persisting uploaded source files across the
submit_batch() -> retrieve_results() gap so the completion email can attach
the original file(s) the user uploaded, not just the Excel/Tally output.

submit_batch() and retrieve_results() run in separate background threads,
potentially far apart in time (Batch API turnaround) — the in-memory
Streamlit UploadedFile handed to submit_batch() is gone by retrieval time,
so the original bytes have to round-trip through disk (see
batch_processor._save_source_files / _load_source_files).
"""
from types import SimpleNamespace
from unittest.mock import patch

import batch_processor as bp
from test_retrieve_results_resilience import _fake_result, _FakeClient, _ITEM_JSON


class _FakeUploadedFile:
    """Minimal stand-in for a Streamlit UploadedFile."""
    def __init__(self, name, content):
        self.name = name
        self._content = content

    def getvalue(self):
        return self._content


def test_save_and_load_source_files_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)

    sources = [_FakeUploadedFile("invoice.pdf", b"%PDF-one")]
    bp._save_source_files("job_a", sources)

    loaded = bp._load_source_files("job_a")

    assert len(loaded) == 1
    assert loaded[0]["filename"] == "invoice.pdf"
    assert loaded[0]["content"] == b"%PDF-one"


def test_camera_capture_sources_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)

    sources = [{"images": [b"jpg-bytes"], "name": "Scanned pages"}]
    bp._save_source_files("job_b", sources)

    assert bp._load_source_files("job_b") == []


def test_load_source_files_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)

    assert bp._load_source_files("never_submitted") == []


def test_cleanup_batch_files_removes_sources_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)

    bp._save_source_files("job_c", [_FakeUploadedFile("a.pdf", b"aaa")])
    assert bp._sources_dir("job_c").exists()

    bp.cleanup_batch_files("job_c")

    assert not bp._sources_dir("job_c").exists()


def test_retrieve_results_passes_persisted_source_files_to_send_email(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "LOG_DIR", tmp_path)
    bp._save_source_files("job_d", [_FakeUploadedFile("invoice.pdf", b"%PDF-content")])

    captured = {}

    def fake_send_email(**kwargs):
        captured.update(kwargs)
        return True, "sent"

    fake_results = [_fake_result("invoice_run_1_t1_i0", 500, 100, _ITEM_JSON)]
    with patch("batch_processor.send_email", side_effect=fake_send_email):
        result = bp.retrieve_results(
            job_id="job_d", batch_ids=["batch1"], file_count=1,
            client=_FakeClient(fake_results), user_email="test@example.com", total_pages=1,
        )

    assert result["success"] is True
    assert captured["source_files"] == [{"filename": "invoice.pdf", "content": b"%PDF-content"}]
