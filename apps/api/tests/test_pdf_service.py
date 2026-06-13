"""Tests for server-side PDF rendering."""

from __future__ import annotations

import pytest

from services.pdf_service import render_pdf_bytes, render_pdf_result


def test_render_pdf_bytes_produces_pdf_header() -> None:
    data = render_pdf_bytes(title="Test Report", content="Hello from EnterpriseGPT")
    assert data[:4] == b"%PDF"


def test_render_pdf_result_empty_content_is_dry_run() -> None:
    result = render_pdf_result(title="Empty", content="   ")
    assert result["__dry_run__"] is True
    assert result["data"]["ok"] is False
