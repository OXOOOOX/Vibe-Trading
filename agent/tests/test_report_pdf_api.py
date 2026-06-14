"""Chat response PDF endpoint tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server


class _FakeHTML:
    last_string = ""

    def __init__(self, *, string: str):
        self.string = string
        type(self).last_string = string

    def write_pdf(self) -> bytes:
        return b"%PDF-fake"


def test_generate_response_pdf_returns_download(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_FakeHTML))
    client = TestClient(api_server.app)

    response = client.post(
        "/reports/pdf",
        json={"title": "Portfolio Summary", "content": "# Result\n\n| A | B |\n|---|---|\n| 1 | 2 |"},
    )

    assert response.status_code == 200
    assert response.content == b"%PDF-fake"
    assert response.headers["content-type"] == "application/pdf"
    assert "Portfolio_Summary.pdf" in response.headers["content-disposition"]


def test_generate_response_pdf_reports_native_library_failure(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    client = TestClient(api_server.app)

    response = client.post("/reports/pdf", json={"title": "Report", "content": "Summary"})

    assert response.status_code == 501


def test_generate_response_pdf_preserves_colored_emoji_cues(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=_FakeHTML))
    client = TestClient(api_server.app)

    response = client.post(
        "/reports/pdf",
        json={"title": "Signals", "content": "✅ 看多\n❌ 风险\n⚠️ 谨慎\n🎯 目标"},
    )

    assert response.status_code == 200
    assert '<span class="emoji emoji-green">✅</span>' in _FakeHTML.last_string
    assert '<span class="emoji emoji-red">❌</span>' in _FakeHTML.last_string
    assert '<span class="emoji emoji-yellow">⚠️</span>' in _FakeHTML.last_string
    assert '<span class="emoji">🎯</span>' in _FakeHTML.last_string
    assert '"Segoe UI Emoji", "Noto Color Emoji"' in _FakeHTML.last_string
