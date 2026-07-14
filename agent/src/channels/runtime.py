"""IM channel runtime that connects MessageBus traffic to SessionService."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.channels.bus.events import InboundMessage, OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.manager import ChannelManager
from src.channels.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command
from src.channels.research_sessions import (
    RESEARCH_CONTROL_NEW_CONVERSATION,
    RESEARCH_CONTROL_REFRESH_REPORT,
    build_research_session_route,
    friendly_route_name,
)
from src.channels.utils import safe_filename
from src.config.paths import get_data_dir
from src.portfolio.state import normalize_symbol
from src.session.models import Message, Session

logger = logging.getLogger(__name__)

_REPORT_DATE_ISO = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_REPORT_DATE_CN = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(\d{1,2})日")
_REPORT_TRAILING_DATE = re.compile(
    r"[\s:：_—-]*(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|20\d{2}年\d{1,2}月\d{1,2}日)\s*$"
)
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _shanghai_date(value: str | None = None) -> str:
    """Return an ISO date interpreted in the user's market timezone."""
    if not value:
        return datetime.now(_SHANGHAI_TZ).date().isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
    else:
        parsed = parsed.astimezone(_SHANGHAI_TZ)
    return parsed.date().isoformat()


def _shanghai_time(value: str | None) -> str:
    """Format a persisted timestamp as a compact Shanghai time label."""
    if not value:
        return "今天"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "今天"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
    else:
        parsed = parsed.astimezone(_SHANGHAI_TZ)
    return parsed.strftime("%H:%M")


def _render_research_pdf(title: str, content: str, target: Path) -> Path:
    """Persist a PDF using the same ReportLab renderer as the Web export."""
    from api_server import _render_pdf_reportlab

    payload = _render_pdf_reportlab(title, content)
    if not payload.startswith(b"%PDF-"):
        raise RuntimeError("PDF renderer returned an invalid document")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_bytes(payload)
    temp.replace(target)
    return target


def _report_title(report: str, fallback: str = "研究报告") -> str:
    """Extract the visible report title using the same contract as Web export."""
    lines = [line.strip() for line in str(report or "").splitlines() if line.strip()]
    heading = next((line for line in lines if re.match(r"^#{1,6}\s+\S", line)), "")
    if not heading:
        heading = next(
            (
                line
                for line in lines
                if 4 <= len(line) <= 120
                and re.search(r"(?:分析|报告|复盘|研究|策略|展望|总结)$", line)
            ),
            "",
        )
    title = re.sub(r"^#{1,6}\s+", "", heading).strip()
    title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", title, flags=re.UNICODE)
    title = re.sub(r"(?:\*\*|__|`)+$", "", title).strip()
    title = re.sub(r"^20\d{2}-\d{2}-\d{2}[_\s-]+", "", title)
    title = _REPORT_TRAILING_DATE.sub("", title).strip(" _-—:：")
    return title or fallback.strip() or "研究报告"


def _report_date(report: str) -> str:
    """Return the report date in local ISO form, falling back to Shanghai today."""
    text = str(report or "")
    match = _REPORT_DATE_ISO.search(text) or _REPORT_DATE_CN.search(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            pass
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _report_pdf_name(report: str, label: str = "研究报告") -> str:
    """Build a Unicode-safe ``date + report title`` attachment filename."""
    stem = safe_filename(f"{_report_date(report)}_{_report_title(report, label)}")
    stem = re.sub(r"\s+", " ", stem).rstrip(". ")[:160] or "research_report"
    return f"{stem}.pdf"


def _looks_like_report(content: str) -> bool:
    """Conservatively identify a substantial assistant report."""
    text = str(content or "").strip()
    if len(text) < 600:
        return False
    has_title = bool(re.search(r"(?m)^#{1,6}\s+\S", text))
    sections = len(re.findall(r"(?m)^##\s+", text))
    tables = len(re.findall(r"(?m)^\s*\|.+\|\s*$", text))
    return has_title and (sections >= 2 or tables >= 4)


def _research_delivery_note(label: str, report: str) -> str:
    """Build the short Feishu companion text for a PDF research report."""
    title = ""
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break

    lines = [f"✅ {label}已完成"]
    if title and title != label:
        lines.append(f"报告：{title}")
    lines.append("完整正文和表格见 PDF 附件；飞书中仅保留这条简要说明。")
    lines.append("后续可直接发送文字，在当前专题 Session 中继续讨论。")
    return "\n".join(lines)


def _is_completed_feishu_research(
    msg: InboundMessage,
    attempt_id: str | None,
    reply: Message,
) -> bool:
    """Return whether this reply must use the Feishu PDF-report delivery mode."""
    metadata = dict(msg.metadata or {})
    reply_status = str((reply.metadata or {}).get("status") or "completed")
    return bool(
        msg.channel == "feishu"
        and (metadata.get("research_action") or metadata.get("pdf_delivery_requested"))
        and attempt_id
        and reply.content.strip()
        and reply_status == "completed"
    )


@dataclass
class ChannelRuntimeConfig:
    """Runtime controls for IM channel processing."""

    reply_timeout_s: float = 600.0
    poll_interval_s: float = 0.25


class ChannelRuntime:
    """Route inbound channel messages into Vibe-Trading sessions."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        session_service: Any,
        manager: ChannelManager | None,
        dispatcher: Any | None = None,
        daily_run_service: Any | None = None,
        daily_schedule_store: Any | None = None,
        session_map_path: Path | None = None,
        reply_timeout_s: float = 600.0,
        poll_interval_s: float = 0.25,
    ) -> None:
        self.bus = bus
        self.session_service = session_service
        self.manager = manager
        self.dispatcher = dispatcher
        self.daily_run_service = daily_run_service
        self.daily_schedule_store = daily_schedule_store
        self.config = ChannelRuntimeConfig(
            reply_timeout_s=reply_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        self.session_map_path = session_map_path or (get_data_dir() / "channels" / "sessions.json")
        self._session_map: dict[str, str] = {}
        self._consumer_task: asyncio.Task[None] | None = None
        self._manager_task: asyncio.Task[Any] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._daily_runs_by_chat: dict[str, str] = {}

    async def start(self, *, start_manager: bool = True) -> None:
        """Start channel processing and, optionally, platform adapters."""
        if self._running:
            return
        self._session_map = self._load_session_map()
        self._running = True
        if start_manager and self.manager is not None:
            self._manager_task = asyncio.create_task(self.manager.start_all())
            await asyncio.sleep(0)
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        """Stop channel processing and platform adapters."""
        self._running = False
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        for task in list(self._handler_tasks):
            task.cancel()
        for task in list(self._handler_tasks):
            with suppress(asyncio.CancelledError):
                await task
        self._handler_tasks.clear()
        if self.manager is not None:
            await self.manager.stop_all()
        if self._manager_task is not None:
            with suppress(asyncio.CancelledError):
                await self._manager_task
            self._manager_task = None

    def status(self) -> dict[str, Any]:
        """Return runtime and channel status."""
        return {
            "running": self._running,
            "inbound_queue": self.bus.inbound_size,
            "outbound_queue": self.bus.outbound_size,
            "session_count": len(self._session_map),
            "channels": self.manager.get_status() if self.manager is not None else {},
        }

    async def _consume_loop(self) -> None:
        while True:
            msg = await self.bus.consume_inbound()
            task = asyncio.create_task(self._handle_inbound(msg))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _handle_inbound(self, msg: InboundMessage) -> None:
        try:
            if self._is_pairing_command(msg.content):
                reply = handle_pairing_command(msg.channel, self._pairing_subcommand_text(msg.content))
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=reply,
                        metadata=self._response_metadata(
                            msg,
                            **{PAIRING_COMMAND_META_KEY: True},
                        ),
                    )
                )
                return

            research_control = str((msg.metadata or {}).get("research_control") or "")
            if research_control == RESEARCH_CONTROL_NEW_CONVERSATION:
                await self._start_new_conversation(msg)
                return
            if research_control == RESEARCH_CONTROL_REFRESH_REPORT:
                refreshed = await self._prepare_refresh_report(msg)
                if refreshed is None:
                    return
                msg = refreshed

            if self._is_new_session_command(msg.content):
                await self._start_new_conversation(msg)
                return

            if self._is_status_command(msg.content):
                await self._publish_status(msg)
                return

            if self._is_cancel_command(msg.content):
                await self._cancel_session(msg)
                return

            if self._is_sessions_command(msg.content):
                await self._publish_sessions(msg)
                return

            if str((msg.metadata or {}).get("stock_research_action") or ""):
                prepared = await self._handle_stock_research_action(msg)
                if prepared is None:
                    return
                msg = prepared

            if bool((msg.metadata or {}).get("pdf_from_previous_report")):
                await self._send_previous_report_pdf(msg)
                return

            if str((msg.metadata or {}).get("daily_report_action") or ""):
                await self._handle_daily_report_action(msg)
                return

            if bool((msg.metadata or {}).get("daily_portfolio_run")):
                await self._run_daily_portfolio(msg)
                return

            session_id = self._session_for(msg)
            if bool((msg.metadata or {}).get("research_activate")):
                self._activate_research_session(msg, session_id)
            if self.dispatcher is not None:
                result = await self.dispatcher.submit(
                    session_id,
                    msg.content,
                    source=msg.channel,
                    source_metadata={
                        "channel_session_key": msg.session_key,
                        "channel_chat_id": msg.chat_id,
                        **dict(msg.metadata or {}),
                    },
                    include_shell_tools=False,
                )
            else:
                result = await self.session_service.send_message(
                    session_id,
                    msg.content,
                    include_shell_tools=False,
                )
            attempt_id = result.get("attempt_id") if isinstance(result, dict) else None
            reply = await self._wait_for_reply(session_id, attempt_id)
            media = await self._research_pdf_media(
                msg,
                session_id=session_id,
                attempt_id=attempt_id,
                reply=reply,
            )
            is_research_report = _is_completed_feishu_research(msg, attempt_id, reply)
            is_pdf_report = is_research_report and bool(media)
            label = str((msg.metadata or {}).get("research_label") or "研究报告").strip()
            if is_pdf_report:
                outbound_content = _research_delivery_note(label, reply.content)
            elif is_research_report:
                outbound_content = (
                    f"⚠️ {label}已完成，但 PDF 生成失败。\n"
                    "为避免在飞书中发送大量表格正文，本次未发送报告内容；请重试生成报告。"
                )
            else:
                outbound_content = reply.content
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=outbound_content,
                    media=media,
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        attempt_id=attempt_id,
                        session_id=session_id,
                        **({"delivery_mode": "report_pdf"} if is_pdf_report else {}),
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - channel errors must surface to users
            logger.exception("Channel runtime failed for %s:%s", msg.channel, msg.chat_id)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Channel runtime error: {type(exc).__name__}: {exc}",
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        error=True,
                    ),
                )
            )

    @staticmethod
    def _base_session_key(msg: InboundMessage) -> str:
        return str(
            (msg.metadata or {}).get("research_base_session_key") or msg.session_key
        )

    def _active_route_key(self, base_key: str, session_id: str | None) -> str | None:
        if not session_id:
            return None
        prefix = f"{base_key}:research:"
        return next(
            (
                key
                for key, candidate_id in self._session_map.items()
                if key.startswith(prefix) and candidate_id == session_id
            ),
            None,
        )

    def _activate_research_session(self, msg: InboundMessage, session_id: str) -> None:
        metadata = dict(msg.metadata or {})
        base_key = str(metadata.get("research_base_session_key") or "")
        route_key = str(metadata.get("research_route_key") or msg.session_key)
        if not base_key or not route_key:
            return
        previous_id = self._session_map.get(base_key)
        if previous_id and previous_id != session_id:
            previous_route = self._active_route_key(base_key, previous_id)
            if previous_route is None:
                self._session_map[f"{base_key}:general"] = previous_id
        self._session_map[route_key] = session_id
        self._session_map[base_key] = session_id
        self._save_session_map()

    def _is_session_busy(self, session_id: str) -> bool:
        get_session = getattr(self.session_service, "get_session", None)
        session = get_session(session_id) if callable(get_session) else None
        attempt_id = getattr(session, "last_attempt_id", None)
        store = getattr(self.session_service, "store", None)
        get_attempt = getattr(store, "get_attempt", None)
        if not attempt_id or not callable(get_attempt):
            return False
        attempt = get_attempt(session_id, attempt_id)
        status = getattr(getattr(attempt, "status", None), "value", None)
        return status in {"pending", "running", "waiting_user"}

    async def _publish_research_control(
        self,
        msg: InboundMessage,
        content: str,
        *,
        open_hub: bool = False,
        **metadata: Any,
    ) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=self._response_metadata(
                    msg,
                    _channel_runtime=True,
                    _research_hub=open_hub,
                    research_base_session_key=self._base_session_key(msg),
                    **metadata,
                ),
            )
        )

    async def _start_new_conversation(self, msg: InboundMessage) -> None:
        """Replace only the active logical topic with a blank Session."""
        base_key = self._base_session_key(msg)
        current_id = self._session_map.get(base_key)
        current_route = self._active_route_key(base_key, current_id)
        current = (
            self.session_service.get_session(current_id)
            if current_id and callable(getattr(self.session_service, "get_session", None))
            else None
        )
        route_key = current_route or f"{base_key}:general"
        if current is not None:
            title = str(getattr(current, "title", "") or "飞书·通用对话")
            config = dict(getattr(current, "config", {}) or {})
        else:
            title = "飞书·通用对话" if msg.channel == "feishu" else f"{msg.channel}:{msg.chat_id}"
            config = {
                "channel": msg.channel,
                "channel_chat_id": msg.chat_id,
                "channel_session_key": route_key,
                "channel_policy": {
                    "research_only": True,
                    "allow_shell_tools": False,
                    "allow_trading_tools": False,
                },
            }
        config["channel_session_key"] = route_key
        created = self.session_service.create_session(title=title, config=config)
        new_id = _session_id(created)
        self._session_map[route_key] = new_id
        self._session_map[base_key] = new_id
        self._save_session_map()
        research = dict(config.get("research_session") or {})
        label = str(research.get("label") or friendly_route_name(base_key, route_key))
        await self._publish_research_control(
            msg,
            f"✅ 已为{label}开启新对话，其他专题 Session 保持不变。",
            session_reset=True,
            previous_session_id=current_id,
            session_id=new_id,
            research_route_key=route_key,
        )

    async def _prepare_refresh_report(
        self, msg: InboundMessage
    ) -> InboundMessage | None:
        """Turn the refresh control into a normal report attempt for the active topic."""
        from src.portfolio.analysis import (
            build_analysis_prompt,
            build_custom_stock_prompt,
            find_holding,
        )
        from src.portfolio.state import load_state

        base_key = self._base_session_key(msg)
        current_id = self._session_map.get(base_key)
        current = (
            self.session_service.get_session(current_id)
            if current_id and callable(getattr(self.session_service, "get_session", None))
            else None
        )
        research = dict(getattr(current, "config", {}).get("research_session") or {}) if current else {}
        if not current_id or not research:
            await self._publish_research_control(
                msg,
                "请先从分析菜单选择盘前、持仓或个股专题。",
                open_hub=True,
            )
            return None
        if self._is_session_busy(current_id):
            await self._publish_research_control(
                msg,
                "⏳ 当前专题的报告仍在生成，请完成后再更新。",
                refresh_rejected=True,
            )
            return None

        kind = str(research.get("kind") or "")
        symbol = str(research.get("symbol") or "")
        if kind == "portfolio":
            action = "portfolio"
            prompt = build_analysis_prompt("portfolio")
            route = build_research_session_route(base_key=base_key, action=action)
        elif kind == "premarket":
            action = "premarket"
            prompt = build_analysis_prompt("market", market_phase="premarket")
            route = await asyncio.to_thread(
                build_research_session_route,
                base_key=base_key,
                action=action,
            )
        elif kind == "symbol" and symbol:
            state = load_state()
            holding = find_holding(state.holdings, symbol)
            if holding is not None:
                action = "holding"
                prompt = build_analysis_prompt("holding", holding)
                name = str(holding.get("name") or symbol)
            else:
                action = "custom_stock"
                prompt = build_custom_stock_prompt(symbol[:6])
                name = symbol
            route = build_research_session_route(
                base_key=base_key,
                action=action,
                symbol=symbol,
                name=name,
            )
        else:
            await self._publish_research_control(
                msg,
                "当前专题缺少可刷新的研究信息，请重新选择分析专题。",
                open_hub=True,
            )
            return None

        metadata = dict(msg.metadata or {})
        metadata.pop("research_control", None)
        metadata.update(
            {
                "research_action": action,
                "research_label": route.label,
                "research_refresh": True,
                **route.metadata(),
            }
        )
        return replace(
            msg,
            content=prompt,
            metadata=metadata,
            session_key_override=route.route_key,
        )

    def _latest_report_for_chat(self, msg: InboundMessage) -> tuple[str, Message] | None:
        """Find the newest substantial assistant report from the same Feishu chat."""
        candidate_ids: list[str] = []

        def add_candidate(session_id: str | None) -> None:
            if session_id and session_id not in candidate_ids:
                candidate_ids.append(session_id)

        add_candidate(self._session_map.get(msg.session_key))
        for key, session_id in reversed(list(self._session_map.items())):
            if key == msg.session_key or key.startswith(f"{msg.session_key}:"):
                add_candidate(session_id)

        list_sessions = getattr(self.session_service, "list_sessions", None)
        if callable(list_sessions):
            for session in list_sessions(limit=200):
                config = dict(getattr(session, "config", {}) or {})
                if (
                    config.get("channel") == msg.channel
                    and config.get("channel_chat_id") == msg.chat_id
                ):
                    add_candidate(str(getattr(session, "session_id", "") or ""))

        newest: tuple[str, Message] | None = None
        for session_id in candidate_ids:
            for message in reversed(self.session_service.get_messages(session_id, limit=200)):
                if message.role != "assistant" or not _looks_like_report(message.content):
                    continue
                if newest is None or message.created_at > newest[1].created_at:
                    newest = (session_id, message)
                break
        return newest

    async def _send_previous_report_pdf(self, msg: InboundMessage) -> None:
        """Send the newest complete report from this chat without invoking the model."""
        found = self._latest_report_for_chat(msg)
        if found is None:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="当前聊天中没有找到可发送的完整报告。请先生成报告，或说明报告标题。",
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        pdf_report_missing=True,
                    ),
                )
            )
            return

        source_session_id, report = found
        title = _report_title(report.content)
        safe_session_id = re.sub(r"[^A-Za-z0-9_-]+", "_", source_session_id)[:80] or "session"
        target = (
            self.session_map_path.parent
            / "reports"
            / safe_session_id
            / _report_pdf_name(report.content, title)
        )
        try:
            rendered = await asyncio.to_thread(
                _render_research_pdf,
                f"{_report_date(report.content)}_{title}",
                report.content,
                target,
            )
        except Exception:
            logger.exception("Unable to render previous Feishu report from %s", source_session_id)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"⚠️ 已找到《{title}》，但 PDF 生成失败，请稍后重试。",
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        pdf_render_failed=True,
                        report_source_session_id=source_session_id,
                    ),
                )
            )
            return

        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "✅ PDF 已生成\n"
                    f"报告：{title}\n"
                    "完整正文和表格见 PDF 附件；后续可直接在当前聊天继续讨论。"
                ),
                media=[str(rendered)],
                metadata=self._response_metadata(
                    msg,
                    _channel_runtime=True,
                    delivery_mode="report_pdf",
                    report_source_session_id=source_session_id,
                ),
            )
        )

    async def _research_pdf_media(
        self,
        msg: InboundMessage,
        *,
        session_id: str,
        attempt_id: str | None,
        reply: Message,
    ) -> list[str]:
        """Render completed Feishu research answers and return the PDF path."""
        if not _is_completed_feishu_research(msg, attempt_id, reply):
            return []

        metadata = dict(msg.metadata or {})

        safe_session_id = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id)[:80] or "session"
        target = (
            self.session_map_path.parent
            / "reports"
            / safe_session_id
            / _report_pdf_name(reply.content, str(metadata.get("research_label") or "研究报告"))
        )
        label = str(metadata.get("research_label") or "研究报告").strip()
        try:
            rendered = await asyncio.to_thread(
                _render_research_pdf,
                f"{_report_date(reply.content)}_{_report_title(reply.content, label)}",
                reply.content,
                target,
            )
            return [str(rendered)]
        except Exception:
            logger.exception(
                "Unable to render Feishu research PDF for session %s attempt %s",
                session_id,
                attempt_id,
            )
            return []

    @staticmethod
    def _session_stock_symbol(session: Any) -> str:
        config = dict(getattr(session, "config", {}) or {})
        research = dict(config.get("research_session") or {})
        daily = dict(config.get("portfolio_daily_run") or {})
        raw = research.get("symbol") or daily.get("symbol") or ""
        return normalize_symbol(str(raw)).upper() if raw else ""

    def _stock_session_info(
        self,
        session_id: str,
        *,
        symbol: str,
        today: str,
        msg: InboundMessage,
        mapped_route: bool = False,
    ) -> dict[str, Any] | None:
        get_session = getattr(self.session_service, "get_session", None)
        session = get_session(session_id) if callable(get_session) else None
        if session is None or _shanghai_date(getattr(session, "created_at", None)) != today:
            return None
        normalized = normalize_symbol(symbol).upper()
        session_symbol = self._session_stock_symbol(session)
        if session_symbol != normalized:
            if not mapped_route or not msg.session_key.endswith(f":symbol:{normalized}"):
                return None
        config = dict(getattr(session, "config", {}) or {})
        daily = dict(config.get("portfolio_daily_run") or {})
        if not daily and not mapped_route:
            if config.get("channel") != msg.channel or config.get("channel_chat_id") != msg.chat_id:
                return None
        return {
            "session_id": session_id,
            "title": str(getattr(session, "title", "") or "个股研究 Session"),
            "created_at": str(getattr(session, "created_at", "") or ""),
            "created_time": _shanghai_time(getattr(session, "created_at", None)),
            "source": "daily_report" if daily else "research",
        }

    def _today_stock_session(
        self, msg: InboundMessage, symbol: str, *, today: str
    ) -> dict[str, Any] | None:
        candidate_ids: list[tuple[str, bool]] = []

        def add(session_id: str | None, mapped: bool = False) -> None:
            if session_id and not any(item[0] == session_id for item in candidate_ids):
                candidate_ids.append((session_id, mapped))

        add(self._session_map.get(msg.session_key), True)
        list_sessions = getattr(self.session_service, "list_sessions", None)
        if callable(list_sessions):
            for session in list_sessions(limit=500):
                add(str(getattr(session, "session_id", "") or ""))

        for session_id, mapped in candidate_ids:
            info = self._stock_session_info(
                session_id,
                symbol=symbol,
                today=today,
                msg=msg,
                mapped_route=mapped,
            )
            if info:
                return info
        return None

    def _today_stock_report(
        self,
        symbol: str,
        *,
        today: str,
        run_id: str = "",
        artifact_id: str = "",
    ) -> dict[str, Any] | None:
        if self.daily_run_service is None:
            return None
        store = getattr(self.daily_run_service, "store", None)
        if store is None:
            return None
        if run_id:
            record = self.daily_run_service.get_run(run_id)
            records = [record] if record else []
        else:
            list_runs = getattr(store, "list", None)
            records = list_runs(limit=500) if callable(list_runs) else []
        normalized = normalize_symbol(symbol).upper()
        for record in records:
            if not isinstance(record, dict):
                continue
            created_today = _shanghai_date(str(record.get("created_at") or "")) == today
            if str(record.get("market_date") or "") != today and not created_today:
                continue
            if record.get("status") not in {"completed", "completed_with_warnings"}:
                continue
            if record.get("stage") == "skipped_data_unavailable":
                continue
            artifacts = list(record.get("artifacts") or [])
            pdf = next(
                (
                    item
                    for item in artifacts
                    if item.get("kind") == "holding_daily_pdf"
                    and normalize_symbol(str(item.get("symbol") or "")).upper() == normalized
                    and not item.get("expired")
                    and not item.get("superseded")
                    and (not artifact_id or item.get("artifact_id") == artifact_id)
                ),
                None,
            )
            if not pdf:
                continue
            markdown = next(
                (
                    item
                    for item in artifacts
                    if item.get("kind") == "holding_daily_markdown"
                    and normalize_symbol(str(item.get("symbol") or "")).upper() == normalized
                    and not item.get("expired")
                    and not item.get("superseded")
                ),
                None,
            )
            worker = next(
                (
                    item
                    for item in record.get("workers") or []
                    if normalize_symbol(str(item.get("symbol") or "")).upper() == normalized
                ),
                {},
            )
            return {
                "run_id": str(record.get("run_id") or ""),
                "market_date": str(record.get("market_date") or today),
                "revision": int(pdf.get("revision") or record.get("revision") or 1),
                "artifact_id": str(pdf.get("artifact_id") or ""),
                "filename": str(pdf.get("filename") or ""),
                "markdown_artifact_id": str((markdown or {}).get("artifact_id") or ""),
                "worker_session_id": str(worker.get("session_id") or ""),
                "created_at": str(record.get("created_at") or ""),
            }
        return None

    async def _seed_stock_report_context(
        self,
        session_id: str,
        *,
        symbol: str,
        report: dict[str, Any],
    ) -> None:
        run_id = str(report.get("run_id") or "")
        messages = self.session_service.get_messages(session_id, limit=500)
        if any(
            (message.metadata or {}).get("daily_report_context_run_id") == run_id
            and (message.metadata or {}).get("daily_report_context_symbol") == symbol
            for message in messages
        ):
            return

        store = getattr(self.daily_run_service, "store", None)
        markdown = ""
        markdown_id = str(report.get("markdown_artifact_id") or "")
        if store is not None and markdown_id:
            resolved = store.resolve_artifact(run_id, markdown_id)
            if resolved:
                _, path = resolved
                try:
                    markdown = path.read_text(encoding="utf-8")
                except OSError:
                    logger.exception("Unable to read daily report context %s", path)
        if not markdown and store is not None:
            safe_symbol = re.sub(r"[^A-Za-z0-9._-]+", "_", symbol)[:40] or "holding"
            brief = store.read_json(run_id, f"outputs/holdings/{safe_symbol}/brief.json")
            if isinstance(brief, dict):
                markdown = json.dumps(brief, ensure_ascii=False, indent=2)
        context = (
            f"# 已载入今日个股报告：{symbol}\n\n"
            f"报告日期：{report.get('market_date')}\n"
            f"报告 revision：{report.get('revision')}\n\n"
            f"{markdown or '报告 PDF 已存在，但正文缓存不可用；回答前应明确说明这一限制。'}"
        )
        metadata = {
            "daily_report_context_run_id": run_id,
            "daily_report_context_symbol": symbol,
            "daily_report_context_revision": int(report.get("revision") or 1),
        }
        execute = getattr(self.session_service, "execute_message", None)
        if callable(execute):
            await execute(
                session_id,
                context,
                role="assistant",
                message_metadata=metadata,
            )
            return
        session_store = getattr(self.session_service, "store", None)
        append_message = getattr(session_store, "append_message", None)
        if callable(append_message):
            append_message(
                Message(
                    session_id=session_id,
                    role="assistant",
                    content=context,
                    metadata=metadata,
                )
            )

    def _activate_existing_stock_session(
        self, msg: InboundMessage, session_id: str
    ) -> None:
        self._ensure_channel_policy(session_id, msg)
        self._activate_research_session(msg, session_id)

    async def _report_continuation_session(
        self,
        msg: InboundMessage,
        *,
        symbol: str,
        report: dict[str, Any],
        requested_session_id: str = "",
    ) -> str:
        today = _shanghai_date()
        session_id = ""
        for candidate in (
            requested_session_id,
            str(report.get("worker_session_id") or ""),
        ):
            if candidate and self._stock_session_info(
                candidate,
                symbol=symbol,
                today=today,
                msg=msg,
                mapped_route=candidate == self._session_map.get(msg.session_key),
            ):
                session_id = candidate
                break
        if not session_id:
            existing = self._today_stock_session(msg, symbol, today=today)
            session_id = str((existing or {}).get("session_id") or "")
        if not session_id:
            self.reset_session(msg.session_key)
            session_id = self._session_for(msg)
        self._activate_existing_stock_session(msg, session_id)
        await self._seed_stock_report_context(
            session_id,
            symbol=normalize_symbol(symbol).upper(),
            report=report,
        )
        return session_id

    async def _handle_stock_research_action(
        self, msg: InboundMessage
    ) -> InboundMessage | None:
        metadata = dict(msg.metadata or {})
        action = str(metadata.get("stock_research_action") or "")
        symbol = normalize_symbol(str(metadata.get("research_symbol") or "")).upper()
        if not symbol:
            raise ValueError("无法识别要分析的证券代码。")
        today = _shanghai_date()

        if action == "prepare":
            session = self._today_stock_session(msg, symbol, today=today)
            report = self._today_stock_report(symbol, today=today)
            if session or report:
                label = str(metadata.get("research_label") or symbol)
                name = label.removesuffix("个股分析").strip() or symbol
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            _stock_research_context=True,
                            stock_research_context={
                                "symbol": symbol,
                                "name": name,
                                "analysis_action": str(metadata.get("research_action") or "holding"),
                                "session": session or {},
                                "report": report or {},
                            },
                        ),
                    )
                )
                return None
            self.reset_session(msg.session_key)
            metadata.pop("stock_research_action", None)
            return replace(msg, metadata=metadata)

        if action == "start_stock_research":
            self.reset_session(msg.session_key)
            metadata.pop("stock_research_action", None)
            return replace(msg, metadata=metadata)

        if action == "resume_stock_session":
            session_id = str(metadata.get("stock_session_id") or "")
            info = self._stock_session_info(
                session_id,
                symbol=symbol,
                today=today,
                msg=msg,
                mapped_route=session_id == self._session_map.get(msg.session_key),
            )
            if not info:
                raise ValueError("今天的个股 Session 已不存在或不再匹配，请重新选择。")
            self._activate_existing_stock_session(msg, session_id)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=(
                        f"已切换到今天的 {symbol} Session：`{session_id}`。\n"
                        "现在直接发送问题即可接着聊，不会重新生成报告。"
                    ),
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        session_id=session_id,
                        stock_session_resumed=True,
                    ),
                )
            )
            return None

        if action in {"send_stock_daily_report", "continue_stock_daily_report"}:
            report = self._today_stock_report(
                symbol,
                today=today,
                run_id=str(metadata.get("daily_run_id") or ""),
                artifact_id=str(metadata.get("artifact_id") or ""),
            )
            if not report:
                raise ValueError("今天的个股日报已不存在、过期或已被新 revision 替代。")
            session_id = await self._report_continuation_session(
                msg,
                symbol=symbol,
                report=report,
                requested_session_id=str(metadata.get("stock_session_id") or ""),
            )
            media: list[str] = []
            if action == "send_stock_daily_report":
                resolved = self.daily_run_service.store.resolve_artifact(
                    str(report["run_id"]), str(report["artifact_id"])
                )
                if not resolved:
                    raise ValueError("今天的个股日报 PDF 已不存在或已过期。")
                _, path = resolved
                media = [str(path)]
                content = (
                    f"已发送 {symbol} 今日个股日报（revision {report['revision']}）。\n"
                    "报告正文也已载入当前 Session，接下来可直接针对报告提问。"
                )
            else:
                content = (
                    f"已载入 {symbol} 今日个股日报（revision {report['revision']}）。\n"
                    "现在直接发送要补充研究的问题即可；不会重复生成原报告。"
                )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    media=media,
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        session_id=session_id,
                        daily_run_id=str(report["run_id"]),
                        delivery_mode="report_pdf" if media else "report_context",
                        stock_report_continuation=True,
                    ),
                )
            )
            return None

        raise ValueError("不支持的个股研究衔接操作。")

    def _session_for(self, msg: InboundMessage) -> str:
        key = msg.session_key
        existing = self._session_map.get(key)
        if existing:
            self._ensure_channel_policy(existing, msg)
            return existing
        metadata = dict(msg.metadata or {})
        title = str(metadata.get("research_session_title") or f"{msg.channel}:{msg.chat_id}")
        config: dict[str, Any] = {
            "channel": msg.channel,
            "channel_chat_id": msg.chat_id,
            "channel_session_key": key,
            "channel_policy": {
                "research_only": True,
                "allow_shell_tools": False,
                "allow_trading_tools": False,
            },
        }
        if metadata.get("research_route_key"):
            config["research_session"] = {
                "base_key": str(metadata.get("research_base_session_key") or ""),
                "route_key": str(metadata.get("research_route_key") or key),
                "kind": str(metadata.get("research_session_kind") or ""),
                "action": str(metadata.get("research_action") or ""),
                "label": str(metadata.get("research_label") or "研究专题"),
                "title": title,
                "symbol": str(metadata.get("research_symbol") or ""),
                "target_date": str(metadata.get("research_target_date") or ""),
            }
        session = self.session_service.create_session(
            title=title,
            config=config,
        )
        session_id = _session_id(session)
        self._session_map[key] = session_id
        self._save_session_map()
        return session_id

    def _ensure_channel_policy(self, session_id: str, msg: InboundMessage) -> None:
        """Upgrade persisted channel sessions to the enforced research policy."""
        get_session = getattr(self.session_service, "get_session", None)
        if not callable(get_session):
            return
        session = get_session(session_id)
        if session is None:
            return
        config = dict(getattr(session, "config", {}) or {})
        policy = dict(config.get("channel_policy") or {})
        expected = {
            "research_only": True,
            "allow_shell_tools": False,
            "allow_trading_tools": False,
        }
        changed = policy != expected
        config.update(
            {
                "channel": msg.channel,
                "channel_chat_id": msg.chat_id,
                "channel_session_key": msg.session_key,
                "channel_policy": expected,
            }
        )
        metadata = dict(msg.metadata or {})
        if metadata.get("research_route_key"):
            research_session = {
                "base_key": str(metadata.get("research_base_session_key") or ""),
                "route_key": str(metadata.get("research_route_key") or msg.session_key),
                "kind": str(metadata.get("research_session_kind") or ""),
                "action": str(metadata.get("research_action") or ""),
                "label": str(metadata.get("research_label") or "研究专题"),
                "title": str(metadata.get("research_session_title") or session.title),
                "symbol": str(metadata.get("research_symbol") or ""),
                "target_date": str(metadata.get("research_target_date") or ""),
            }
            if config.get("research_session") != research_session:
                config["research_session"] = research_session
                changed = True
        if not changed:
            return
        session.config = config
        store = getattr(self.session_service, "store", None)
        update_session = getattr(store, "update_session", None)
        if callable(update_session):
            update_session(session)

    async def _publish_status(self, msg: InboundMessage) -> None:
        base_key = self._base_session_key(msg)
        session_id = self._session_map.get(base_key)
        if not session_id:
            content = "当前没有活动 Session。"
        else:
            session = self.session_service.get_session(session_id)
            attempt = None
            store = getattr(self.session_service, "store", None)
            if session is not None and getattr(session, "last_attempt_id", None) and store is not None:
                attempt = store.get_attempt(session_id, session.last_attempt_id)
            status = getattr(getattr(attempt, "status", None), "value", None) or "idle"
            route_key = self._active_route_key(base_key, session_id) or base_key
            label = friendly_route_name(base_key, route_key)
            research = dict(getattr(session, "config", {}).get("research_session") or {}) if session else {}
            label = str(research.get("label") or label)
            content = f"当前专题：{label}\nSession：{session_id}\n状态：{status}"
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=self._response_metadata(msg, _channel_runtime=True, session_status=True),
            )
        )

    async def _cancel_session(self, msg: InboundMessage) -> None:
        daily_run_id = self._daily_runs_by_chat.get(self._base_session_key(msg))
        if daily_run_id and self.daily_run_service is not None:
            record = await self.daily_run_service.cancel(daily_run_id)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"已请求取消组合晨会：{record.get('run_id')}",
                    metadata=self._response_metadata(
                        msg, _channel_runtime=True, daily_run_cancel=True, _progress=True
                    ),
                )
            )
            return
        session_id = self._session_map.get(self._base_session_key(msg))
        if not session_id:
            content = "No active session for this chat or topic."
        elif self.dispatcher is None:
            running = bool(self.session_service.cancel_current(session_id))
            content = "Cancellation requested." if running else "No queued or running task."
        else:
            result = await self.dispatcher.cancel_session(session_id)
            running = bool(result.get("running"))
            queued = int(result.get("queued", 0))
            content = (
                f"Cancellation requested: running={int(running)}, queued={queued}."
                if running or queued
                else "No queued or running task."
            )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=self._response_metadata(msg, _channel_runtime=True, session_cancel=True),
            )
        )

    async def _publish_sessions(self, msg: InboundMessage) -> None:
        prefix = self._base_session_key(msg)
        active_id = self._session_map.get(prefix)
        rows = [
            (key, session_id)
            for key, session_id in self._session_map.items()
            if key != prefix
            and (key == f"{prefix}:general" or key.startswith(f"{prefix}:research:"))
        ]
        if active_id and not any(session_id == active_id for _, session_id in rows):
            rows.insert(0, (prefix, active_id))
        if not rows:
            content = "当前聊天还没有专题 Session。"
        else:
            lines = ["可恢复的专题 Session："]
            for key, session_id in rows[-20:]:
                marker = " ← 当前" if session_id == active_id else ""
                lines.append(
                    f"- {friendly_route_name(prefix, key)} · {session_id}{marker}"
                )
            content = "\n".join(lines)
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=self._response_metadata(msg, _channel_runtime=True, session_list=True),
            )
        )

    @staticmethod
    def _response_metadata(msg: InboundMessage, **updates: Any) -> dict[str, Any]:
        """Keep adapter routing metadata on every runtime response."""
        return {**dict(msg.metadata or {}), **updates}

    async def _run_daily_portfolio(self, msg: InboundMessage) -> None:
        """Run the report-oriented portfolio pipeline outside chat Session routing."""
        if self.daily_run_service is None:
            raise RuntimeError("组合晨会服务尚未启用")
        if self.daily_schedule_store is not None:
            self.daily_schedule_store.remember_delivery_target(
                channel=msg.channel,
                chat_id=msg.chat_id,
                chat_type=str((msg.metadata or {}).get("chat_type") or "p2p"),
                session_key=self._base_session_key(msg),
            )
        record = await self.daily_run_service.start(
            refresh_policy="ensure_fresh",
            report_profile="master_with_holding_appendices",
            trigger=msg.channel,
        )
        if (
            record.get("deduplicated") is True
            and record.get("trigger") == "scheduled_0912"
        ):
            run_id = str(record.get("run_id") or "")
            self._daily_runs_by_chat[self._base_session_key(msg)] = run_id
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=(
                        f"09:12 自动组合晨会 `{run_id}` 已在处理同一份输入，"
                        "本次不再重复生成或发送报告。"
                    ),
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        daily_run_id=run_id,
                        scheduled_daily_reused=True,
                    ),
                )
            )
            return
        await self._monitor_daily_portfolio(msg, record)

    async def deliver_scheduled_daily(
        self,
        record: dict[str, Any] | None,
        job: dict[str, Any],
        target: dict[str, str],
    ) -> None:
        """Synchronously deliver one terminal scheduled result."""

        if self.manager is None:
            raise RuntimeError("channel manager is not available")
        channel = str(target.get("channel") or "feishu")
        chat_id = str(target.get("chat_id") or "")
        if not chat_id:
            raise RuntimeError("scheduled delivery chat is not configured")
        if self._manager_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._manager_task), timeout=30.0
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"channel manager did not become ready for {channel}"
                ) from exc
        chat_type = str(target.get("chat_type") or "p2p")
        session_key = str(target.get("session_key") or f"{channel}:{chat_id}")
        run_id = str((record or {}).get("run_id") or job.get("run_id") or "")
        common_metadata = {
            "_channel_runtime": True,
            "chat_type": chat_type,
            "research_base_session_key": session_key,
            "daily_run_id": run_id,
            "daily_run_revision": int((record or {}).get("revision") or 1),
            "scheduled_portfolio_daily": True,
        }

        if not record or record.get("status") not in {
            "completed",
            "completed_with_warnings",
        }:
            error = str(
                (record or {}).get("error")
                or job.get("error")
                or "晨会未生成报告"
            )
            await self.manager.send_direct(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=(
                        "09:12 自动组合晨会生成失败。\n"
                        f"运行：{run_id or '未创建'}\n"
                        f"原因：{error}\n"
                        "请检查持仓或数据状态后手工重试。"
                    ),
                    metadata={**common_metadata, "portfolio_daily_failed": True},
                )
            )
            return

        if record.get("stage") == "skipped_data_unavailable":
            gate = record.get("analysis_gate") or {}
            await self.manager.send_direct(
                OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=(
                        "09:12 自动组合晨会因关键数据覆盖不足而停止，未生成 PDF。\n"
                        f"覆盖：{gate.get('eligible_count', 0)}/{gate.get('total_count', 0)}"
                    ),
                    metadata={**common_metadata, "portfolio_daily_skipped": True},
                )
            )
            return

        master = next(
            (
                item
                for item in record.get("artifacts") or []
                if item.get("kind") == "master_pdf"
                and not item.get("expired")
                and not item.get("superseded")
            ),
            None,
        )
        if master is None or not master.get("path"):
            raise RuntimeError("scheduled report completed without a master PDF")
        counts = record.get("summary") or {}
        holding_reports = self._daily_report_options(record)
        decision_text = self._daily_decision_text(record)
        content = (
            f"✅ 09:12 自动组合晨会已完成｜{record.get('market_date')}｜"
            f"revision {record.get('revision', 1)}\n"
            f"退出 {counts.get('exit', 0)} · 减仓 {counts.get('reduce', 0)} · "
            f"加仓 {counts.get('add', 0)} · 观察 {counts.get('observe', 0)}\n"
            f"个股 PDF {len(holding_reports)} 份。综合报告已附上。"
            f"{f'{chr(10)}{decision_text}' if decision_text else ''}"
        )
        await self.manager.send_direct(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=content,
                media=[str(master["path"])],
                metadata={
                    **common_metadata,
                    "portfolio_daily_complete": True,
                    "holding_reports": holding_reports,
                    "delivery_mode": "report_pdf",
                },
            )
        )

    def _daily_report_options(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Return callback-safe holding report descriptors without local paths."""

        store = getattr(self.daily_run_service, "store", None)
        snapshot = (
            store.read_json(str(record["run_id"]), "inputs/portfolio_snapshot.json")
            if store is not None
            else None
        )
        names = {
            str(item.get("symbol") or item.get("code") or "").upper(): str(
                item.get("name") or item.get("symbol") or item.get("code") or ""
            )
            for item in (snapshot or {}).get("holdings") or []
        }
        workers = {
            str(item.get("symbol") or "").upper(): item
            for item in record.get("workers") or []
        }
        options: list[dict[str, Any]] = []
        for artifact in record.get("artifacts") or []:
            if (
                artifact.get("kind") != "holding_daily_pdf"
                or artifact.get("expired")
                or artifact.get("superseded")
            ):
                continue
            symbol = str(artifact.get("symbol") or "").upper()
            worker = workers.get(symbol) or {}
            options.append(
                {
                    "symbol": symbol,
                    "name": names.get(symbol) or symbol,
                    "artifact_id": str(artifact.get("artifact_id") or ""),
                    "filename": str(artifact.get("filename") or ""),
                    "revision": int(artifact.get("revision") or record.get("revision") or 1),
                    "status": str(worker.get("status") or "completed"),
                }
            )
        return options

    def _daily_decision_text(self, record: dict[str, Any]) -> str:
        store = getattr(self.daily_run_service, "store", None)
        decision = (
            store.read_json(str(record["run_id"]), "outputs/decision.json")
            if store is not None
            else None
        )
        if not isinstance(decision, dict):
            return ""
        lines: list[str] = []
        cash = decision.get("cash_summary") or {}
        if cash:
            actual = cash.get("actual_cash")
            minimum = cash.get("minimum_cash")
            lines.append(f"现金：实际 {actual if actual is not None else '未维护'} · 最低保留 {minimum}")
        for sleeve in (decision.get("sleeve_summaries") or [])[:4]:
            lines.append(
                f"{sleeve.get('name') or sleeve.get('id')}：当前 {sleeve.get('current_amount')} · "
                f"目标 {sleeve.get('target_amount')} · 缺口 {sleeve.get('gap_amount')}"
            )
        observations = decision.get("today_observation_points") or []
        if observations:
            lines.append("今日优先关注：")
            lines.extend(
                f"- {item.get('symbol')}：{item.get('watch_point')}"
                for item in observations[:3]
            )
        if not decision.get("quantitative_plan_enabled", False):
            lines.append("定量金额计划：已关闭（方向与观察结论仍保留）")
        return "\n".join(lines)

    async def _handle_daily_report_action(self, msg: InboundMessage) -> None:
        if self.daily_run_service is None:
            raise RuntimeError("组合晨会服务尚未启用")
        metadata = dict(msg.metadata or {})
        action = str(metadata.get("daily_report_action") or "")
        run_id = str(metadata.get("daily_run_id") or "")

        if action == "rerun_daily":
            rerun = await self.daily_run_service.start(
                refresh_policy="force",
                report_profile="master_with_holding_appendices",
                trigger=msg.channel,
                force_new=True,
            )
            await self._monitor_daily_portfolio(msg, rerun)
            return

        record = self.daily_run_service.get_run(run_id) if run_id else None
        if not record:
            raise ValueError("找不到这次组合晨会，报告可能已过期。")

        if action == "show_daily_reports":
            options = self._daily_report_options(record)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=(
                        "请选择要发送或重新分析的个股日报。"
                        if options
                        else "这次运行没有可发送的个股 PDF。"
                    ),
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        _daily_report_picker=True,
                        daily_run_id=run_id,
                        daily_run_revision=int(record.get("revision") or 1),
                        holding_reports=options,
                    ),
                )
            )
            return

        if action in {"send_daily_report", "send_daily_master"}:
            if action == "send_daily_master":
                artifact = next(
                    (
                        item
                        for item in record.get("artifacts") or []
                        if item.get("kind") == "master_pdf"
                        and not item.get("expired")
                        and not item.get("superseded")
                    ),
                    None,
                )
                artifact_id = str((artifact or {}).get("artifact_id") or "")
            else:
                artifact_id = str(metadata.get("artifact_id") or "")
            resolved = self.daily_run_service.store.resolve_artifact(run_id, artifact_id)
            if not resolved:
                raise ValueError("报告文件不存在、已过期或已失效。")
            artifact, path = resolved
            expected_kind = "master_pdf" if action == "send_daily_master" else "holding_daily_pdf"
            if artifact.get("kind") != expected_kind:
                raise ValueError("报告类型不匹配。")
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=(
                        f"已发送组合综合报告（revision {artifact.get('revision', 1)}）。"
                        if action == "send_daily_master"
                        else f"已发送 {artifact.get('symbol')} 个股日报（revision {artifact.get('revision', 1)}）。"
                    ),
                    media=[str(path)],
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        daily_run_id=run_id,
                        delivery_mode="report_pdf",
                    ),
                )
            )
            return

        if action == "retry_daily_holding":
            symbol = str(metadata.get("symbol") or "")
            retried = await self.daily_run_service.retry(run_id, symbol=symbol)
            await self._monitor_daily_portfolio(msg, retried)
            return

        if action == "cancel_daily_run":
            cancelled = await self.daily_run_service.cancel(run_id)
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"已请求取消组合晨会 `{run_id}`，当前状态：{cancelled.get('status')}。",
                    metadata=self._response_metadata(
                        msg, _channel_runtime=True, daily_run_id=run_id
                    ),
                )
            )
            return

        raise ValueError("不支持的组合晨会操作。")

    async def _monitor_daily_portfolio(
        self, msg: InboundMessage, record: dict[str, Any]
    ) -> None:
        """Stream one run's progress and deliver its immutable artifacts."""

        run_id = str(record["run_id"])
        key = self._base_session_key(msg)
        self._daily_runs_by_chat[key] = run_id
        retry_symbol = str(record.get("retry_symbol") or "")
        lead = (
            f"已启动 `{retry_symbol}` 个股重试"
            if retry_symbol
            else "已启动组合晨会"
        )
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"# 组合晨会\n\n{lead} `{run_id}`，正在冻结输入。可点击取消或发送 `/cancel`。",
                metadata=self._response_metadata(
                    msg,
                    _channel_runtime=True,
                    daily_run_id=run_id,
                    _stream_delta=True,
                    _stream_id=f"daily-run:{run_id}",
                ),
            )
        )
        completion: asyncio.Task[Any] | None = None
        try:
            if record.get("status") not in {"completed", "completed_with_warnings", "failed", "cancelled"}:
                completion = asyncio.create_task(self.daily_run_service.wait(run_id))
                last_progress = None
                while not completion.done():
                    current = self.daily_run_service.get_run(run_id)
                    if current:
                        progress = current.get("progress") or {}
                        signature = (
                            current.get("stage"),
                            progress.get("completed"),
                            progress.get("total"),
                            progress.get("percent"),
                        )
                        if signature != last_progress:
                            last_progress = signature
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=msg.channel,
                                    chat_id=msg.chat_id,
                                    content=(
                                        f"\n\n- 阶段：{current.get('stage')}"
                                        f"\n- 进度：{progress.get('completed', 0)}/{progress.get('total', 0)}"
                                        f"（{progress.get('percent', 0)}%）"
                                    ),
                                    metadata=self._response_metadata(
                                        msg,
                                        _channel_runtime=True,
                                        daily_run_id=run_id,
                                        _stream_delta=True,
                                        _stream_id=f"daily-run:{run_id}",
                                    ),
                                )
                            )
                    try:
                        await asyncio.wait_for(asyncio.shield(completion), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                record = await completion
            if record.get("status") not in {"completed", "completed_with_warnings"}:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"\n\n组合晨会 **{record.get('status')}**：{record.get('error') or '未生成报告'}",
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            daily_run_id=run_id,
                            _stream_delta=True,
                            _stream_id=f"daily-run:{run_id}",
                        ),
                    )
                )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            daily_run_id=run_id,
                            _stream_end=True,
                            _stream_id=f"daily-run:{run_id}",
                        ),
                    )
                )
                return
            if record.get("stage") == "skipped_data_unavailable":
                gate = record.get("analysis_gate") or {}
                warning = "\n".join(record.get("warnings") or [])
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="\n\n⚠️ 数据覆盖不足，已在模型分析前停止，未生成 PDF。",
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            daily_run_id=run_id,
                            _stream_delta=True,
                            _stream_id=f"daily-run:{run_id}",
                        ),
                    )
                )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            daily_run_id=run_id,
                            _stream_end=True,
                            _stream_id=f"daily-run:{run_id}",
                        ),
                    )
                )
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=(
                            f"关键数据覆盖 {gate.get('eligible_count', 0)}/{gate.get('total_count', 0)}；"
                            "未启动个股研究 Session，也未生成综合或个股 PDF。"
                            f"{f'{chr(10)}{warning}' if warning else ''}"
                        ),
                        metadata=self._response_metadata(
                            msg,
                            _channel_runtime=True,
                            daily_run_id=run_id,
                            daily_run_revision=int(record.get("revision") or 1),
                            portfolio_daily_skipped=True,
                        ),
                    )
                )
                return
            master = next(
                (item for item in record.get("artifacts") or [] if item.get("kind") == "master_pdf"),
                None,
            )
            media = [str(master["path"])] if master else []
            counts = record.get("summary") or {}
            holding_reports = self._daily_report_options(record)
            decision_text = self._daily_decision_text(record)
            content = (
                f"✅ 组合晨会已完成｜{record.get('market_date')}｜revision {record.get('revision', 1)}\n"
                f"退出 {counts.get('exit', 0)} · 减仓 {counts.get('reduce', 0)} · "
                f"加仓 {counts.get('add', 0)} · 观察 {counts.get('observe', 0)}\n"
                f"个股 PDF {len(holding_reports)} 份。综合报告已附上；可选择发送已有个股报告。"
                f"{f'{chr(10)}{decision_text}' if decision_text else ''}"
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="\n\n✅ 全部阶段完成，综合报告与个股附录已生成。",
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        daily_run_id=run_id,
                        _stream_delta=True,
                        _stream_id=f"daily-run:{run_id}",
                    ),
                )
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        daily_run_id=run_id,
                        _stream_end=True,
                        _stream_id=f"daily-run:{run_id}",
                    ),
                )
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    media=media,
                    metadata=self._response_metadata(
                        msg,
                        _channel_runtime=True,
                        daily_run_id=run_id,
                        daily_run_revision=int(record.get("revision") or 1),
                        portfolio_daily_complete=True,
                        holding_reports=holding_reports,
                        delivery_mode="report_pdf",
                    ),
                )
            )
        finally:
            if completion is not None and not completion.done():
                completion.cancel()
            if self._daily_runs_by_chat.get(key) == run_id:
                self._daily_runs_by_chat.pop(key, None)

    async def _wait_for_reply(self, session_id: str, attempt_id: str | None) -> Message:
        deadline = time.monotonic() + self.config.reply_timeout_s
        last_assistant: Message | None = None
        while time.monotonic() < deadline:
            messages = self.session_service.get_messages(session_id, limit=200)
            for message in reversed(messages):
                if message.role != "assistant":
                    continue
                if attempt_id and message.linked_attempt_id != attempt_id:
                    if last_assistant is None:
                        last_assistant = message
                    continue
                return message
            await asyncio.sleep(self.config.poll_interval_s)
        if last_assistant is not None:
            return last_assistant
        raise TimeoutError("timed out waiting for assistant reply")

    def _load_session_map(self) -> dict[str, str]:
        try:
            data = json.loads(self.session_map_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring invalid channel session map at %s", self.session_map_path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if value}

    def _save_session_map(self) -> None:
        self.session_map_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.session_map_path.with_suffix(self.session_map_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._session_map, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.session_map_path)

    def reset_session(self, session_key: str) -> str | None:
        """Remove a session mapping so the next message creates a fresh session.

        Args:
            session_key: The channel:chat_id key to reset.

        Returns:
            The removed session_id, or None if no mapping existed.
        """
        removed = self._session_map.pop(session_key, None)
        if removed is not None:
            self._save_session_map()
        return removed

    @staticmethod
    def _is_pairing_command(content: str) -> bool:
        stripped = content.strip().lower()
        return stripped == "/pairing" or stripped.startswith("/pairing ")

    @staticmethod
    def _pairing_subcommand_text(content: str) -> str:
        parts = content.strip().split(None, 1)
        return parts[1] if len(parts) > 1 else "list"

    @staticmethod
    def _is_new_session_command(content: str) -> bool:
        """Check if the message is a session reset command (/new, /reset, /newsession)."""
        return content.strip().lower() in ("/new", "/reset", "/newsession", "新对话")

    @staticmethod
    def _is_status_command(content: str) -> bool:
        return content.strip().lower() in ("/status", "status")

    @staticmethod
    def _is_cancel_command(content: str) -> bool:
        return content.strip().lower() in ("/cancel", "cancel")

    @staticmethod
    def _is_sessions_command(content: str) -> bool:
        return content.strip().lower() in ("/sessions", "sessions")


def _session_id(session: Session | dict[str, Any] | Any) -> str:
    if isinstance(session, Session):
        return session.session_id
    if isinstance(session, dict):
        return str(session["session_id"])
    return str(getattr(session, "session_id"))
