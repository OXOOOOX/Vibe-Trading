from __future__ import annotations

from fastapi.testclient import TestClient

import api_server
from src.portfolio.monitoring.service import MonitoringService
from src.portfolio.monitoring.store import MonitoringStore
from src.usage import UsageRecorder, UsageStore


def test_monitor_usage_global_job_and_event_endpoints(monkeypatch, tmp_path) -> None:
    monitor_store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    usage_store = UsageStore(tmp_path / "sessions.db")
    service = MonitoringService.__new__(MonitoringService)
    service.store = monitor_store
    service.usage_store = usage_store

    job = monitor_store.create_planner_job(
        symbols=["600000.SH"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="report_ready",
    )
    recorder = UsageRecorder(
        usage_store,
        "monitor_job",
        str(job["job_id"]),
        attempt_id="600000.SH:1",
    )
    recorder.record_llm(
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        provider="deepseek",
        model="deepseek-v4-pro",
        status="ok",
        elapsed_ms=10,
    )
    recorder.record_resource(
        provider="eastmoney",
        category="market",
        status="ok",
        elapsed_ms=12,
        cache_mode="network",
        query={"code": "600000.SH", "interval": "1m"},
    )

    monkeypatch.setattr(api_server, "_portfolio_monitoring_service", service)
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    global_response = client.get("/portfolio/monitoring/usage?period=today")
    assert global_response.status_code == 200
    global_payload = global_response.json()
    assert global_payload["period"] == "today"
    assert global_payload["scope_count"] == 1
    assert global_payload["session"]["tokens"]["total_tokens"] == 120
    assert global_payload["session"]["calls"]["external_requests"] == 1
    assert global_payload["recent_jobs"][0]["job_id"] == job["job_id"]

    job_response = client.get(f"/portfolio/monitor-planner-jobs/{job['job_id']}/usage")
    assert job_response.status_code == 200
    assert job_response.json()["job"]["activation_mode"] == "autonomous"
    assert job_response.json()["session"]["cost"]["priced_calls"] == 1

    events_response = client.get(
        f"/portfolio/monitor-planner-jobs/{job['job_id']}/usage/events"
        "?kind=resource_call&category=market"
    )
    assert events_response.status_code == 200
    assert [item["provider"] for item in events_response.json()["items"]] == ["eastmoney"]

    assert client.get("/portfolio/monitoring/usage/events?cursor=bad").status_code == 400
    assert client.get("/portfolio/monitor-planner-jobs/missing/usage").status_code == 404
