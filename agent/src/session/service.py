"""Session lifecycle orchestration for message flow, attempt creation, and execution scheduling.

V5: Uses AgentLoop instead of the fixed pipeline behind the generate skill.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Dedicated thread pool limited to four concurrent agents to avoid exhausting the default executor.
_AGENT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")

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

from src.session.events import EventBus
from src.session.models import (
    Attempt,
    AttemptStatus,
    Message,
    Session,
)
from src.session.search import get_shared_index
from src.session.store import SessionStore


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
        self._active_loops: Dict[str, "AgentLoop"] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._search_index = get_shared_index()

    def create_session(self, title: str = "", config: Optional[Dict[str, Any]] = None) -> Session:
        """Create a new session.

        Args:
            title: Session title.
            config: Session configuration.

        Returns:
            The newly created Session.
        """
        session = Session(title=title, config=config or {})
        self.store.create_session(session)
        self._search_index.index_session(session.session_id, title)
        self.event_bus.emit(session.session_id, "session.created", {"session_id": session.session_id, "title": title})
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
        return self.store.get_session(session_id)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        """List all sessions."""
        return self.store.list_sessions(limit)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        self.event_bus.clear(session_id)
        return self.store.delete_session(session_id)

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
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.store.get_session(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            message = Message(
                message_id=message_id or uuid.uuid4().hex[:12],
                session_id=session_id,
                role=role,
                content=content,
                metadata=dict(message_metadata or {}),
            )
            self.store.append_message(message)
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
                prompt=content,
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
            attempt = Attempt(session_id=session_id, parent_attempt_id=session.last_attempt_id, prompt=content)
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

    async def _run_attempt(self, session: Session, attempt: Attempt, *, include_shell_tools: bool = False) -> None:
        """Execute an Attempt in the background."""
        attempt.mark_running()
        self.store.update_attempt(attempt)
        self.event_bus.emit(session.session_id, "attempt.started", {"attempt_id": attempt.attempt_id})

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

            self.store.update_attempt(attempt)
            reply_metadata = {}
            if attempt.run_dir:
                reply_metadata["run_id"] = Path(attempt.run_dir).name
            reply_metadata["status"] = attempt.status.value
            if attempt.metrics:
                reply_metadata["metrics"] = attempt.metrics

            reply = Message(
                session_id=session.session_id, role="assistant",
                content=self._format_result_message(attempt),
                linked_attempt_id=attempt.attempt_id,
                metadata=reply_metadata,
            )
            self.store.append_message(reply)
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
                 "summary": attempt.summary, "error": attempt.error, "run_dir": attempt.run_dir,
                 "message_id": reply.message_id},
            )

        except Exception as exc:
            attempt.mark_failed(error=str(exc))
            self.store.update_attempt(attempt)
            self.event_bus.emit(session.session_id, "attempt.failed", {"attempt_id": attempt.attempt_id, "error": str(exc)})

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

        llm = ChatLLM()
        pm = PersistentMemory()

        session_id = attempt.session_id
        attempt_id = attempt.attempt_id
        loop = asyncio.get_running_loop()

        safe_overrides = sanitize_session_overrides(session_config) if session_config else session_config
        agent_config = load_runtime_agent_config(overrides=safe_overrides)

        def event_callback(event_type: str, data: Dict[str, Any]) -> None:
            """Forward AgentLoop events to the SSE event bus."""
            data["attempt_id"] = attempt_id
            self.event_bus.emit(session_id, event_type, data)

        def _mcp_collision_warn(msg: str) -> None:
            """Forward MCP server-name collision warnings to the operator event channel."""
            self.event_bus.emit(session_id, "mcp.warning", {"attempt_id": attempt_id, "message": msg})

        portfolio_analysis = dict((session_config or {}).get("portfolio_analysis") or {})
        portfolio_daily_run = dict((session_config or {}).get("portfolio_daily_run") or {})
        channel_policy = dict((session_config or {}).get("channel_policy") or {})
        if portfolio_daily_run.get("research_only"):
            registry = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: build_filtered_registry(_PORTFOLIO_DAILY_RUN_TOOL_NAMES, include_shell_tools=False),
            )
        elif channel_policy.get("research_only"):
            registry = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: build_filtered_registry(_CHANNEL_RESEARCH_TOOL_NAMES, include_shell_tools=False),
            )
        elif portfolio_analysis.get("research_only"):
            registry = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: build_filtered_registry(_PORTFOLIO_ANALYSIS_TOOL_NAMES, include_shell_tools=False),
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
            max_iterations=50,
            persistent_memory=pm,
        )
        self._active_loops[session_id] = agent

        # Build the message history context.
        history = self._convert_messages_to_history(messages) if messages else None

        try:
            result = await loop.run_in_executor(
                _AGENT_EXECUTOR,
                lambda: agent.run(
                    user_message=attempt.prompt,
                    history=history,
                    session_id=session_id,
                ),
            )
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
    def _format_result_message(attempt: Attempt) -> str:
        """Format the final execution result message."""
        if attempt.status == AttemptStatus.COMPLETED:
            return attempt.summary or "Strategy execution completed."
        if attempt.status == AttemptStatus.CANCELLED:
            return "Execution cancelled by user."
        return f"Execution failed: {attempt.error or 'unknown error'}"
