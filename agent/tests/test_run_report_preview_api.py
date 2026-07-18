from __future__ import annotations

from fastapi.testclient import TestClient

import api_server


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_run_report_preview_returns_the_full_markdown_artifact(tmp_path, monkeypatch) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "20260717_003433_20_ea12c7"
    run_dir.mkdir(parents=True)
    (run_dir / "notes.md").write_text("# Notes\n" + "x" * 5000, encoding="utf-8")
    report = run_dir / "泰晶科技_603738_深度研究报告_20260717.md"
    report.write_text("# 泰晶科技（603738.SH）深度研究报告\n\n## 核心结论\n完整正文", encoding="utf-8")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)

    response = _client().get("/runs/20260717_003433_20_ea12c7/report-preview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "泰晶科技（603738.SH）深度研究报告"
    assert payload["filename"] == report.name
    assert payload["relative_path"] == f"agent/runs/20260717_003433_20_ea12c7/{report.name}"
    assert payload["content"].endswith("完整正文")
    assert payload["source"] == "run_artifact"


def test_run_report_preview_rejects_an_unsafe_run_id() -> None:
    response = _client().get("/runs/foo.bar/report-preview")

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid run_id"


def test_run_report_preview_reports_a_missing_markdown_artifact(tmp_path, monkeypatch) -> None:
    runs_dir = tmp_path / "runs"
    (runs_dir / "run_without_report").mkdir(parents=True)
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)

    response = _client().get("/runs/run_without_report/report-preview")

    assert response.status_code == 404
    assert response.json()["detail"] == "Markdown report not found for this run"
