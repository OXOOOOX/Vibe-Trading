"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus, Message, Session
from src.session.search import get_shared_index
from src.session.store import SessionStore
from src.usage import UsageRecorder, UsageStore, bind_usage_recorder

if TYPE_CHECKING:
    from src.agent.loop import AgentLoop

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
_STANDARD_AGENT_MAX_ITERATIONS = 50
_EQUITY_DEEP_REPORT_MAX_ITERATIONS = 100

# Portfolio-page sessions are explicitly research-only. Keep their registry
# narrow enough that an LLM cannot reach any broker/order tool even if a live
# MCP connector is configured for ordinary Agent sessions. The unified data
# facade is the only market/fundamentals/news/report entry point exposed here.
_PORTFOLIO_ANALYSIS_TOOL_NAMES = [
    "load_skill",
    "publish_obsidian_note",
    "portfolio_state",
    "get_data_context",
    "web_search",
    "read_url",
    "read_research_document",
    "query_research_knowledge",
    "get_sector_info",
    "get_fund_flow",
    "get_northbound_flow",
    "get_margin_trading",
    "get_block_trades",
    "get_dragon_tiger",
    "get_shareholder_count",
    "get_lockup_expiry",
    "pattern",
]

# Daily-run workers receive an immutable data manifest in their prompt. They
# may load a formatting/research skill, but cannot re-read portfolio state,
# bypass the data refresh policy, write files, or reach any broker surface.
_PORTFOLIO_DAILY_RUN_TOOL_NAMES = ["load_skill"]

# Remote chat channels are intentionally limited to research and backtesting.
# This is an actual registry allowlist, not a prompt-only promise: broker,
# order, mandate, shell, swarm, and generic file-write tools never enter the
# AgentLoop for sessions created by ChannelRuntime.
_CHANNEL_RESEARCH_TOOL_NAMES = [
    "load_skill",
    "portfolio_state",
    "get_data_context",
    "web_search",
    "read_url",
    "read_research_document",
    "query_research_knowledge",
    "read_document",
    "read_file",
    "analyze_image",
    "get_sector_info",
    "get_fund_flow",
    "get_northbound_flow",
    "get_margin_trading",
    "get_block_trades",
    "get_dragon_tiger",
    "get_shareholder_count",
    "get_lockup_expiry",
    "get_macro_series",
    "iwencai_search",
    "screen_market",
    "get_options_chain",
    "options_pricing",
    "get_sec_filings",
    "search_symbol",
    "pattern",
    "backtest",
    "alpha_zoo",
    "alpha_bench",
    "alpha_compare",
    "factor_analysis",
    "financial_rigor",
    "analyze_financial_snapshot",
    "report_audit",
    "run_research_autopilot",
    "generate_backtest_config",
    "scaffold_signal_engine",
    "link_autopilot_backtest",
    "create_hypothesis",
    "update_hypothesis",
    "link_backtest",
    "search_hypotheses",
    "start_research_goal",
    "update_research_goal_status",
    "get_research_goal",
    "add_goal_evidence",
    "extract_shadow_strategy",
    "run_shadow_backtest",
    "render_shadow_report",
    "scan_shadow_signals",
    "analyze_trade_journal",
    "remember",
    "session_search",
    "publish_obsidian_note",
]

# Equity Deep Report runs get a narrower registry than ordinary research chat.
# In particular, generic file writers and target-price helpers are absent so
# the Agent must submit section bodies to the compiler-owned Report Workspace.
_EQUITY_DEEP_RESEARCH_TOOL_NAMES = [
    "search_symbol",
    "analyze_financial_snapshot",
    "get_data_context",
    "web_search",
    "read_url",
    "read_research_document",
    "query_research_knowledge",
    "read_document",
    "read_file",
    "get_sector_info",
    "get_shareholder_count",
    "record_report_evidence",
    "financial_rigor",
    "report_workspace",
]
_EQUITY_DEEP_FINANCIAL_COMMANDS = {
    "calc",
    "implied_terminal_earnings",
    "validate_terminal_scenarios",
}


def _research_tool_names_for_session(
    session_config: Optional[Dict[str, Any]],
) -> list[str] | None:
    """Return the enforced research registry for a persisted session.

    A daily-run worker can later be rebound to a Feishu research topic so the
    user can continue discussing that report.  In that state both
    ``portfolio_daily_run`` and ``channel_policy`` are present.  The live
    channel policy must win: the immutable daily worker only needs
    ``load_skill``, while the continued chat needs portfolio and unified-data
    tools to refresh the report.
    """

    config = dict(session_config or {})
    channel_policy = dict(config.get("channel_policy") or {})
    if channel_policy.get("research_only"):
        return _CHANNEL_RESEARCH_TOOL_NAMES

    portfolio_daily_run = dict(config.get("portfolio_daily_run") or {})
    if portfolio_daily_run.get("research_only"):
        return _PORTFOLIO_DAILY_RUN_TOOL_NAMES

    portfolio_analysis = dict(config.get("portfolio_analysis") or {})
    if portfolio_analysis.get("research_only"):
        return _PORTFOLIO_ANALYSIS_TOOL_NAMES

    return None

# Compatibility tools remain available to API clients and local scripts, but
# normal Agent sessions get one policy-controlled data surface. This avoids an
# LLM silently bypassing cache/freshness/quorum rules with a lower-level call.
_UNIFIED_DATA_SUPERSEDED_TOOL_NAMES = {
    "get_market_data",
    "verified_market_data",
    "get_stock_news",
    "get_research_reports",
    "get_financial_statements",
    "get_stock_profile",
}

class SessionService:
    """Session lifecycle service.

    Attributes:
        store: Session persistence store.
        event_bus: SSE event bus.
        runs_dir: Root runs directory.
    """

    def __init__(
        self,
        store: SessionStore,
        event_bus: EventBus,
        runs_dir: Path,
        usage_store: UsageStore | None = None,
    ) -> None:
        """Initialize the session service.

        Args:
            store: Session persistence store.
            event_bus: SSE event bus.
            runs_dir: Root runs directory.
        """
        self.store = store
        self.event_bus = event_bus
        self.runs_dir = runs_dir
        self.usage_store = usage_store or UsageStore(store.base_dir.parent / "sessions.db")
        self._active_loops: Dict[str, "AgentLoop"] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # Web chat asks for an id before it can open the SSE stream.  Keep that
        # id in memory only; the session becomes durable when the first message
        # is accepted.  Abandoned composers therefore never become empty
        # session directories or sidebar entries.
        self._draft_sessions: Dict[str, Session] = {}
        self._search_index = get_shared_index()
        from src.reports import DeepReportService

        self.deep_reports = DeepReportService(runs_dir.parent / "reports")

    def _linked_report_context(self, report_id: str) -> str:
        """Build bounded structured context without injecting old Markdown."""

        try:
            from src.research import (
                get_research_knowledge_store,
                history_reuse_enabled,
                knowledge_enabled,
            )

            if knowledge_enabled() and history_reuse_enabled():
                return get_research_knowledge_store().linked_context(report_id)
        except Exception:
            pass
        record = self.deep_reports.require(report_id)
        workspace = self.deep_reports.inspect_workspace(
            report_id,
            section_ids=[],
            fact_metrics=["__catalog_only__"],
            evidence_domains=["__catalog_only__"],
            include_module_statuses=True,
            include_section_bodies=False,
        )
        return json.dumps(
            {
                "report_id": record.report_id,
                "revision": record.revision,
                "quality_status": record.quality_status,
                "symbol": record.symbol,
                "module_statuses": workspace.get("module_statuses") or {},
                "fact_catalog": workspace.get("fact_catalog") or [],
                "evidence_catalog": workspace.get("evidence_catalog") or [],
                "note": "Historical report prose is excluded; inspect original sources before new factual claims.",
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _index_research_session_message(session: Session, message: Message) -> None:
        research = dict((session.config or {}).get("research_session") or {})
        linked_report_id = str((message.metadata or {}).get("linked_report_id") or "")
        if not research and not linked_report_id:
            return
        symbol = str(research.get("symbol") or research.get("resolved_symbol") or "").upper()
        try:
            from src.research import get_research_knowledge_store, knowledge_enabled

            if knowledge_enabled():
                get_research_knowledge_store().index_research_session(
                    session_id=session.session_id,
                    symbol=symbol,
                    role=message.role,
                    content=message.content,
                    message_id=message.message_id,
                )
        except Exception:
            # The session remains authoritative and this index can be replayed.
            return

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {})
        return self._persist_new_session(session)

    def create_draft_session(
        self,
        title: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """Reserve a web-chat session id without persisting an empty session."""
        session = Session(title=title, config=config or {})
        self._draft_sessions[session.session_id] = session
        self.usage_store.start_scope("session", session.session_id)
        return session

    def is_draft_session(self, session_id: str) -> bool:
        """Return whether ``session_id`` is reserved but not yet durable."""
        return session_id in self._draft_sessions

    def persist_draft_session(self, session_id: str) -> Optional[Session]:
        """Persist a reserved session once its first message is accepted."""
        persisted = self.store.get_session(session_id)
        if persisted is not None:
            self._draft_sessions.pop(session_id, None)
            return persisted
        session = self._draft_sessions.get(session_id)
        if session is None:
            return None
        self._persist_new_session(session)
        self._draft_sessions.pop(session_id, None)
        return session

    def _persist_new_session(self, session: Session) -> Session:
        """Write and index one new durable session."""
        self.store.create_session(session)
        self.usage_store.start_scope("session", session.session_id)
        self._search_index.index_session(session.session_id, session.title)
        self.event_bus.emit(
            session.session_id,
            "session.created",
            {"session_id": session.session_id, "title": session.title},
        )
        return session

    def fork_session(self, session_id: str, after_message_id: str, title: str = "") -> Session:
        """Create a new session containing history through one source message.

        Args:
            session_id: Source session ID.
            after_message_id: Last source message to include in the fork.
            title: Optional title for the forked session.

        Returns:
            The newly created Session.

        Raises:
            ValueError: If the source session or target message does not exist.
            PermissionError: If the target is not an assistant output.
        """
        source = self.store.get_session(session_id)
        if not source:
            raise ValueError(f"Session {session_id} not found")

        messages = self.store.get_all_messages(session_id)
        target_index = next(
            (idx for idx, msg in enumerate(messages) if msg.message_id == after_message_id),
            -1,
        )
        if target_index < 0:
            raise ValueError(f"Message {after_message_id} not found in session {session_id}")
        if messages[target_index].role != "assistant":
            raise PermissionError("Conversation branches can only start from assistant outputs")

        fork_title = title.strip() or f"{source.title or session_id} (fork)"
        fork = Session(title=fork_title, config=dict(source.config))
        self.store.create_session(fork)
        self.usage_store.start_scope("session", fork.session_id)
        self._search_index.index_session(fork.session_id, fork.title)

        for source_message in messages[: target_index + 1]:
            metadata = dict(source_message.metadata)
            metadata.setdefault("forked_from_session_id", session_id)
            metadata.setdefault("forked_from_message_id", source_message.message_id)
            copied = Message(
                session_id=fork.session_id,
                role=source_message.role,
                content=source_message.content,
                created_at=source_message.created_at,
                linked_attempt_id=None,
                metadata=metadata,
            )
            self.store.append_message(copied)
            self._search_index.index_message(fork.session_id, copied.role, copied.content)

        fork.updated_at = datetime.now().isoformat()
        self.store.update_session(fork)
        self.event_bus.emit(
            fork.session_id,
            "session.created",
            {
                "session_id": fork.session_id,
                "title": fork.title,
                "forked_from_session_id": session_id,
                "forked_after_message_id": after_message_id,
            },
        )
        return fork

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID."""
        return self.store.get_session(session_id) or self._draft_sessions.get(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        self.event_bus.clear(session_id)
        draft_removed = self._draft_sessions.pop(session_id, None) is not None
        deleted = self.store.delete_session(session_id) or draft_removed
        if deleted:
            self.usage_store.delete_scope("session", session_id)
        return deleted

    def update_session(self, session: Session) -> None:
        """Update a durable session or its in-memory draft."""
        if session.session_id in self._draft_sessions:
            self._draft_sessions[session.session_id] = session
            return
        self.store.update_session(session)

    async def send_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        """Reserve IDs and schedule a serialized turn for this session.

        Args:
            session_id: Session ID.
            content: Message content.
            role: Message role.
            include_shell_tools: Whether this attempt may use shell tools.

        Returns:
            Dictionary containing message_id and attempt_id.
        """
        if self.is_draft_session(session_id):
            self.persist_draft_session(session_id)
        if not self.store.get_session(session_id):
            raise ValueError(f"Session {session_id} not found")
        message_id = uuid.uuid4().hex[:12]
        attempt_id = uuid.uuid4().hex[:12]
        asyncio.create_task(
            self.execute_message(
                session_id=session_id,
                content=content,
                role=role,
                include_shell_tools=include_shell_tools,
                message_id=message_id,
                attempt_id=attempt_id,
            )
        )
        result: Dict[str, Any] = {"message_id": message_id}
        if role == "user":
            result["attempt_id"] = attempt_id
        return result

    async def execute_message(
        self,
        session_id: str,
        content: str,
        role: str = "user",
        *,
        include_shell_tools: bool = False,
        message_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        message_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist and execute one turn under a per-session single-flight lock."""
        if self.is_draft_session(session_id):
            self.persist_draft_session(session_id)
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.store.get_session(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            metadata = dict(message_metadata or {})
            response_mode = str(metadata.get("response_mode") or "chat").strip().lower()
            report_profile = str(metadata.get("report_profile") or "").strip() or None
            if response_mode not in {"chat", "deep_report"}:
                raise ValueError("response_mode must be chat or deep_report")
            attempt_prompt = content
            linked_report_id = str(metadata.get("linked_report_id") or "").strip()
            if response_mode == "chat" and linked_report_id:
                linked_report = self.deep_reports.require(linked_report_id)
                if linked_report.session_id != session_id:
                    raise ValueError("linked Deep Report does not belong to this session")
                linked_content = self._linked_report_context(linked_report_id)
                attempt_prompt = (
                    "[LINKED_DEEP_REPORT_CONTEXT]\n"
                    f"report_id={linked_report.report_id}\n"
                    f"revision={linked_report.revision}\n"
                    f"quality_status={linked_report.quality_status}\n"
                    "Answer the user's follow-up without creating or modifying a report revision. "
                    "Verified Facts may be reused only while valid; Prior Claims are context, not Evidence.\n\n"
                    f"[USER_FOLLOWUP]\n{content}\n\n"
                    f"[LINKED_STRUCTURED_CONTEXT]\n{linked_content[:24000]}"
                )
            elif response_mode == "deep_report":
                if not self._deep_report_enabled():
                    raise RuntimeError("Deep Report is disabled by VIBE_TRADING_DEEP_REPORT_ENABLED")
                report_profile = report_profile or "equity_deep_research"
                allowed = self._enabled_deep_report_profiles()
                if report_profile not in allowed:
                    raise ValueError(f"report profile is not enabled: {report_profile}")
                if report_profile != "equity_deep_research":
                    raise ValueError(f"unsupported report profile: {report_profile}")
                from src.reports import build_equity_deep_research_prompt

                attempt_prompt = build_equity_deep_research_prompt(
                    content,
                    parent_report_id=str(metadata.get("parent_report_id") or "") or None,
                    revision_sections=[
                        str(item) for item in (metadata.get("revision_sections") or []) if str(item)
                    ],
                    revision_mode=str(metadata.get("revision_mode") or "initial"),
                )
                metadata["response_mode"] = "deep_report"
                metadata["report_profile"] = report_profile

            message = Message(
                message_id=message_id or uuid.uuid4().hex[:12],
                session_id=session_id,
                role=role,
                content=content,
                metadata=metadata,
            )
            self.store.append_message(message)
            self._index_research_session_message(session, message)
            self._search_index.index_message(session_id, role, content)
            self.event_bus.emit(
                session_id,
                "message.received",
                {"message_id": message.message_id, "role": role, "content": content},
            )
            if role != "user":
                return {"message_id": message.message_id}

            attempt = Attempt(
                attempt_id=attempt_id or uuid.uuid4().hex[:12],
                session_id=session_id,
                parent_attempt_id=session.last_attempt_id,
                prompt=attempt_prompt,
                metadata={
                    "response_mode": response_mode,
                    "report_profile": report_profile,
                    "request_content": content,
                    "parent_report_id": metadata.get("parent_report_id"),
                    "revision_sections": metadata.get("revision_sections") or [],
                    "revision_mode": metadata.get("revision_mode") or "initial",
                    "linked_report_id": linked_report_id or None,
                    "generation_source": metadata.get("generation_source") or "manual",
                    "generation_reason": metadata.get("generation_reason") or "",
                },
            )
            self.store.create_attempt(attempt)
            message.linked_attempt_id = attempt.attempt_id
            self.store.replace_messages(session_id, self.store.get_all_messages(session_id)[:-1] + [message])
            session.config["include_shell_tools"] = include_shell_tools
            session.last_attempt_id = attempt.attempt_id
            session.updated_at = datetime.now().isoformat()
            self.store.update_session(session)
            self.event_bus.emit(
                session_id,
                "attempt.created",
                {"attempt_id": attempt.attempt_id, "prompt": content},
            )
            await self._run_attempt(session, attempt, include_shell_tools=include_shell_tools)
            return {"message_id": message.message_id, "attempt_id": attempt.attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        """Return the message history."""
        return self.store.get_messages(session_id, limit)

    async def edit_user_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
        *,
        rerun: bool = True,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        """Edit the latest user message, prune its old response, and optionally rerun it."""
        session = self.store.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")
        if session_id in self._active_loops:
            raise RuntimeError("Cannot edit while an agent attempt is still running; cancel it first.")

        messages = self.store.get_all_messages(session_id)
        target_index = next((idx for idx, msg in enumerate(messages) if msg.message_id == message_id), -1)
        if target_index < 0:
            raise ValueError(f"Message {message_id} not found in session {session_id}")
        target = messages[target_index]
        if target.role != "user":
            raise ValueError("Only user messages can be edited")
        if any(msg.role == "user" for msg in messages[target_index + 1:]):
            raise ValueError("Only the latest user message can be edited")

        old_content = target.content
        target.content = content
        target.metadata = dict(target.metadata or {})
        target.metadata.setdefault("original_content", old_content)
        target.metadata["edited_at"] = datetime.now().isoformat()
        kept = messages[: target_index + 1]

        result: Dict[str, Any] = {"message_id": target.message_id}
        if rerun:
            attempt_metadata = {
                key: value for key, value in dict(target.metadata or {}).items()
                if key in {
                    "response_mode", "report_profile", "parent_report_id", "revision_sections",
                    "revision_mode", "linked_report_id", "generation_source", "generation_reason",
                }
            }
            attempt_prompt = content
            linked_report_id = str(attempt_metadata.get("linked_report_id") or "").strip()
            if attempt_metadata.get("response_mode") == "chat" and linked_report_id:
                linked_report = self.deep_reports.require(linked_report_id)
                if linked_report.session_id != session_id:
                    raise ValueError("linked Deep Report does not belong to this session")
                linked_content = self._linked_report_context(linked_report_id)
                attempt_prompt = (
                    "[LINKED_DEEP_REPORT_CONTEXT]\n"
                    f"report_id={linked_report.report_id}\n"
                    f"revision={linked_report.revision}\n"
                    f"quality_status={linked_report.quality_status}\n"
                    "Answer the user's follow-up without creating or modifying a report revision.\n\n"
                    f"[USER_FOLLOWUP]\n{content}\n\n"
                    "Verified Facts may be reused only while valid; Prior Claims are context, not Evidence.\n\n"
                    f"[LINKED_STRUCTURED_CONTEXT]\n{linked_content[:24000]}"
                )
            elif attempt_metadata.get("response_mode") == "deep_report":
                from src.reports import build_equity_deep_research_prompt

                attempt_prompt = build_equity_deep_research_prompt(
                    content,
                    parent_report_id=str(attempt_metadata.get("parent_report_id") or "") or None,
                    revision_sections=[
                        str(item) for item in (attempt_metadata.get("revision_sections") or []) if str(item)
                    ],
                    revision_mode=str(attempt_metadata.get("revision_mode") or "initial"),
                )
            attempt = Attempt(
                session_id=session_id,
                parent_attempt_id=session.last_attempt_id,
                prompt=attempt_prompt,
                metadata={**attempt_metadata, "request_content": content},
            )
            self.store.create_attempt(attempt)
            target.linked_attempt_id = attempt.attempt_id
            session.config["include_shell_tools"] = include_shell_tools
            session.last_attempt_id = attempt.attempt_id
            result["attempt_id"] = attempt.attempt_id
        else:
            target.linked_attempt_id = None

        self.store.replace_messages(session_id, kept)
        self._search_index.reindex_from_store(self.store.base_dir)
        session.updated_at = datetime.now().isoformat()
        self.store.update_session(session)
        self.event_bus.emit(session_id, "message.edited", {"message_id": target.message_id, "content": content})

        if rerun:
            self.event_bus.emit(session_id, "attempt.created", {"attempt_id": result["attempt_id"], "prompt": content})
            asyncio.create_task(self._run_attempt(session, attempt, include_shell_tools=include_shell_tools))

        return result

    def cancel_current(self, session_id: str) -> bool:
        """Cancel the currently running AgentLoop for a session.

        Args:
            session_id: Session ID.

        Returns:
            Whether cancellation succeeded. True means an active loop existed and received a cancel signal.
        """
        loop = self._active_loops.get(session_id)
        if loop is None:
            return False
        loop.cancel()
        return True

    def recover_interrupted_attempts(self) -> int:
        """Reconcile attempts whose in-process worker disappeared on restart.

        A persisted ``pending`` or ``running`` attempt cannot still have a
        live AgentLoop when a new server process starts.  Leaving those states
        untouched makes the UI wait forever and keeps revision actions
        disabled.  Reconciliation mirrors an already-terminal report when one
        exists; otherwise it closes both the attempt and report as a technical
        interruption and publishes an actionable diagnostic.
        """

        recovered = 0
        interruption = (
            "后端进程在任务完成前重启，本次运行已中断；"
            "请使用“用新数据更新”创建新的 revision。"
        )
        for attempt in self.store.list_attempts():
            if attempt.status not in {AttemptStatus.PENDING, AttemptStatus.RUNNING}:
                continue

            report = self.deep_reports.find_by_attempt(attempt.session_id, attempt.attempt_id)
            if report is not None and report.status == "completed":
                attempt.mark_completed(summary=self._format_deep_report_delivery(report))
            elif report is not None and report.status == "cancelled":
                attempt.mark_cancelled("报告任务已取消")
            else:
                attempt.mark_failed(interruption)
                if report is not None and report.status == "running":
                    report = self.deep_reports.mark_failed(report.report_id, interruption)

            if report is not None:
                attempt.metadata.update(
                    {
                        "report_id": report.report_id,
                        "report_quality_status": report.quality_status,
                        "report_symbol": report.symbol,
                        "report_security_name": report.security_name,
                        "report_revision": report.revision,
                        "report_parent_id": report.parent_report_id,
                        "report_delivery_kind": report.delivery_kind,
                    }
                )
            self.store.update_attempt(attempt)

            existing_reply = next(
                (
                    message
                    for message in self.store.get_all_messages(attempt.session_id)
                    if message.role == "assistant"
                    and message.linked_attempt_id == attempt.attempt_id
                ),
                None,
            )
            if existing_reply is None:
                delivery_message = (
                    self._format_deep_report_delivery(report)
                    if report is not None and report.status == "completed"
                    else interruption
                )
                reply = Message(
                    session_id=attempt.session_id,
                    role="assistant",
                    content=delivery_message,
                    linked_attempt_id=attempt.attempt_id,
                    metadata={
                        "status": attempt.status.value,
                        **(
                            {
                                "report_id": report.report_id,
                                "report_quality_status": report.quality_status,
                                "report_symbol": report.symbol,
                                "report_security_name": report.security_name,
                                "report_revision": report.revision,
                                "report_parent_id": report.parent_report_id,
                                "report_delivery_kind": report.delivery_kind,
                                "report_artifacts": report.artifacts,
                            }
                            if report is not None
                            else {}
                        ),
                    },
                )
                self.store.append_message(reply)
                self._search_index.index_message(
                    attempt.session_id,
                    "assistant",
                    reply.content,
                )

            self.event_bus.emit(
                attempt.session_id,
                "attempt.completed" if attempt.status == AttemptStatus.COMPLETED else "attempt.cancelled"
                if attempt.status == AttemptStatus.CANCELLED else "attempt.failed",
                {
                    "attempt_id": attempt.attempt_id,
                    "status": attempt.status.value,
                    "error": attempt.error,
                    **({"report_id": report.report_id} if report is not None else {}),
                },
            )
            recovered += 1
        return recovered

    async def _run_attempt(self, session: Session, attempt: Attempt, *, include_shell_tools: bool = False) -> None:
        """Execute an Attempt in the background."""
        report_record = None
        if attempt.metadata.get("response_mode") == "deep_report":
            report_record = self.deep_reports.begin(
                session_id=session.session_id,
                attempt_id=attempt.attempt_id,
                request_content=str(attempt.metadata.get("request_content") or attempt.prompt),
                profile=str(attempt.metadata.get("report_profile") or "equity_deep_research"),
                parent_report_id=str(attempt.metadata.get("parent_report_id") or "") or None,
                generation_source=str(attempt.metadata.get("generation_source") or "") or None,
                generation_reason=str(attempt.metadata.get("generation_reason") or "") or None,
                revision_mode=str(attempt.metadata.get("revision_mode") or "initial"),
                revision_sections=[
                    str(value) for value in (attempt.metadata.get("revision_sections") or [])
                ],
            )
            attempt.metadata["report_id"] = report_record.report_id
        attempt.mark_running()
        self.store.update_attempt(attempt)
        self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})
        if report_record is not None:
            self.event_bus.emit(
                session.session_id,
                "report.started",
                {
                    "attempt_id": attempt.attempt_id,
                    "report_id": report_record.report_id,
                    "profile": report_record.profile,
                    "phase": "financial_data",
                    "revision": report_record.revision,
                    "parent_report_id": report_record.parent_report_id,
                    "revision_mode": report_record.revision_mode,
                },
            )
            for phase, message in (
                ("history", "读取历史研究，历史结论仅作为待复核上下文"),
                ("coverage_planning", "制定资料清单和权威来源覆盖门槛"),
                ("source_verification", "核验权威来源并检查资料时效"),
            ):
                self.event_bus.emit(
                    session.session_id,
                    "report.progress",
                    {
                        "attempt_id": attempt.attempt_id,
                        "report_id": report_record.report_id,
                        "phase": phase,
                        "message": message,
                    },
                )

        try:
            messages = self.store.get_messages(session.session_id)
            result = await self._run_with_agent(
                attempt,
                messages=messages,
                include_shell_tools=include_shell_tools,
                session_config=dict(session.config),
            )
            if result.get("status") == "success":
                attempt.mark_completed(summary=result.get("content", ""))
            elif result.get("status") == "cancelled":
                attempt.mark_cancelled(result.get("reason", "cancelled by user"))
            else:
                attempt.mark_failed(error=result.get("reason", "unknown"))
            attempt.run_dir = result.get("run_dir")
            # AgentLoop already records an event stream on disk, but retain the
            # compact ReAct trace on the Attempt as well so completed sessions
            # remain diagnosable through the session API after SSE replay ages
            # out.
            attempt.react_trace = list(result.get("react_trace") or [])

            if report_record is not None:
                self.event_bus.emit(
                    session.session_id,
                    "report.progress",
                    {
                        "attempt_id": attempt.attempt_id,
                        "report_id": report_record.report_id,
                        "phase": "chapter_writing",
                        "message": "Agent 已完成研究，正在编译 Report Workspace",
                    },
                )
                self.event_bus.emit(
                    session.session_id,
                    "report.progress",
                    {
                        "attempt_id": attempt.attempt_id,
                        "report_id": report_record.report_id,
                        "phase": "compiling",
                        "message": "正在生成标准章节、引用索引和 Markdown 产物",
                    },
                )
                if attempt.status == AttemptStatus.COMPLETED:
                    evaluation = self.deep_reports.evaluate_workspace(report_record.report_id)
                    if self.deep_reports.should_auto_repair(report_record.report_id, evaluation):
                        self.deep_reports.mark_repairing(report_record.report_id)
                        self.event_bus.emit(
                            session.session_id,
                            "report.progress",
                            {
                                "attempt_id": attempt.attempt_id,
                                "report_id": report_record.report_id,
                                "phase": "repairing",
                                "message": "首次校验未通过，正在自动修复失败章节（最多一次）",
                                "validation_issues": evaluation["validation"].get("issues") or [],
                            },
                        )
                        workspace = self.deep_reports.inspect_workspace(report_record.report_id)
                        target_sections = [
                            section_id
                            for section_id, section in dict(workspace.get("sections") or {}).items()
                            if str((section or {}).get("status")) != "passed"
                        ]
                        original_prompt = attempt.prompt
                        original_revision_mode = attempt.metadata.get("revision_mode")
                        attempt.metadata["pre_repair_run_dir"] = attempt.run_dir
                        attempt.metadata["agent_summary"] = attempt.summary
                        attempt.metadata["revision_mode"] = "repair"
                        attempt.prompt = (
                            "[EQUITY_DEEP_REPORT_REPAIR]\n"
                            "首次 Report Workspace 编译未通过。不要重新输出整篇报告，也不要手工替代确定性计算。\n"
                            f"需要修复的章节：{', '.join(target_sections) or '根据校验问题定位'}\n"
                            "校验问题：\n- "
                            + "\n- ".join(evaluation["validation"].get("issues") or [])
                            + "\n先用 report_workspace.inspect 读取对应章节和 Fact/Evidence，"
                            "再用 report_workspace.submit_section 只提交修复后的章节正文。"
                        )
                        attempt.mark_running()
                        self.store.update_attempt(attempt)
                        try:
                            repair_result = await self._run_with_agent(
                                attempt,
                                messages=messages,
                                include_shell_tools=include_shell_tools,
                                session_config=dict(session.config),
                            )
                        finally:
                            attempt.prompt = original_prompt
                            attempt.metadata["revision_mode"] = original_revision_mode
                        attempt.react_trace.extend([
                            {"type": "repair_round", "round": 1},
                            *list(repair_result.get("react_trace") or []),
                        ])
                        if repair_result.get("status") == "success":
                            attempt.mark_completed(summary=repair_result.get("content", ""))
                            attempt.run_dir = repair_result.get("run_dir") or attempt.run_dir
                            evaluation = self.deep_reports.evaluate_workspace(report_record.report_id)
                        elif repair_result.get("status") == "cancelled":
                            attempt.mark_cancelled(repair_result.get("reason", "cancelled by user"))
                        else:
                            attempt.mark_failed(repair_result.get("reason", "automatic report repair failed"))
                    if attempt.status == AttemptStatus.COMPLETED:
                        self.event_bus.emit(
                            session.session_id,
                            "report.progress",
                            {
                                "attempt_id": attempt.attempt_id,
                                "report_id": report_record.report_id,
                                "phase": "auditing",
                            "message": "正在逐项核对报告中的数字与数据来源",
                            },
                        )
                        report_record = self.deep_reports.publish_workspace(
                            report_record.report_id,
                            evaluation,
                        )
                    else:
                        report_record = self.deep_reports.mark_failed(
                            report_record.report_id,
                            attempt.error or "deep report repair did not complete",
                            cancelled=attempt.status == AttemptStatus.CANCELLED,
                        )
                else:
                    report_record = self.deep_reports.mark_failed(
                        report_record.report_id,
                        attempt.error or "deep report attempt did not complete",
                        cancelled=attempt.status == AttemptStatus.CANCELLED,
                    )
                attempt.metadata.update(
                    {
                        "report_id": report_record.report_id,
                        "report_quality_status": report_record.quality_status,
                        "report_symbol": report_record.symbol,
                        "report_security_name": report_record.security_name,
                        "report_revision": report_record.revision,
                        "report_parent_id": report_record.parent_report_id,
                        "report_delivery_kind": report_record.delivery_kind,
                    }
                )

            self.store.update_attempt(attempt)
            reply_metadata = {}
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
            reply_metadata["status"] = attempt.status.value
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics
            if report_record is not None:
                missing_modules = [
                    key for key, value in report_record.analysis_modules.items()
                    if value.status in {
                        "warning", "failed_validation", "insufficient_evidence", "not_requested",
                    }
                ]
                reply_metadata.update(
                    {
                        "report_id": report_record.report_id,
                        "report_profile": report_record.profile,
                        "report_quality_status": report_record.quality_status,
                        "report_symbol": report_record.symbol,
                        "report_security_name": report_record.security_name,
                        "report_data_as_of": report_record.data_as_of,
                        "report_missing_modules": missing_modules,
                        "report_artifacts": report_record.artifacts,
                        "report_generation_source": report_record.generation_source,
                        "report_generation_reason": report_record.generation_reason,
                        "report_revision": report_record.revision,
                        "report_parent_id": report_record.parent_report_id,
                        "report_revision_mode": report_record.revision_mode,
                        "report_delivery_kind": report_record.delivery_kind,
                    }
                )

            delivery_message = (
                self._format_deep_report_delivery(report_record)
                if report_record is not None and attempt.status == AttemptStatus.COMPLETED
                else self._format_result_message(attempt)
            )
            reply = Message(
                session_id=session.session_id, role="assistant",
                content=delivery_message,
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
            )
            self.store.append_message(reply)
            self._index_research_session_message(session, reply)
            self._search_index.index_message(session.session_id, "assistant", reply.content)
            terminal_event = (
                "attempt.completed"
                if attempt.status == AttemptStatus.COMPLETED
                else "attempt.cancelled"
                if attempt.status == AttemptStatus.CANCELLED
                else "attempt.failed"
            )
            self.event_bus.emit(
                session.session_id,
                terminal_event,
                {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "summary": delivery_message, "delivery_message": delivery_message,
                 "error": attempt.error, "run_dir": attempt.run_dir,
                 "message_id": reply.message_id,
                 **({
                     "report_id": report_record.report_id,
                     "report_profile": report_record.profile,
                     "report_quality_status": report_record.quality_status,
                     "report_symbol": report_record.symbol,
                     "report_security_name": report_record.security_name,
                     "report_data_as_of": report_record.data_as_of,
                     "report_missing_modules": missing_modules,
                     "report_artifacts": report_record.artifacts,
                     "report_generation_source": report_record.generation_source,
                     "report_generation_reason": report_record.generation_reason,
                     "report_revision": report_record.revision,
                     "report_parent_id": report_record.parent_report_id,
                     "report_revision_mode": report_record.revision_mode,
                     "report_delivery_kind": report_record.delivery_kind,
                 } if report_record is not None else {})},
            )
            if report_record is not None:
                self.event_bus.emit(
                    session.session_id,
                    (
                        "report.completed"
                        if report_record.status == "completed"
                        else "report.cancelled"
                        if report_record.status == "cancelled"
                        else "report.failed"
                    ),
                    {
                        "attempt_id": attempt.attempt_id,
                        "report_id": report_record.report_id,
                        "profile": report_record.profile,
                        "status": report_record.status,
                        "quality_status": report_record.quality_status,
                        "symbol": report_record.symbol,
                        "security_name": report_record.security_name,
                        "data_as_of": report_record.data_as_of,
                        "artifacts": report_record.artifacts,
                        "validation_issues": report_record.validation_issues,
                        "generation_source": report_record.generation_source,
                        "generation_reason": report_record.generation_reason,
                        "revision": report_record.revision,
                        "parent_report_id": report_record.parent_report_id,
                        "revision_mode": report_record.revision_mode,
                        "delivery_kind": report_record.delivery_kind,
                        "analysis_modules": {
                            key: {
                                "status": value.status,
                                "coverage": value.coverage,
                                "reason": value.reason,
                            }
                            for key, value in report_record.analysis_modules.items()
                        },
                        "missing_modules": missing_modules,
                    },
                )

        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            if report_record is not None:
                try:
                    report_record = self.deep_reports.mark_failed(report_record.report_id, str(exc))
                except Exception:
                    pass
            self.event_bus.emit(session.session_id, "attempt.failed", {"attempt_id": attempt.attempt_id, "error": str(exc)})

    @staticmethod
    def _deep_report_enabled() -> bool:
        return os.getenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "0").strip().lower() not in {
            "0", "false", "no", "off",
        }

    @staticmethod
    def _enabled_deep_report_profiles() -> set[str]:
        raw = os.getenv("VIBE_TRADING_DEEP_REPORT_PROFILES", "equity_deep_research")
        return {item.strip() for item in raw.split(",") if item.strip()}

    async def _run_with_agent(
        self,
        attempt: Attempt,
        messages: list = None,
        *,
        include_shell_tools: bool = False,
        session_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an attempt with the V5 AgentLoop.

        Args:
            attempt: Current execution attempt.
            messages: Session message history.
            include_shell_tools: Whether the registry may include shell tools.
            session_config: Optional session-level config overrides. MCP server
                definitions under the ``mcpServers`` key are merged on top of
                the user config file via ``load_runtime_agent_config`` so each
                session can extend or override the global MCP server list.

        Returns:
            Result dictionary containing status, run_dir, run_id, metrics, and related fields.
        """
        from src.tools import build_filtered_registry, build_registry
        from src.providers.chat import ChatLLM
        from src.agent.loop import AgentLoop
        from src.memory.persistent import PersistentMemory
        from src.config.loader import load_runtime_agent_config, sanitize_session_overrides

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        def _usage_notify(revision: int) -> None:
            self.event_bus.emit(
                session_id,
                "usage.updated",
                {
                    "scope_type": "session",
                    "scope_id": session_id,
                    "attempt_id": attempt_id,
                    "revision": revision,
                },
            )

        usage_recorder = UsageRecorder(
            store=self.usage_store,
            scope_type="session",
            scope_id=session_id,
            session_id=session_id,
            attempt_id=attempt_id,
            notify=_usage_notify,
        )
        llm = ChatLLM()
        pm = PersistentMemory()

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            if event_type == "report.analysis_snapshot":
                report_id = str(attempt.metadata.get("report_id") or "")
                analysis = data.get("analysis")
                if report_id and isinstance(analysis, dict):
                    self.deep_reports.attach_analysis(report_id, analysis)
                    self.event_bus.emit(
                        session_id,
                        "report.progress",
                        {
                            "attempt_id": attempt_id,
                            "report_id": report_id,
                            "phase": "financial_standardization",
                            "message": "财务数据已完成核验并保存",
                            "quality_status": analysis.get("quality_status"),
                            "module_statuses": analysis.get("module_statuses") or {},
                        },
                    )
                return
            if event_type == "report.external_evidence":
                report_id = str(attempt.metadata.get("report_id") or "")
                bundle = data.get("bundle")
                if report_id and isinstance(bundle, dict):
                    self.deep_reports.attach_external_evidence(report_id, bundle)
                    self.event_bus.emit(
                        session_id,
                        "report.progress",
                        {
                            "attempt_id": attempt_id,
                            "report_id": report_id,
                            "phase": "industry_evidence",
                            "message": "外部证据与提取事实已写入报告账本",
                        },
                    )
                return
            if event_type == "report.deterministic_result":
                report_id = str(attempt.metadata.get("report_id") or "")
                command = str(data.get("command") or "")
                result = data.get("result")
                if report_id and command and isinstance(result, dict):
                    if (
                        attempt.metadata.get("report_profile") == "equity_deep_research"
                        and command not in {"implied_terminal_earnings", "validate_terminal_scenarios"}
                    ):
                        raise ValueError(f"deterministic command is not allowed for equity_deep_research: {command}")
                    self.deep_reports.attach_deterministic_result(report_id, command, result)
                    self.event_bus.emit(
                        session_id,
                        "report.progress",
                        {
                            "attempt_id": attempt_id,
                            "report_id": report_id,
                            "phase": "deterministic_calculation",
                            "message": "财务与估值计算结果已完成核验并保存",
                        },
                    )
                return
            if event_type == "report.audit_result":
                report_id = str(attempt.metadata.get("report_id") or "")
                audit_result = data.get("result")
                if report_id and isinstance(audit_result, dict):
                    self.deep_reports.attach_audit_result(report_id, audit_result)
                    self.event_bus.emit(
                        session_id,
                        "report.progress",
                        {
                            "attempt_id": attempt_id,
                            "report_id": report_id,
                            "phase": "review",
                            "message": "数字抽样审计已完整覆盖并绑定最终草稿",
                            "audit_id": audit_result.get("audit_id"),
                        },
                    )
                return
            if event_type == "report.workspace_section":
                report_id = str(attempt.metadata.get("report_id") or "")
                self.event_bus.emit(
                    session_id,
                    "report.progress",
                    {
                        "attempt_id": attempt_id,
                        "report_id": report_id,
                        "phase": "chapter_writing",
                        "message": f"章节 {data.get('section_id') or ''} 已通过工作区校验",
                        "section_id": data.get("section_id"),
                    },
                )
                return
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        is_equity_deep_report = (
            attempt.metadata.get("response_mode") == "deep_report"
            and attempt.metadata.get("report_profile") == "equity_deep_research"
        )

        def _workspace_string_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                raw = value.strip()
                if raw.startswith("[") and raw.endswith("]"):
                    try:
                        parsed = ast.literal_eval(raw)
                    except (SyntaxError, ValueError):
                        parsed = None
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed if str(item).strip()]
                return [item.strip(" \t'\"") for item in raw.split(",") if item.strip(" \t'\"")]
            return []

        def _workspace_bool(value: Any, default: bool = True) -> bool:
            if value is None:
                return default
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "no", "off"}
            return bool(value)

        def _report_workspace_handler(command: str, payload: dict[str, Any]) -> dict[str, Any]:
            report_id = str(attempt.metadata.get("report_id") or "")
            if not is_equity_deep_report or not report_id:
                raise ValueError("report workspace is unavailable outside an active Deep Report")
            if command == "inspect":
                fact_metrics = _workspace_string_list(payload.get("fact_metrics"))
                evidence_domains = _workspace_string_list(payload.get("evidence_domains"))
                return {
                    "status": "ok",
                    **self.deep_reports.inspect_workspace(
                        report_id,
                        section_ids=_workspace_string_list(payload.get("section_ids")) or None,
                        # Unfiltered inspect is deliberately catalog-only.  A
                        # complete Fact/Evidence ledger plus eight inherited
                        # section bodies can exceed a model context before it
                        # writes anything.  The Agent first sees catalogs, then
                        # requests only the records needed for a section.
                        fact_metrics=fact_metrics or ["__catalog_only__"],
                        evidence_domains=evidence_domains or ["__catalog_only__"],
                        include_module_statuses=_workspace_bool(
                            payload.get("include_module_statuses"), True
                        ),
                        include_section_bodies=(
                            _workspace_bool(payload.get("include_section_bodies"))
                            if payload.get("include_section_bodies") is not None
                            else None
                        ),
                    ),
                }
            if command == "submit_section":
                section_id = str(payload.get("section_id") or "")
                body_markdown = str(payload.get("body_markdown") or "")
                section = self.deep_reports.submit_section(
                    report_id,
                    section_id=section_id,
                    body_markdown=body_markdown,
                )
                return {"status": "ok", "section": section.to_dict()}
            raise ValueError(f"unknown report workspace command: {command}")

        research_tool_names = (
            list(_EQUITY_DEEP_RESEARCH_TOOL_NAMES)
            if is_equity_deep_report
            else _research_tool_names_for_session(session_config)
        )
        if is_equity_deep_report:
            revision_mode = str(attempt.metadata.get("revision_mode") or "initial")
            if revision_mode == "repair":
                # A repair round receives only the failed section bodies, exact
                # validation issues, Ledger indexes, and deterministic module
                # states through Report Workspace. It cannot mutate evidence,
                # refresh data, browse, or rerun a failed deterministic model.
                research_tool_names = ["report_workspace"]
            elif revision_mode == "section_revision":
                research_tool_names = [
                    name for name in (research_tool_names or [])
                    if name != "analyze_financial_snapshot"
                ]
        if research_tool_names is not None:
            registry = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: build_filtered_registry(
                    research_tool_names,
                    include_shell_tools=False,
                    session_id=session_id,
                    event_callback=event_callback,
                    financial_rigor_commands=(
                        _EQUITY_DEEP_FINANCIAL_COMMANDS if is_equity_deep_report else None
                    ),
                    report_workspace_handler=(
                        _report_workspace_handler if is_equity_deep_report else None
                    ),
                ),
            )
        else:
            registry = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: build_registry(
                    persistent_memory=pm,
                    include_shell_tools=include_shell_tools,
                    agent_config=agent_config,
                    session_id=session_id,
                    event_callback=event_callback,
                    warn_callback=_mcp_collision_warn,
                    exclude_tool_names=_UNIFIED_DATA_SUPERSEDED_TOOL_NAMES,
                ),
            )

        agent = AgentLoop(
            registry=registry,
            llm=llm,
            event_callback=event_callback,
            max_iterations=(
                _EQUITY_DEEP_REPORT_MAX_ITERATIONS
                if is_equity_deep_report
                else _STANDARD_AGENT_MAX_ITERATIONS
            ),
            persistent_memory=pm,
            usage_recorder=usage_recorder,
        )
        self._active_loops[session_id] = agent

        # Build the message history context.
        history = self._convert_messages_to_history(messages) if messages else None

        try:
            def _execute_scoped_agent() -> Dict[str, Any]:
                with bind_usage_recorder(usage_recorder):
                    return agent.run(
                        user_message=attempt.prompt,
                        history=history,
                        session_id=session_id,
                    )

            result = await loop.run_in_executor(_AGENT_EXECUTOR, _execute_scoped_agent)
        finally:
            self._active_loops.pop(session_id, None)

        # Load metrics from the run output when available.
        if result.get("run_dir"):
            metrics = self._load_metrics(Path(result["run_dir"]))
            if metrics:
                result["metrics"] = metrics

        return result

    @staticmethod
    def _convert_messages_to_history(messages: list) -> list[Dict[str, Any]]:
        """Convert Session messages into OpenAI-format history.

        Keeps the readable ``[prev_run: {run_id}]`` marker instead of removing it
        completely, and trims by character budget instead of a hard six-message cap
        so the LLM can still see previous artifact paths and strategy content during
        iterative updates.

        Args:
            messages: Session message list without the current turn.

        Returns:
            OpenAI-format messages trimmed from the newest items within the token budget.
        """
        import re
        from pathlib import Path

        def _shorten_run_dir(match: re.Match) -> str:
            path_str = match.group(0).replace("Run directory:", "").strip()
            run_id = Path(path_str).name if path_str else ""
            return f"[prev_run: {run_id}]" if run_id else ""

        history = []
        for msg in messages[:-1]:
            role = msg.role if hasattr(msg, "role") else msg.get("role", "user")
            content = msg.content if hasattr(msg, "content") else msg.get("content", "")
            if not content.strip() or role not in ("user", "assistant"):
                continue
            content = re.sub(r"Run directory:\s*\S+", _shorten_run_dir, content).strip()
            if content:
                history.append({"role": role, "content": content})

        # Trim from the newest messages within a character budget of roughly 3000 tokens.
        MAX_HISTORY_CHARS = 12000
        total_chars = 0
        trimmed: list = []
        for msg in reversed(history):
            msg_len = len(msg.get("content", ""))
            if total_chars + msg_len > MAX_HISTORY_CHARS:
                break
            trimmed.append(msg)
            total_chars += msg_len
        return list(reversed(trimmed))

    @staticmethod
    def _load_metrics(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Load metrics.csv from a run directory."""
        import csv
        metrics_path = run_dir / "artifacts" / "metrics.csv"
        if not metrics_path.exists():
            return None
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    return {k: float(v) for k, v in rows[0].items() if v}
        except Exception:
            pass
        return None

    @staticmethod
    def _format_deep_report_delivery(record: Any) -> str:
        identity = record.security_name or record.symbol or "单股"
        symbol = f"（{record.symbol}）" if record.symbol else ""
        if record.quality_status == "failed_validation":
            return (
                f"{identity}{symbol}穿透式深度研究第 {record.revision} 版尚未形成正式报告。"
                "关键数据或内容没有通过发布前校验；你可以打开诊断结果查看原因，系统不会生成 PDF。"
            )
        if record.quality_status == "passed_with_gaps":
            return (
                f"{identity}{symbol}穿透式深度研究第 {record.revision} 版已完成。"
                "部分判断仍缺少可靠公开证据，报告已明确说明保留项；点击下方卡片即可阅读完整报告。"
            )
        return (
            f"{identity}{symbol}穿透式深度研究第 {record.revision} 版已完成并通过校验。"
            "点击下方卡片即可阅读完整报告。"
        )

    @staticmethod
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message."""
        if attempt.status == AttemptStatus.COMPLETED:
            return attempt.summary or "Strategy execution completed."
        if attempt.status == AttemptStatus.CANCELLED:
            return "Execution cancelled by user."
        return f"Execution failed: {attempt.error or 'unknown error'}"
