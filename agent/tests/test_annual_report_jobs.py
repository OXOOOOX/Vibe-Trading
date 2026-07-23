from __future__ import annotations

from pathlib import Path

from src.research.annual_report_jobs import (
    AnnualReportBackfillJobService,
    AnnualReportBackfillJobStore,
)


class _OfficialBackfillFixture:
    def backfill_annual_reports(
        self,
        symbol: str,
        *,
        years: list[int],
        force: bool,
        progress_callback,
    ):
        for year in years:
            progress_callback({"stage": "discovering", "year": year})
            progress_callback({"stage": "discovered", "year": year, "provider_id": "fixture"})
            progress_callback({"stage": "downloading", "year": year})
            progress_callback({"stage": "parsing", "year": year})
            progress_callback({"stage": "validating", "year": year, "document_ref": f"doc-{year}"})
            progress_callback({"stage": "completed", "year": year, "document_ref": f"doc-{year}"})
        return {
            "symbol": symbol,
            "status": "completed",
            "refreshed": len(years),
            "failed": 0,
            "document_refs": [f"doc-{year}" for year in years],
            "coverage": {
                "symbol": symbol,
                "requested_years": years,
                "covered_years": years,
                "archived_years": years,
                "analysis_ready_years": years,
                "needs_review_years": [],
                "unusable_years": [],
                "missing_years": [],
                "coverage_ratio": 1.0,
                "analysis_ready_ratio": 1.0,
                "documents_by_year": {},
            },
            "structured": {
                "documents": len(years),
                "validated": len(years),
                "needs_review": 0,
                "metrics": len(years) * 8,
            },
        }


def _service(tmp_path: Path) -> AnnualReportBackfillJobService:
    return AnnualReportBackfillJobService(
        AnnualReportBackfillJobStore(tmp_path / "annual-report-jobs.sqlite3")
    )


def test_annual_report_job_persists_per_year_phases_and_compact_result(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job, deduplicated = service.create_job(
        symbol="000651.SZ",
        years=[2025, 2024],
    )

    result = service.run_job(job["job_id"], _OfficialBackfillFixture())
    replayed = service.store.get(job["job_id"])

    assert deduplicated is False
    assert result["status"] == "completed"
    assert result["progress_pct"] == 100
    assert replayed == result
    assert result["result"]["coverage"]["analysis_ready_years"] == [2025, 2024]
    assert "results" not in result["result"]["structured"]
    for item in result["year_progress"]:
        assert item["status"] == "completed"
        assert set(item["phases"].values()) == {"completed"}
        assert item["document_ref"] == f"doc-{item['year']}"


def test_annual_report_job_deduplicates_only_active_identical_requests(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first, first_deduplicated = service.create_job(
        symbol="000651.SZ",
        years=[2025, 2024],
    )
    duplicate, duplicate_deduplicated = service.create_job(
        symbol="000651.SZ",
        years=[2024, 2025],
    )

    assert first_deduplicated is False
    assert duplicate_deduplicated is True
    assert duplicate["job_id"] == first["job_id"]

    service.run_job(first["job_id"], _OfficialBackfillFixture())
    next_job, next_deduplicated = service.create_job(
        symbol="000651.SZ",
        years=[2025, 2024],
    )
    assert next_deduplicated is False
    assert next_job["job_id"] != first["job_id"]


def test_job_store_marks_abandoned_running_job_interrupted_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "annual-report-jobs.sqlite3"
    service = AnnualReportBackfillJobService(AnnualReportBackfillJobStore(path))
    job, _ = service.create_job(symbol="000651.SZ", years=[2025])
    service.store.update(job["job_id"], lambda record: record.update({"status": "running"}))

    reopened = AnnualReportBackfillJobStore(path)
    recovered = reopened.get(job["job_id"])

    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["error"] == "service_restarted"
