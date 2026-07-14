from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.channels.bus.events import InboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.research_sessions import build_research_session_route
from src.channels.runtime import ChannelRuntime
from src.session.models import Message, Session


class FakeSessionService:
    def get_messages(self, *args, **kwargs):
        return []


class FakeDailyRunService:
    def __init__(self, target: Path) -> None:
        self.target = target
        self.holding_target = target.with_name("holding.pdf")
        self.store = self
        self.start_calls = 0
        self.record = {"run_id": "dpr_test", "status": "queued", "revision": 1}

    async def start(self, **kwargs):
        assert kwargs["trigger"] == "feishu"
        self.start_calls += 1
        return dict(self.record)

    async def wait(self, run_id: str):
        assert run_id == "dpr_test"
        self.target.write_bytes(b"%PDF-test")
        self.holding_target.write_bytes(b"%PDF-holding")
        self.record = {
            "run_id": run_id,
            "market_date": "2026-07-13",
            "status": "completed",
            "revision": 1,
            "summary": {"exit": 1, "reduce": 2, "add": 3, "observe": 4},
            "workers": [{"symbol": "600036.SH", "status": "completed"}],
            "artifacts": [
                {
                    "artifact_id": "master",
                    "kind": "master_pdf",
                    "path": str(self.target),
                    "revision": 1,
                },
                {
                    "artifact_id": "holding",
                    "kind": "holding_daily_pdf",
                    "symbol": "600036.SH",
                    "filename": "holding.pdf",
                    "path": str(self.holding_target),
                    "revision": 1,
                },
            ],
        }
        return dict(self.record)

    def get_run(self, run_id: str):
        return dict(self.record)

    def read_json(self, run_id: str, relative_path: str):
        return {
            "holdings": [{"symbol": "600036.SH", "name": "招商银行"}]
        } if relative_path.endswith("portfolio_snapshot.json") else None

    def resolve_artifact(self, run_id: str, artifact_id: str):
        artifact = next(
            (item for item in self.record.get("artifacts", []) if item["artifact_id"] == artifact_id),
            None,
        )
        return (artifact, Path(artifact["path"])) if artifact else None

    async def cancel(self, run_id: str):
        return {"run_id": run_id, "status": "cancelling"}


class FakeSkippedDailyRunService(FakeDailyRunService):
    async def wait(self, run_id: str):
        self.record = {
            "run_id": run_id,
            "market_date": "2026-07-13",
            "status": "completed_with_warnings",
            "stage": "skipped_data_unavailable",
            "revision": 1,
            "analysis_gate": {"eligible_count": 0, "total_count": 2},
            "warnings": ["关键数据覆盖不足。"],
            "artifacts": [],
        }
        return dict(self.record)


class StockSessionService:
    def __init__(self, symbol: str) -> None:
        now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        self.session = Session(
            session_id="stock-session-today",
            title=f"今日个股研究 {symbol}",
            created_at=now,
            updated_at=now,
            config={"portfolio_daily_run": {"symbol": symbol, "run_id": "dpr_today"}},
        )
        self.messages: dict[str, list[Message]] = {self.session.session_id: []}
        self.sent: list[tuple[str, str]] = []

    def get_session(self, session_id: str):
        return self.session if session_id == self.session.session_id else None

    def list_sessions(self, limit: int = 50):
        del limit
        return [self.session]

    def get_messages(self, session_id: str, limit: int = 100):
        del limit
        return list(self.messages.get(session_id, []))

    async def execute_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        message_metadata: dict[str, Any] | None = None,
        **_kwargs,
    ):
        self.messages[session_id].append(
            Message(
                session_id=session_id,
                role=role,
                content=content,
                metadata=message_metadata or {},
            )
        )
        return {"message_id": "seeded"}

    async def send_message(self, session_id: str, content: str, **_kwargs):
        self.sent.append((session_id, content))
        attempt_id = f"attempt-{len(self.sent)}"
        self.messages[session_id].append(
            Message(
                session_id=session_id,
                role="assistant",
                content=f"continued: {content}",
                linked_attempt_id=attempt_id,
            )
        )
        return {"message_id": "followup", "attempt_id": attempt_id}


class StockDailyRunService:
    def __init__(self, tmp_path: Path, *, session_id: str, symbol: str) -> None:
        self.store = self
        self.pdf = tmp_path / "today-holding.pdf"
        self.markdown = tmp_path / "today-holding.md"
        self.pdf.write_bytes(b"%PDF-today")
        self.markdown.write_text(
            f"# {symbol} 今日个股报告\n\n## 结论\n继续观察。\n\n## 风险\n数据变化。",
            encoding="utf-8",
        )
        now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        today = now[:10]
        self.record = {
            "run_id": "dpr_today",
            "market_date": today,
            "created_at": now,
            "status": "completed",
            "revision": 2,
            "workers": [
                {"symbol": symbol, "status": "completed", "session_id": session_id}
            ],
            "artifacts": [
                {
                    "artifact_id": "pdf-today",
                    "kind": "holding_daily_pdf",
                    "symbol": symbol,
                    "filename": self.pdf.name,
                    "path": str(self.pdf),
                    "revision": 2,
                },
                {
                    "artifact_id": "md-today",
                    "kind": "holding_daily_markdown",
                    "symbol": symbol,
                    "filename": self.markdown.name,
                    "path": str(self.markdown),
                    "revision": 2,
                },
            ],
        }

    def list(self, limit: int = 50):
        del limit
        return [dict(self.record)]

    def get_run(self, run_id: str):
        return dict(self.record) if run_id == self.record["run_id"] else None

    def resolve_artifact(self, run_id: str, artifact_id: str):
        if run_id != self.record["run_id"]:
            return None
        artifact = next(
            (
                item
                for item in self.record["artifacts"]
                if item["artifact_id"] == artifact_id
            ),
            None,
        )
        return (artifact, Path(artifact["path"])) if artifact else None

    def read_json(self, *_args, **_kwargs):
        return None


def test_channel_runtime_delivers_daily_run_master_pdf(tmp_path: Path) -> None:
    async def scenario():
        bus = MessageBus()
        daily = FakeDailyRunService(tmp_path / "master.pdf")
        runtime = ChannelRuntime(
            bus=bus,
            session_service=FakeSessionService(),
            manager=None,
            daily_run_service=daily,
            session_map_path=tmp_path / "sessions.json",
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="启动组合晨会",
                    metadata={"daily_portfolio_run": True},
                )
            )
            started = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            messages = [started]
            while len(messages) < 6:
                outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
                messages.append(outbound)
                if outbound.media:
                    break
            return messages
        finally:
            await runtime.stop()

    messages = asyncio.run(scenario())
    started = messages[0]
    completed = messages[-1]
    assert "组合晨会" in started.content and "已启动" in started.content
    assert started.metadata["_stream_delta"] is True
    assert any(item.metadata.get("_stream_end") for item in messages)
    assert completed.media == [str(tmp_path / "master.pdf")]
    assert completed.metadata["delivery_mode"] == "report_pdf"
    assert completed.metadata["portfolio_daily_complete"] is True
    assert completed.metadata["holding_reports"][0]["artifact_id"] == "holding"
    assert "退出 1" in completed.content


def test_channel_runtime_reuses_existing_holding_pdf_without_new_run(tmp_path: Path) -> None:
    async def scenario():
        bus = MessageBus()
        daily = FakeDailyRunService(tmp_path / "master.pdf")
        await daily.wait("dpr_test")
        runtime = ChannelRuntime(
            bus=bus,
            session_service=FakeSessionService(),
            manager=None,
            daily_run_service=daily,
            session_map_path=tmp_path / "sessions.json",
        )
        message = InboundMessage(
            channel="feishu",
            sender_id="u1",
            chat_id="c1",
            content="",
            metadata={
                "daily_report_action": "show_daily_reports",
                "daily_run_id": "dpr_test",
            },
        )
        await runtime._handle_daily_report_action(message)
        picker = await bus.consume_outbound()
        message.metadata.update(
            {"daily_report_action": "send_daily_report", "artifact_id": "holding"}
        )
        await runtime._handle_daily_report_action(message)
        delivered = await bus.consume_outbound()
        return daily, picker, delivered

    daily, picker, delivered = asyncio.run(scenario())
    assert picker.metadata["_daily_report_picker"] is True
    assert picker.metadata["holding_reports"][0]["symbol"] == "600036.SH"
    assert delivered.media == [str(tmp_path / "holding.pdf")]
    assert daily.start_calls == 0


def test_channel_runtime_does_not_claim_completion_when_data_gate_stops(tmp_path: Path) -> None:
    async def scenario():
        bus = MessageBus()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=FakeSessionService(),
            manager=None,
            daily_run_service=FakeSkippedDailyRunService(tmp_path / "master.pdf"),
            session_map_path=tmp_path / "sessions.json",
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="启动组合晨会",
                    metadata={"daily_portfolio_run": True},
                )
            )
            messages = []
            while len(messages) < 6:
                outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
                messages.append(outbound)
                if outbound.metadata.get("portfolio_daily_skipped"):
                    break
            return messages
        finally:
            await runtime.stop()

    messages = asyncio.run(scenario())
    stopped = messages[-1]
    assert stopped.metadata["portfolio_daily_skipped"] is True
    assert stopped.media == []
    assert "未启动个股研究 Session" in stopped.content
    assert not any(item.metadata.get("portfolio_daily_complete") for item in messages)


def test_stock_analysis_reuses_todays_session_and_report_for_followups(tmp_path: Path) -> None:
    async def scenario():
        symbol = "600036.SH"
        service = StockSessionService(symbol)
        daily = StockDailyRunService(
            tmp_path,
            session_id=service.session.session_id,
            symbol=symbol,
        )
        bus = MessageBus()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            daily_run_service=daily,
            session_map_path=tmp_path / "sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        route = build_research_session_route(
            base_key="feishu:c1",
            action="holding",
            symbol=symbol,
            name="招商银行",
        )
        prepare_metadata = {
            "stock_research_action": "prepare",
            "research_action": "holding",
            "research_label": route.label,
            **route.metadata(),
        }
        await runtime._handle_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="c1",
                content="full report prompt",
                metadata=prepare_metadata,
                session_key_override=route.route_key,
            )
        )
        chooser = await bus.consume_outbound()

        await runtime._handle_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="c1",
                content="",
                metadata={
                    "stock_research_action": "send_stock_daily_report",
                    "stock_session_id": service.session.session_id,
                    "daily_run_id": "dpr_today",
                    "artifact_id": "pdf-today",
                    "research_action": "holding",
                    "research_label": route.label,
                    **route.metadata(),
                },
                session_key_override=route.route_key,
            )
        )
        delivered = await bus.consume_outbound()

        await runtime._handle_inbound(
            InboundMessage(
                channel="feishu",
                sender_id="u1",
                chat_id="c1",
                content="报告里的风险具体怎么看？",
            )
        )
        followup = await bus.consume_outbound()
        return runtime, service, chooser, delivered, followup

    runtime, service, chooser, delivered, followup = asyncio.run(scenario())
    context = chooser.metadata["stock_research_context"]
    assert chooser.metadata["_stock_research_context"] is True
    assert context["session"]["session_id"] == "stock-session-today"
    assert context["report"]["artifact_id"] == "pdf-today"
    assert service.sent == [
        ("stock-session-today", "报告里的风险具体怎么看？")
    ]
    assert delivered.media == [str(tmp_path / "today-holding.pdf")]
    assert delivered.metadata["stock_report_continuation"] is True
    assert any(
        message.metadata.get("daily_report_context_run_id") == "dpr_today"
        for message in service.messages["stock-session-today"]
    )
    assert runtime._session_map["feishu:c1"] == "stock-session-today"
    assert followup.content == "continued: 报告里的风险具体怎么看？"
