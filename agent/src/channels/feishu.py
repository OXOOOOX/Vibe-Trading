"""Feishu long-connection adapter with topic-to-session routing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.config.paths import get_runtime_root
from src.session.dispatcher import DispatchJob, SessionDispatcher

logger = logging.getLogger(__name__)


def _csv_set(value: Optional[str]) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


class FeishuBindingStore:
    """Persistent Feishu message dedupe and external-thread bindings."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (get_runtime_root() / "channels" / "feishu.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thread_bindings (
                    external_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_key TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    root_message_id TEXT NOT NULL,
                    owner_open_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feishu_chat_bindings
                    ON thread_bindings(tenant_key, chat_id, created_at);
                CREATE TABLE IF NOT EXISTS private_active_sessions (
                    external_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def claim_message(self, message_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO processed_messages(message_id, processed_at) VALUES (?, ?)",
                (message_id, datetime.now().isoformat()),
            )
            return cursor.rowcount == 1

    def get_thread(self, external_key: str) -> Optional[dict[str, str]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM thread_bindings WHERE external_key = ?", (external_key,)
            ).fetchone()
        return dict(row) if row else None

    def bind_thread(
        self,
        external_key: str,
        session_id: str,
        *,
        tenant_key: str,
        chat_id: str,
        root_message_id: str,
        owner_open_id: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO thread_bindings
                (external_key, session_id, tenant_key, chat_id, root_message_id, owner_open_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    external_key,
                    session_id,
                    tenant_key,
                    chat_id,
                    root_message_id,
                    owner_open_id,
                    datetime.now().isoformat(),
                ),
            )

    def list_chat_sessions(self, tenant_key: str, chat_id: str, limit: int = 10) -> list[dict[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM thread_bindings WHERE tenant_key = ? AND chat_id = ?
                ORDER BY created_at DESC LIMIT ?""",
                (tenant_key, chat_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_private_active(self, external_key: str) -> Optional[str]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM private_active_sessions WHERE external_key = ?", (external_key,)
            ).fetchone()
        return str(row["session_id"]) if row else None

    def set_private_active(self, external_key: str, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO private_active_sessions(external_key, session_id, updated_at)
                VALUES (?, ?, ?) ON CONFLICT(external_key) DO UPDATE SET
                session_id = excluded.session_id, updated_at = excluded.updated_at""",
                (external_key, session_id, datetime.now().isoformat()),
            )

    def can_control(self, session_id: str, open_id: str, operators: set[str]) -> bool:
        if open_id in operators:
            return True
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_open_id FROM thread_bindings WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
        return bool(row and row["owner_open_id"] == open_id)


def _card(title: str, body: str, color: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title[:80]}},
        "elements": [{"tag": "markdown", "content": body[:28000]}],
    }


def _is_live_control_request(content: str) -> bool:
    """Reject explicit execution/control requests while allowing research wording."""
    normalized = content.lower().replace("-", " ").replace("_", " ")
    patterns = (
        r"(?:帮我|立即|现在)?(?:实盘下单|执行交易|启动实盘|开启实盘|停止实盘|关闭实盘)",
        r"(?:替我|给我)?(?:买入|卖出|下单)\s*[0-9a-z.]+",
        r"\b(?:commit mandate|start live runner|stop live runner|broker authorize|clear kill switch)\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


class FeishuTransport:
    """Thin wrapper around the official lark-oapi client."""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark

        self.lark = lark
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .msg_type("interactive")
            .reply_in_thread(True)
            .uuid(uuid.uuid4().hex[:32])
            .build()
        )
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu reply failed: {response.code} {response.msg}")
        return str(response.data.message_id)

    def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.patch(request)
        if not response.success():
            raise RuntimeError(f"Feishu card update failed: {response.code} {response.msg}")

    def reply_text(self, message_id: str, text: str) -> str:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        body = (
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text[:140000]}, ensure_ascii=False))
            .msg_type("text")
            .reply_in_thread(True)
            .uuid(uuid.uuid4().hex[:32])
            .build()
        )
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu reply failed: {response.code} {response.msg}")
        return str(response.data.message_id)

    def reply_file(self, message_id: str, path: Path) -> None:
        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        with path.open("rb") as stream:
            upload_body = (
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(path.name)
                .file(stream)
                .build()
            )
            upload = self.client.im.v1.file.create(
                CreateFileRequest.builder().request_body(upload_body).build()
            )
        if not upload.success():
            raise RuntimeError(f"Feishu file upload failed: {upload.code} {upload.msg}")
        body = (
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"file_key": upload.data.file_key}))
            .msg_type("file")
            .reply_in_thread(True)
            .uuid(uuid.uuid4().hex[:32])
            .build()
        )
        response = self.client.im.v1.message.reply(
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        )
        if not response.success():
            raise RuntimeError(f"Feishu file reply failed: {response.code} {response.msg}")


class FeishuBot:
    """Route Feishu chat events to persistent Vibe-Trading sessions."""

    def __init__(
        self,
        service: Any,
        dispatcher: SessionDispatcher,
        *,
        binding_store: Optional[FeishuBindingStore] = None,
        transport: Optional[FeishuTransport] = None,
    ) -> None:
        self.service = service
        self.dispatcher = dispatcher
        self.app_id = os.getenv("FEISHU_APP_ID", "").strip()
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.allowed_chat_ids = _csv_set(os.getenv("FEISHU_ALLOWED_CHAT_IDS"))
        self.allowed_open_ids = _csv_set(os.getenv("FEISHU_ALLOWED_OPEN_IDS"))
        self.operator_open_ids = _csv_set(os.getenv("FEISHU_OPERATOR_OPEN_IDS"))
        self.bindings = binding_store or FeishuBindingStore()
        self.transport = transport
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_client: Any = None
        self._thread: Optional[threading.Thread] = None
        self.dispatcher.add_listener(self._on_job_update)

    @staticmethod
    def enabled() -> bool:
        return os.getenv("FEISHU_BOT_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}

    def start(self) -> None:
        if not self.enabled():
            return
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required when FEISHU_BOT_ENABLED=1")
        if not self.allowed_chat_ids and not self.allowed_open_ids:
            raise RuntimeError("Configure FEISHU_ALLOWED_CHAT_IDS or FEISHU_ALLOWED_OPEN_IDS before enabling the bot")
        self._main_loop = asyncio.get_running_loop()
        if self.transport is None:
            self.transport = FeishuTransport(self.app_id, self.app_secret)
        self._thread = threading.Thread(target=self._run_ws, name="feishu-ws", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        # lark-oapi's WS client has no public stop method. Disable reconnect and
        # close its private connection best-effort; the daemon thread cannot
        # keep the API process alive.
        if self._ws_client is not None:
            self._ws_client._auto_reconnect = False
            try:
                import lark_oapi.ws.client as ws_module

                future = asyncio.run_coroutine_threadsafe(self._ws_client._disconnect(), ws_module.loop)
                await asyncio.wrap_future(future)
            except Exception:
                logger.debug("Unable to close Feishu WebSocket cleanly", exc_info=True)

    def _run_ws(self) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.ws import Client

            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._receive_event)
                .build()
            )
            self._ws_client = Client(
                self.app_id,
                self.app_secret,
                log_level=lark.LogLevel.INFO,
                event_handler=handler,
            )
            self._ws_client.start()
        except Exception:
            logger.exception("Feishu long connection stopped")

    def _receive_event(self, data: Any) -> None:
        if self._main_loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.handle_event(data), self._main_loop)

    async def handle_event(self, data: Any) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if not message or not sender or getattr(sender, "sender_type", "") == "bot":
            return
        message_id = str(getattr(message, "message_id", "") or "")
        if not message_id or not self.bindings.claim_message(message_id):
            return
        sender_id = getattr(sender, "sender_id", None)
        open_id = str(getattr(sender_id, "open_id", "") or "")
        tenant_key = str(getattr(getattr(data, "header", None), "tenant_key", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        chat_type = str(getattr(message, "chat_type", "") or "")
        is_group = chat_type == "group"
        if is_group and chat_id not in self.allowed_chat_ids:
            return
        if not is_group and open_id not in self.allowed_open_ids and open_id not in self.operator_open_ids:
            return
        mentions = list(getattr(message, "mentions", None) or [])
        if is_group and not mentions:
            return
        if getattr(message, "message_type", "") != "text":
            await self._reply_text(message_id, "首版仅支持文本问题；结果可以返回文件。")
            return
        try:
            content = str(json.loads(getattr(message, "content", "{}") or "{}").get("text", ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        for mention in mentions:
            key = str(getattr(mention, "key", "") or "")
            if key:
                content = content.replace(key, "")
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            await self._reply_text(message_id, "请在 @机器人 后输入研究或回测问题。")
            return

        root_id = str(getattr(message, "root_id", "") or getattr(message, "thread_id", "") or message_id)
        external_key = f"{tenant_key}:{chat_id}:{root_id}" if is_group else f"{tenant_key}:p2p:{open_id}"
        binding = self.bindings.get_thread(external_key) if is_group else None
        session_id = binding["session_id"] if binding else self.bindings.get_private_active(external_key)

        command = content.split(maxsplit=1)[0].lower()
        if command in {"/help", "帮助"}:
            await self._reply_text(message_id, "命令：/status 状态、/cancel 取消、/sessions 最近会话；群聊中每个顶层 @ 会创建新 Session。")
            return
        if command in {"/sessions", "会话"}:
            await self._send_sessions(message_id, tenant_key, chat_id, is_group)
            return
        if command in {"/status", "状态"}:
            await self._send_status(message_id, session_id)
            return
        if command in {"/cancel", "取消"}:
            if not session_id:
                await self._reply_text(message_id, "当前没有可取消的 Session。")
            elif not self.bindings.can_control(session_id, open_id, self.operator_open_ids) and is_group:
                await self._reply_text(message_id, "只有话题发起人或机器人管理员可以取消任务。")
            else:
                result = await self.dispatcher.cancel_session(session_id)
                await self._reply_text(message_id, f"已取消：运行中 {int(result['running'])} 个，排队 {result['queued']} 个。")
            return
        if _is_live_control_request(content):
            await self._reply_text(message_id, "飞书渠道只开放研究与回测，不允许实盘下单、授权或运行控制。")
            return
        if not is_group and command == "/use":
            parts = content.split(maxsplit=1)
            target_id = parts[1].strip() if len(parts) == 2 else ""
            target = self.service.get_session(target_id) if target_id else None
            target_owner = str(getattr(target, "config", {}).get("feishu_owner_open_id", "")) if target else ""
            if not target or (open_id not in self.operator_open_ids and target_owner != open_id):
                await self._reply_text(message_id, "Session 不存在，或你没有切换权限。")
            else:
                self.bindings.set_private_active(external_key, target_id)
                await self._reply_text(message_id, f"已切换到 Session {target_id}。")
            return
        if not is_group and command in {"/new", "新会话"}:
            session = self.service.create_session(
                title=f"飞书私聊｜{datetime.now().strftime('%m-%d %H:%M')}",
                config={
                    "channel": "feishu",
                    "feishu_tenant_key": tenant_key,
                    "feishu_chat_id": chat_id,
                    "feishu_owner_open_id": open_id,
                },
            )
            self.bindings.set_private_active(external_key, session.session_id)
            await self._reply_text(message_id, f"已创建并切换到 Session {session.session_id}。")
            return

        if not session_id:
            title = f"飞书{'群' if is_group else '私聊'}｜{content[:42]}"
            session = self.service.create_session(
                title=title,
                config={
                    "channel": "feishu",
                    "feishu_tenant_key": tenant_key,
                    "feishu_chat_id": chat_id,
                    "feishu_owner_open_id": open_id,
                },
            )
            session_id = session.session_id
            if is_group:
                self.bindings.bind_thread(
                    external_key,
                    session_id,
                    tenant_key=tenant_key,
                    chat_id=chat_id,
                    root_message_id=root_id,
                    owner_open_id=open_id,
                )
            else:
                self.bindings.set_private_active(external_key, session_id)

        ack_id = await self._reply_card(
            message_id,
            _card("Vibe-Trading 已接收", f"Session `{session_id}`\n\n任务正在排队。", "blue"),
        )
        try:
            result = await self.dispatcher.submit(
                session_id,
                content,
                source="feishu",
                source_metadata={
                    "feishu_message_id": message_id,
                    "feishu_ack_message_id": ack_id,
                    "feishu_chat_id": chat_id,
                    "feishu_open_id": open_id,
                    "feishu_root_id": root_id,
                },
            )
            await self._update_card(
                ack_id,
                _card(
                    "Vibe-Trading 已排队",
                    f"Session `{session_id}`\n\n队列位置：{result['queue_position']}",
                    "blue",
                ),
            )
        except Exception as exc:
            await self._update_card(ack_id, _card("提交失败", str(exc), "red"))

    async def _on_job_update(self, job: DispatchJob) -> None:
        if job.source != "feishu" or not self.transport:
            return
        ack_id = str(job.source_metadata.get("feishu_ack_message_id", ""))
        original_id = str(job.source_metadata.get("feishu_message_id", ""))
        if not ack_id:
            return
        if job.status == "running":
            await self._update_card(ack_id, _card("Vibe-Trading 研究中", f"Session `{job.session_id}`\n\n正在执行研究与回测。", "blue"))
            return
        if job.status == "completed":
            attempt = self.service.store.get_attempt(job.session_id, job.attempt_id)
            summary = str(getattr(attempt, "summary", "") or "任务已完成。")
            await self._update_card(ack_id, _card("研究完成", summary, "green"))
            run_dir = getattr(attempt, "run_dir", None)
            if run_dir and original_id:
                await self._send_artifacts(original_id, Path(run_dir))
            return
        title = "任务已取消" if job.status == "cancelled" else "任务失败"
        color = "orange" if job.status == "cancelled" else "red"
        await self._update_card(ack_id, _card(title, job.error or title, color))

    async def _send_artifacts(self, message_id: str, run_dir: Path) -> None:
        if not run_dir.is_dir() or not self.transport:
            return
        candidates = []
        for pattern in ("*.pdf", "*.html", "*.csv"):
            candidates.extend(run_dir.rglob(pattern))
        for path in sorted(candidates, key=lambda item: (item.suffix != ".pdf", item.name))[:3]:
            if path.is_file() and path.stat().st_size <= 30 * 1024 * 1024:
                try:
                    await asyncio.to_thread(self.transport.reply_file, message_id, path)
                except Exception:
                    logger.warning("Unable to send Feishu artifact %s", path, exc_info=True)

    async def _send_status(self, message_id: str, session_id: Optional[str]) -> None:
        if not session_id:
            await self._reply_text(message_id, "当前话题还没有关联 Session。")
            return
        session = self.service.get_session(session_id)
        attempt = self.service.store.get_attempt(session_id, session.last_attempt_id) if session and session.last_attempt_id else None
        status = getattr(getattr(attempt, "status", None), "value", "idle")
        await self._reply_text(message_id, f"Session {session_id}\n状态：{status}")

    async def _send_sessions(self, message_id: str, tenant_key: str, chat_id: str, is_group: bool) -> None:
        if not is_group:
            sessions = [
                session for session in self.service.list_sessions(limit=100)
                if str(session.config.get("feishu_chat_id", "")) == chat_id
            ][:10]
            if not sessions:
                await self._reply_text(message_id, "还没有私聊 Session；使用 /new 创建。")
                return
            lines = ["最近的私聊 Sessions："] + [f"- {session.session_id}  {session.title}" for session in sessions]
            lines.append("使用 /use <session_id> 切换。")
            await self._reply_text(message_id, "\n".join(lines))
            return
        rows = self.bindings.list_chat_sessions(tenant_key, chat_id)
        if not rows:
            await self._reply_text(message_id, "当前群还没有飞书研究 Session。")
            return
        lines = ["最近的群聊 Sessions："] + [f"- {row['session_id']}" for row in rows]
        await self._reply_text(message_id, "\n".join(lines))

    async def _reply_text(self, message_id: str, text: str) -> str:
        if not self.transport:
            return ""
        return await asyncio.to_thread(self.transport.reply_text, message_id, text)

    async def _reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        if not self.transport:
            return ""
        return await asyncio.to_thread(self.transport.reply_card, message_id, card)

    async def _update_card(self, message_id: str, card: dict[str, Any]) -> None:
        if self.transport and message_id:
            await asyncio.to_thread(self.transport.update_card, message_id, card)
