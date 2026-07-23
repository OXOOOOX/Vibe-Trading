from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
from src.channels.bus.events import DeliveryReceipt


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


def test_run_report_artifact_is_inline_until_download_is_explicit(tmp_path, monkeypatch) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run_with_report"
    run_dir.mkdir(parents=True)
    report = run_dir / "研究报告.md"
    report.write_text("# 报告正文", encoding="utf-8")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)

    inline = _client().get("/runs/run_with_report/report-artifact")
    download = _client().get("/runs/run_with_report/report-artifact?download=1")

    assert inline.status_code == 200
    assert inline.headers["content-disposition"].startswith("inline;")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")


def test_report_center_sends_selected_run_markdown_to_bound_feishu_once(
    tmp_path, monkeypatch
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run_to_send"
    run_dir.mkdir(parents=True)
    report = run_dir / "飞书研究报告.md"
    report.write_text("# 飞书研究报告", encoding="utf-8")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)

    target = {
        "target_id": "target-1",
        "channel": "feishu",
        "chat_id": "ou_test",
        "status": "active",
    }
    monkeypatch.setattr(
        api_server,
        "_get_portfolio_monitoring_service",
        lambda: SimpleNamespace(store=SimpleNamespace(
            list_targets=lambda: [target],
            get_default_delivery_target_id=lambda: None,
        )),
    )
    delivered = []

    class Manager:
        async def send_direct(self, message):
            delivered.append(message)
            return DeliveryReceipt(
                provider="feishu",
                remote_message_id="om-report-center",
                provider_request_id="req-1",
                accepted_at="2026-07-18T12:00:00+08:00",
            )

    async def start_runtime():
        return SimpleNamespace(manager=Manager())

    monkeypatch.setattr(api_server, "_start_channel_runtime", start_runtime)

    response = _client().post(
        "/reports/send-to-feishu",
        json={
            "source": "run",
            "report_id": "run_to_send",
            "artifact_id": "markdown",
        },
    )

    assert response.status_code == 200
    assert response.json()["remote_message_id"] == "om-report-center"
    assert response.json()["filename"] == report.name
    assert len(delivered) == 1
    assert delivered[0].channel == "feishu"
    assert delivered[0].chat_id == "ou_test"
    assert delivered[0].media == [str(report)]
