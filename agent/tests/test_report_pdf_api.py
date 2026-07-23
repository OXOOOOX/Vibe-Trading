"""Chat response PDF endpoint tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pypdfium2 as pdfium

import api_server


class _FakeHTML:
    last_string = ""

    def __init__(self, *, string: str):
        self.string = string
        type(self).last_string = string

    def write_pdf(self) -> bytes:
        return b"%PDF-fake"


def test_reportlab_glyph_normalization_uses_visible_cjk_currency_symbol() -> None:
    assert api_server._normalize_reportlab_glyphs("¥37.80\u2011test") == "￥37.80-test"


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


def test_reportlab_footer_contains_subject_date_revision_and_page_number() -> None:
    pdf = api_server._render_pdf_reportlab(
        "格力电器（000651.SZ）穿透式深度研究",
        "> - 报告版本：第 3 版\n"
        "> - 数据更新至：2026-07-20 15:00\n\n"
        + ("研究正文用于分页验证。\n\n" * 260),
    )
    reader = pdfium.PdfDocument(pdf)

    assert len(reader) >= 2
    for page_number, page in enumerate(reader, start=1):
        text_page = page.get_textpage()
        text = text_page.get_text_range()
        assert "000651.SZ" in text
        assert "2026-07-20" in text
        assert str(page_number) in text


def test_reportlab_fallback_renders_markdown_table() -> None:
    pdf = api_server._render_pdf_reportlab(
        "Markdown Report",
        "# Summary\n\n**Important** result.\n\n| Metric | Value |\n|---|---|\n| Sharpe | 1.25 |",
    )

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000


def test_reportlab_fallback_renders_colored_condition_table() -> None:
    pdf = api_server._render_pdf_reportlab(
        "组合晨会",
        "# 条件建议\n\n"
        "| 优先级 | 标的 | 触发条件 | 建议响应 |\n"
        "|---|---|---|---|\n"
        "| 🔴 高 | 格力电器（000651.SZ） | 跌破37.05（成本线） | 重新评估持仓逻辑 |\n"
        "| 🔵 常规 | 招商银行（600036.SH） | 放量突破38.00 | 评估加仓机会 |",
    )

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000


def test_holding_penetration_table_gives_summary_nearly_half_the_page() -> None:
    widths = api_server._reportlab_table_width_shares(
        ["成分", "权重", "入选原因", "研究状态", "可用摘要"]
    )

    assert sum(widths) == 1.0
    assert widths == [0.16, 0.08, 0.14, 0.14, 0.48]
    assert widths[-1] > sum(widths[1:4])


def test_condition_table_blank_targets_become_one_rowspan() -> None:
    body = (
        "<table><thead><tr>"
        "<th>优先级</th><th>标的</th><th>触发条件</th><th>建议响应</th>"
        "</tr></thead><tbody>"
        "<tr><td>🔴 高</td><td>格力电器（000651.SZ）</td><td>跌破37.05</td><td>重新评估</td></tr>"
        "<tr><td>🔵 常规</td><td></td><td>突破38.00</td><td>评估加仓</td></tr>"
        "</tbody></table>"
    )

    merged = api_server._merge_grouped_table_cells(body)

    assert '<td rowspan="2">格力电器（000651.SZ）</td>' in merged
    assert merged.count("格力电器（000651.SZ）") == 1


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
