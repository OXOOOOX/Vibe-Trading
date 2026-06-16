"""Chat response PDF endpoint tests."""

from __future__ import annotations

import sys
from pathlib import Path
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


def test_generate_response_pdf_falls_back_when_native_library_is_unavailable(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    monkeypatch.setattr(api_server, "_render_pdf_chromium", lambda document: (_ for _ in ()).throw(OSError("missing")))
    monkeypatch.setattr(api_server, "_render_pdf_reportlab", lambda title, content: b"%PDF-fallback")
    client = TestClient(api_server.app)

    response = client.post("/reports/pdf", json={"title": "Report", "content": "Summary"})

    assert response.status_code == 200
    assert response.content == b"%PDF-fallback"


def test_generate_response_pdf_uses_chromium_before_reportlab(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    monkeypatch.setattr(api_server, "_render_pdf_chromium", lambda document: b"%PDF-chromium")
    monkeypatch.setattr(api_server, "_render_pdf_reportlab", lambda title, content: b"%PDF-reportlab")
    client = TestClient(api_server.app)

    response = client.post("/reports/pdf", json={"title": "Report", "content": "✅ Summary"})

    assert response.status_code == 200
    assert response.content == b"%PDF-chromium"


def test_reportlab_fallback_embeds_available_cjk_font() -> None:
    pdf = api_server._render_pdf_reportlab(
        "中文报告",
        "# 摘要\n\n这是中文正文。\n\n| 指标 | 数值 |\n|---|---|\n| 收益率 | 12.3% |",
    )

    assert pdf.startswith(b"%PDF-")
    if Path("C:/Windows/Fonts/simhei.ttf").is_file():
        assert b"/FontFile2" in pdf


def test_reportlab_fallback_renders_markdown_table() -> None:
    pdf = api_server._render_pdf_reportlab(
        "Markdown Report",
        "# Summary\n\n**Important** result.\n\n| Metric | Value |\n|---|---|\n| Sharpe | 1.25 |",
    )

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000


def test_reportlab_fallback_replaces_emoji_with_text_badges() -> None:
    pdf = api_server._render_pdf_reportlab(
        "Signals",
        "✅ 看多\n❌ 风险\n⚠️ 谨慎\n🎯 目标\n📈 上行\n🧪 未知图标",
    )

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000


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
