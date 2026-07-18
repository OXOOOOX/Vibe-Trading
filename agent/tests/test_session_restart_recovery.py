from pathlib import Path

from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus, Session
from src.session.service import SessionService
from src.session.store import SessionStore


def _service(tmp_path: Path) -> SessionService:
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def test_store_lists_persisted_attempts_across_sessions(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    first = store.create_session(Session(title="first"))
    second = store.create_session(Session(title="second"))
    store.create_attempt(Attempt(session_id=first.session_id, status=AttemptStatus.RUNNING))
    store.create_attempt(Attempt(session_id=second.session_id, status=AttemptStatus.COMPLETED))

    assert len(store.list_attempts()) == 2
    assert len(store.list_attempts(first.session_id)) == 1


def test_restart_recovery_closes_orphaned_deep_report_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    session = service.store.create_session(Session(title="deep report"))
    attempt = service.store.create_attempt(
        Attempt(
            session_id=session.session_id,
            status=AttemptStatus.RUNNING,
            metadata={
                "response_mode": "deep_report",
                "report_profile": "equity_deep_research",
            },
        )
    )
    report = service.deep_reports.begin(
        session_id=session.session_id,
        attempt_id=attempt.attempt_id,
        request_content="泰晶科技",
    )

    assert service.recover_interrupted_attempts() == 1

    recovered_attempt = service.store.get_attempt(session.session_id, attempt.attempt_id)
    recovered_report = service.deep_reports.require(report.report_id)
    assert recovered_attempt is not None
    assert recovered_attempt.status == AttemptStatus.FAILED
    assert "用新数据更新" in str(recovered_attempt.error)
    assert recovered_report.status == "failed"
    assert recovered_report.pipeline_state == "technical_failed"
    assert recovered_report.delivery_kind == "diagnostic"
    assert "用新数据更新" in service.deep_reports.read_markdown(report.report_id)
    replies = [
        message
        for message in service.store.get_messages(session.session_id)
        if message.linked_attempt_id == attempt.attempt_id and message.role == "assistant"
    ]
    assert len(replies) == 1

    assert service.recover_interrupted_attempts() == 0
    assert len(service.store.get_messages(session.session_id)) == 1


def test_restart_recovery_preserves_already_published_report(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.store.create_session(Session(title="published"))
    attempt = service.store.create_attempt(
        Attempt(session_id=session.session_id, status=AttemptStatus.RUNNING)
    )
    report = service.deep_reports.begin(
        session_id=session.session_id,
        attempt_id=attempt.attempt_id,
        request_content="泰晶科技",
    )
    published = service.deep_reports.require(report.report_id)
    published.status = "completed"
    published.quality_status = "passed_with_gaps"
    published.pipeline_state = "published"
    service.deep_reports._write_manifest(published)

    assert service.recover_interrupted_attempts() == 1
    recovered_attempt = service.store.get_attempt(session.session_id, attempt.attempt_id)
    recovered_report = service.deep_reports.require(report.report_id)
    assert recovered_attempt is not None
    assert recovered_attempt.status == AttemptStatus.COMPLETED
    assert recovered_report.status == "completed"
    assert recovered_report.pipeline_state == "published"
