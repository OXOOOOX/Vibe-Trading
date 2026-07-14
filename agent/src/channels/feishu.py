"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from src.channels.bus.events import OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.base import BaseChannel
from src.channels.research_sessions import (
    BOT_MENU_NEW_CONVERSATION,
    BOT_MENU_REFRESH_REPORT,
    BOT_MENU_RESEARCH_HUB,
    RESEARCH_CONTROL_NEW_CONVERSATION,
    RESEARCH_CONTROL_REFRESH_REPORT,
    build_research_session_route,
    warm_market_calendar,
)
from src.channels.utils import get_media_dir, safe_filename
# logging_bridge not needed (using stdlib logging)

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model import MentionEvent, P2ImMessageReceiveV1

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
_LOGIN_CONSOLE = Console()


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _load_lark_runtime() -> tuple[Any, str, str]:
    """Import the heavy Feishu SDK lazily.

    lark_oapi imports a large generated API surface at module import time, so
    keep it out of channel discovery and constructor paths.
    """
    import sys

    ws_client_already_imported = "lark_oapi.ws.client" in sys.modules
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    if (
        not ws_client_already_imported
        and threading.current_thread() is not threading.main_thread()
    ):
        import_loop = getattr(lark_ws_client, "loop", None)
        if (
            import_loop is not None
            and not import_loop.is_running()
            and not import_loop.is_closed()
        ):
            import_loop.close()
        lark_ws_client.loop = None
        with suppress(Exception):
            asyncio.set_event_loop(None)

    return lark, FEISHU_DOMAIN, LARK_DOMAIN

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}

_NEW_RESEARCH_PATTERN = re.compile(r"^[\s\u3000]*(?:发起|开始|创建|做)?[\s\u3000]*新的?研究[\s\u3000！!。.]*(?:吧)?[\s\u3000]*$")
_RESEARCH_FLOW = "new_research"
_RESEARCH_ACTIONS = {
    "morning_meeting",
    "show_stock_picker",
    "premarket",
    "portfolio",
    "holding",
    "custom_stock",
    "show_daily_reports",
    "send_daily_report",
    "send_daily_master",
    "retry_daily_holding",
    "rerun_daily",
    "cancel_daily_run",
    "resume_stock_session",
    "send_stock_daily_report",
    "continue_stock_daily_report",
    "start_stock_research",
}
_DIRECT_RESEARCH_ACTIONS = {
    "组合晨会": "morning_meeting",
    "一键晨会": "morning_meeting",
    "盘前分析": "premarket",
    "盘前研究": "premarket",
    "全仓分析": "portfolio",
    "组合分析": "portfolio",
    "持仓分析": "portfolio",
    "个股分析": "show_stock_picker",
}
_RESEARCH_CONTROL_TEXT = {
    "新对话": RESEARCH_CONTROL_NEW_CONVERSATION,
    "更新报告": RESEARCH_CONTROL_REFRESH_REPORT,
}
_PDF_REQUEST_VERBS = ("发送", "发给", "给我", "生成", "导出", "转成", "做成", "保存为")
_PDF_PREVIOUS_REPORT_MARKERS = ("这份", "这个", "刚才", "刚刚", "上面", "前面", "当前")


def _is_new_research_request(text: str) -> bool:
    """Return whether *text* is the dedicated Feishu research-menu trigger."""
    normalized = re.sub(r"[\s\u3000！!。.]+", "", str(text or ""))
    return normalized == "分析菜单" or bool(_NEW_RESEARCH_PATTERN.fullmatch(str(text or "")))


def _research_control_action(text: str) -> str | None:
    normalized = re.sub(r"[\s\u3000！!。.]+", "", str(text or ""))
    return _RESEARCH_CONTROL_TEXT.get(normalized)


def _pdf_delivery_request(text: str) -> tuple[bool, bool]:
    """Return ``(requested, use_previous_report)`` for explicit PDF asks."""
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    if "pdf" not in normalized or not any(verb in normalized for verb in _PDF_REQUEST_VERBS):
        return False, False
    use_previous = any(marker in normalized for marker in _PDF_PREVIOUS_REPORT_MARKERS)
    return True, use_previous


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _direct_research_action(text: str) -> str | None:
    normalized = re.sub(r"[\s\u3000！!。.]", "", str(text or ""))
    return _DIRECT_RESEARCH_ACTIONS.get(normalized)


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    # user_dsl: original card definition (richest source for rendered cards)
    user_dsl = content.get("user_dsl")
    if isinstance(user_dsl, str) and user_dsl.strip():
        try:
            dsl = json.loads(user_dsl)
            if isinstance(dsl, dict):
                parts.extend(_extract_interactive_content(dsl))
                if parts:
                    return parts
        except (json.JSONDecodeError, TypeError):
            pass

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    # Top-level elements: flat list or nested list format
    elements = content.get("elements")
    if isinstance(elements, list):
        if elements and isinstance(elements[0], list):
            # Nested list: [[{tag:"text",text:"..."}], ...]
            for row in elements:
                if isinstance(row, list):
                    for element in row:
                        parts.extend(_extract_element_content(element))
        else:
            # Flat list: [{tag:"markdown",content:"..."}, ...]
            for element in elements:
                parts.extend(_extract_element_content(element))

    # Body elements (schema 2.0)
    body = content.get("body", {})
    if isinstance(body, dict):
        body_elements = body.get("elements")
        if isinstance(body_elements, list):
            for element in body_elements:
                parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "text":
        text = element.get("text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "table":
        columns = [
            (column["name"], str(column.get("display_name") or column["name"]))
            for column in (element.get("columns") or [])
            if isinstance(column, dict) and column.get("name")
        ]
        rows = element.get("rows", [])
        if columns:
            parts.append(" | ".join(header for _, header in columns))
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values = []
                for name, _ in columns:
                    value = row.get(name)
                    if isinstance(value, list):
                        value = " ".join(str(item).strip() for item in value if item is not None)
                    values.append("" if value is None else str(value).strip())
                row_text = " | ".join(values).strip()
                if row_text:
                    parts.append(row_text)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message.

    Handles three payload shapes:
    - Direct:    {"title": "...", "content": [[...]]}
    - Localized: {"zh_cn": {"title": "...", "content": [...]}}
    - Wrapped:   {"post": {"zh_cn": {"title": "...", "content": [...]}}}
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code_text = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.

    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuConfig(BaseModel):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    react_emoji: str = "THUMBSUP"
    done_emoji: str | None = None  # Emoji to show when task is completed (e.g., "DONE", "OK")
    tool_hint_prefix: str = "\U0001f527"  # Prefix for inline tool hints (default: 🔧)
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False  # If True, bot replies quote the user's original message
    streaming: bool = True
    domain: Literal["feishu", "lark"] = "feishu"  # Set to "lark" for international Lark
    topic_isolation: bool = True  # If True, each topic in group chat gets its own session (isolation)


# =============================================================================
# QR scan-to-create onboarding
#
# Device-code flow: user scans a QR code with the Feishu/Lark mobile app and
# the platform creates a fully configured bot application automatically.
# =============================================================================

_ONBOARD_ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_ONBOARD_REQUEST_TIMEOUT_S = 10


def _accounts_base_url(domain: str) -> str:
    return _ONBOARD_ACCOUNTS_URLS.get(domain, _ONBOARD_ACCOUNTS_URLS["feishu"])


def _post_registration(base_url: str, body: dict[str, str]) -> dict:
    """POST form-encoded data to the registration endpoint, return parsed JSON.

    The registration endpoint returns JSON even on HTTP errors (e.g. poll
    returns authorization_pending as a 400). We always parse the body.
    """
    import httpx

    url = f"{base_url}{_REGISTRATION_PATH}"
    resp = httpx.post(
        url,
        data=body,
        timeout=_ONBOARD_REQUEST_TIMEOUT_S,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        return resp.json()
    except json.JSONDecodeError:
        resp.raise_for_status()
        return {}


def _init_registration(domain: str = "feishu") -> None:
    """Verify the environment supports client_secret auth. Raises RuntimeError if not."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration does not support client_secret auth. "
            f"Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    """Start the device-code flow. Returns device_code, qr_url, interval, expire_in."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark registration did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    if not qr_url:
        raise RuntimeError("Feishu / Lark registration did not return a login URL")
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
) -> dict | None:
    """Poll until the user scans the QR code, or timeout/denial.

    Returns dict with app_id, app_secret, domain on success, None on failure.
    """
    deadline = time.monotonic() + expire_in
    current_domain = domain
    poll_count = 0

    while time.monotonic() < deadline:
        base_url = _accounts_base_url(current_domain)
        try:
            res = _post_registration(base_url, {
                "action": "poll",
                "device_code": device_code,
                "tp": "ob_app",
            })
        except Exception:
            time.sleep(interval)
            continue

        poll_count += 1

        # Domain auto-detection: if the user's tenant is on Lark, switch automatically
        user_info = res.get("user_info") or {}
        tenant_brand = user_info.get("tenant_brand")
        if tenant_brand == "lark":
            current_domain = "lark"

        # Success
        if res.get("client_id") and res.get("client_secret"):
            return {
                "app_id": res["client_id"],
                "app_secret": res["client_secret"],
                "domain": current_domain,
            }

        # Terminal errors
        error = res.get("error", "")
        if error in ("access_denied", "expired_token"):
            _LOGIN_CONSOLE.print("[yellow]Authorization was cancelled or expired.[/yellow]")
            return None

        # authorization_pending or unknown — keep polling
        time.sleep(interval)

    _LOGIN_CONSOLE.print("[yellow]Authorization timed out.[/yellow]")
    return None


def qr_register(
    *,
    initial_domain: str = "feishu",
) -> dict | None:
    """Run the Feishu / Lark scan-to-create QR registration flow.

    Returns on success:
        {
            "app_id": str,
            "app_secret": str,
            "domain": "feishu" | "lark",
        }

    Returns None on expected failures (network, auth denied, timeout).
    Unexpected errors (bugs, protocol regressions) propagate to the caller.
    """
    import httpx

    try:
        return _qr_register_inner(initial_domain=initial_domain)
    except (RuntimeError, OSError, json.JSONDecodeError, httpx.HTTPError) as exc:
        _LOGIN_CONSOLE.print(
            f"[yellow]Unable to start Feishu/Lark login:[/yellow] {escape(str(exc))}"
        )
        return None


def _print_qr_code(url: str) -> None:
    """Print QR code as ASCII art if qrcode package is available, otherwise print URL."""
    try:
        import qrcode as qr_lib

        _LOGIN_CONSOLE.print("\n[bold]Scan with Feishu or Lark[/bold]\n")
        qr = qr_lib.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        _LOGIN_CONSOLE.print()
    except ImportError:
        _LOGIN_CONSOLE.print()
        _LOGIN_CONSOLE.print(Panel.fit(Text(url), title="Open with Feishu or Lark", border_style="cyan"))
        _LOGIN_CONSOLE.print()


def _qr_register_inner(
    *,
    initial_domain: str,
) -> dict | None:
    """Run init → begin → poll. Raises on network/protocol errors."""
    _LOGIN_CONSOLE.print("[cyan]Preparing Feishu/Lark login...[/cyan]")
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)

    _print_qr_code(begin["qr_url"])

    with _LOGIN_CONSOLE.status("Waiting for authorization in Feishu/Lark...", spinner="dots"):
        return _poll_registration(
            device_code=begin["device_code"],
            interval=begin["interval"],
            expire_in=begin["expire_in"],
            domain=initial_domain,
        )


_STREAM_ELEMENT_ID = "streaming_md"


@dataclass
class _FeishuStreamBuf:
    """Per-chat streaming accumulator using CardKit streaming API."""

    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5  # throttle between CardKit streaming updates

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._processed_card_action_ids: OrderedDict[str, None] = OrderedDict()
        self._processed_bot_menu_event_ids: OrderedDict[str, None] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}
        self._bot_open_id: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id

    # ------------------------------------------------------------------
    # QR login — writes credentials directly to config.json
    # ------------------------------------------------------------------

    async def login(self, force: bool = False) -> bool:
        """Perform QR code scan-to-create login for Feishu/Lark.

        Uses the Feishu device-code registration flow to create a new bot
        application automatically.  Opens a URL for the user to authorize
        with the Feishu or Lark mobile app.

        On success, writes ``appId``, ``appSecret``, and ``domain`` to
        ``channels.feishu`` in ``config.json`` and sets ``enabled: true``.

        Args:
            force: If True, clear existing credentials and force re-authentication.

        Returns True on success.
        """
        if force:
            self.config.app_id = ""
            self.config.app_secret = ""

        if self.config.app_id and self.config.app_secret:
            _LOGIN_CONSOLE.print("[green]Feishu/Lark is already authenticated.[/green]")
            _LOGIN_CONSOLE.print("Use --force to re-authenticate with a new bot.\n")
            return True

        _LOGIN_CONSOLE.print("Authorize with the mobile app. vibe-trading will save the new bot credentials.\n")

        result = qr_register(initial_domain=self.config.domain or "feishu")
        if not result:
            _LOGIN_CONSOLE.print(
                "[yellow]Login was not completed.[/yellow] "
                "Run 'vibe-trading channels login feishu --force' to retry."
            )
            return False

        self.config.app_id = result["app_id"]
        self.config.app_secret = result["app_secret"]
        self.config.domain = result.get("domain", "feishu")

        # Persist the validated adapter section so restart and auto-start use
        # the credentials obtained by QR onboarding.
        from src.config.loader import load_agent_config, save_agent_config
        from src.config.schema import AgentConfig

        config = load_agent_config()
        payload = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        channels = dict(payload.get("channels") or {})
        channels["feishu"] = self.config.model_dump(mode="json", by_alias=True)
        payload["channels"] = channels
        save_agent_config(AgentConfig.model_validate(payload))

        _LOGIN_CONSOLE.print("\n[green]Feishu/Lark login complete.[/green]")
        _LOGIN_CONSOLE.print(f"App ID: {escape(result['app_id'])}")
        _LOGIN_CONSOLE.print(f"Domain: {escape(self.config.domain)}")
        return True

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            self.logger.error("SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            self.logger.error(
                "app_id and app_secret not configured. "
                "Run 'vibe-trading channels login feishu' to set up via QR code."
            )
            return

        lark, feishu_domain, lark_domain = await asyncio.to_thread(_load_lark_runtime)

        # (stdlib logging handles Lark SDK output via propagation)

        self._running = True
        self._loop = asyncio.get_running_loop()
        calendar_task = asyncio.create_task(asyncio.to_thread(warm_market_calendar))
        self._background_tasks.add(calendar_task)
        calendar_task.add_done_callback(self._on_background_task_done)

        # Create Lark client for sending messages
        domain = lark_domain if self.config.domain == "lark" else feishu_domain
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_card_action_trigger", self._on_card_action_trigger
        )
        builder = self._register_optional_event(
            builder, "register_p2_application_bot_menu_v6", self._on_bot_menu
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        # Silence "processor not found" errors when bots are added/removed from groups.
        # These events carry no actionable data for the agent.
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_added_v1",
            lambda _: None,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_deleted_v1",
            lambda _: None,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # Start WebSocket client in a separate thread with reconnect loop.
        # A dedicated event loop is created for this thread so that lark_oapi's
        # module-level `loop = asyncio.get_event_loop()` picks up an idle loop
        # instead of the already-running main asyncio loop, which would cause
        # "This event loop is already running" errors.
        def run_ws():
            import time

            import lark_oapi.ws.client as _lark_ws_client

            previous_loop = getattr(_lark_ws_client, "loop", None)
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            # Patch the module-level loop used by lark's ws Client.start()
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        self.logger.warning("WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                if getattr(_lark_ws_client, "loop", None) is ws_loop:
                    _lark_ws_client.loop = previous_loop
                with suppress(Exception):
                    asyncio.set_event_loop(None)
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            self.logger.info("bot open_id: {}", self._bot_open_id)
        else:
            self.logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        self.logger.info("bot started with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        self.logger.info("bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """Fetch the bot's own open_id via GET /open-apis/bot/v3/info."""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            self.logger.warning("Failed to get bot info: code={}, msg={}", response.code, response.msg)
            return None
        except Exception as e:
            self.logger.warning("Error fetching bot info: {}", e)
            return None

    @staticmethod
    def _resolve_mentions(text: str, mentions: list[MentionEvent] | None) -> str:
        """Replace @_user_n placeholders with actual user info from mentions.

        Args:
            text: The message text containing @_user_n placeholders
            mentions: List of mention objects from Feishu message

        Returns:
            Text with placeholders replaced by @姓名 (open_id)
        """
        if not mentions or not text:
            return text

        for mention in mentions:
            key = mention.key or None
            if not key:
                continue
            # Feishu placeholders are numbered keys like @_user_1. Keep
            # punctuation-adjacent mentions valid without matching @_user_10.
            pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
            if not re.search(pattern, text):
                continue

            user_id_obj = mention.id or None
            if not user_id_obj:
                continue

            open_id = user_id_obj.open_id
            user_id = user_id_obj.user_id
            name = mention.name or key

            # Format: @姓名 (open_id, user_id: xxx)
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"

            text = re.sub(pattern, replacement, text)

        return text

    def _is_bot_mention_event(self, mention: Any) -> bool:
        mid = getattr(mention, "id", None)
        if not mid:
            return False

        mention_open_id = getattr(mid, "open_id", None) or ""
        bot_open_id = getattr(self, "_bot_open_id", None) or ""
        if bot_open_id:
            return mention_open_id == bot_open_id

        # Fallback heuristic when bot open_id is unavailable.
        return not getattr(mid, "user_id", None) and mention_open_id.startswith("ou_")

    def _strip_leading_bot_mention(
        self, text: str, mentions: list[MentionEvent] | None
    ) -> str:
        """Remove a required leading bot mention before slash command routing."""
        if not mentions or not text:
            return text

        candidate = text.lstrip()
        for mention in mentions:
            key = getattr(mention, "key", None) or ""
            if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
                continue
            if not self._is_bot_mention_event(mention):
                continue

            stripped = candidate[len(key) :].strip()
            return stripped or text

        return text

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            if self._is_bot_mention_event(mention):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Allow group messages when policy is open or bot is @mentioned."""
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    @staticmethod
    def _research_route(
        *,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, str]:
        base_session_key = session_key or f"feishu:{reply_chat_id}"
        return {
            "reply_chat_id": reply_chat_id,
            "chat_type": chat_type,
            "base_session_key": base_session_key,
        }

    @staticmethod
    def _research_callback(
        action: str, route: dict[str, str], **extra: str
    ) -> dict[str, str]:
        return {"flow": _RESEARCH_FLOW, "action": action, **route, **extra}

    @classmethod
    def _research_button(
        cls,
        text: str,
        action: str,
        route: dict[str, str],
        *,
        button_type: str = "default",
        element_id: str | None = None,
        **extra: str,
    ) -> dict[str, Any]:
        button: dict[str, Any] = {
            "tag": "button",
            "text": _plain_text(text),
            "type": button_type,
            "width": "fill",
            "behaviors": [
                {
                    "type": "callback",
                    "value": cls._research_callback(action, route, **extra),
                }
            ],
        }
        if element_id:
            button["element_id"] = element_id
        return button

    @classmethod
    def _build_research_menu_card(
        cls, *, reply_chat_id: str, chat_type: str, session_key: str | None
    ) -> dict[str, Any]:
        """Build the first-stage research chooser card."""
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        choices = [
            ("组合晨会", "morning_meeting", "primary", "research_morning_meeting"),
            ("盘前分析", "premarket", "primary", "research_premarket"),
            ("个股分析", "show_stock_picker", "default", "research_stock"),
            ("持仓分析", "portfolio", "default", "research_portfolio"),
        ]
        columns = []
        for label, action, button_type, element_id in choices:
            button = cls._research_button(
                label,
                action,
                route,
                button_type=button_type,
                element_id=element_id,
            )
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [button],
                }
            )
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "blue",
                "title": _plain_text("分析菜单"),
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "点击即可切换或恢复对应专题 Session：已有则续聊，没有则创建。完整研究会发回 PDF，后续可直接发送文字继续讨论。",
                    },
                    {
                        "tag": "column_set",
                        "flex_mode": "flow",
                        "horizontal_spacing": "8px",
                        "columns": columns,
                    },
                ]
            },
        }

    @classmethod
    def _build_stock_picker_card(
        cls,
        holdings: list[dict[str, Any]],
        *,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        """Build holding shortcuts plus a six-digit custom-code form."""
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        elements: list[dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": "选择当前持仓，或在下方输入新的 6 位证券代码。",
            }
        ]

        holding_buttons: list[dict[str, Any]] = []
        for index, holding in enumerate(holdings[:20]):
            symbol = str(holding.get("symbol") or holding.get("code") or "").strip().upper()
            code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", symbol)
            if not code_match:
                continue
            code = code_match.group(1)
            name = str(holding.get("name") or code).strip()
            holding_buttons.append(
                cls._research_button(
                    f"{name} · {code}",
                    "holding",
                    route,
                    element_id=f"holding_{index}",
                    symbol=symbol,
                )
            )

        for index in range(0, len(holding_buttons), 2):
            pair = holding_buttons[index : index + 2]
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "flow",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [button],
                        }
                        for button in pair
                    ],
                }
            )

        if not holding_buttons:
            elements.append(
                {"tag": "markdown", "content": "当前还没有可选择的持仓，请直接输入代码。"}
            )

        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "form",
                    "name": "research_code_form",
                    "direction": "vertical",
                    "vertical_spacing": "8px",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "stock_code",
                            "input_type": "text",
                            "required": True,
                            "placeholder": _plain_text("例如：600036"),
                        },
                        {
                            **cls._research_button(
                                "分析此代码",
                                "custom_stock",
                                route,
                                button_type="primary",
                                element_id="custom_stock_submit",
                            ),
                            "name": "custom_stock_submit",
                            "form_action_type": "submit",
                        },
                    ],
                },
            ]
        )
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "turquoise",
                "title": _plain_text("选择个股"),
            },
            "body": {"elements": elements},
        }

    @classmethod
    def _build_stock_reuse_card(
        cls,
        context: dict[str, Any],
        *,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        """Ask whether today's stock Session/report should be reused."""
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        symbol = str(context.get("symbol") or "")
        name = str(context.get("name") or symbol)
        analysis_action = str(context.get("analysis_action") or "holding")
        session = dict(context.get("session") or {})
        report = dict(context.get("report") or {})
        common = {
            "symbol": symbol,
            "name": name,
            "analysis_action": analysis_action,
        }

        status_lines = ["已检查今天可复用的内容："]
        if session:
            created_time = str(session.get("created_time") or "今天")
            title = str(session.get("title") or "个股研究 Session")
            status_lines.append(f"- Session：{title}（{created_time} 创建）")
        else:
            status_lines.append("- Session：今天尚未创建")
        if report:
            status_lines.append(
                f"- 个股日报：{report.get('market_date') or '今日'} · "
                f"revision {report.get('revision') or 1}"
            )
        else:
            status_lines.append("- 个股日报：今天尚未生成")
        status_lines.append("发送或载入已有报告都不会重复生成；随后可直接继续提问。")

        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": "\n".join(status_lines)}
        ]
        buttons: list[dict[str, Any]] = []
        if session:
            buttons.append(
                cls._research_button(
                    "继续今日 Session",
                    "resume_stock_session",
                    route,
                    button_type="primary",
                    element_id="stock_resume_session",
                    session_id=str(session.get("session_id") or ""),
                    **common,
                )
            )
        if report:
            report_common = {
                "run_id": str(report.get("run_id") or ""),
                "artifact_id": str(report.get("artifact_id") or ""),
                "session_id": str(session.get("session_id") or ""),
                **common,
            }
            buttons.extend(
                [
                    cls._research_button(
                        "发送今日 PDF",
                        "send_stock_daily_report",
                        route,
                        button_type="primary" if not session else "default",
                        element_id="stock_send_daily_pdf",
                        **report_common,
                    ),
                    cls._research_button(
                        "载入报告继续研究",
                        "continue_stock_daily_report",
                        route,
                        element_id="stock_continue_daily_report",
                        **report_common,
                    ),
                ]
            )
        buttons.append(
            cls._research_button(
                "重新生成完整分析",
                "start_stock_research",
                route,
                element_id="stock_start_new_research",
                **common,
            )
        )
        for index in range(0, len(buttons), 2):
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "flow",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [button],
                        }
                        for button in buttons[index : index + 2]
                    ],
                }
            )
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "turquoise",
                "title": _plain_text(f"{name} · 今日已有研究"),
            },
            "body": {"elements": elements},
        }

    @classmethod
    def _build_daily_complete_card(
        cls,
        content: str,
        *,
        run_id: str,
        revision: int,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        buttons = [
            cls._research_button(
                "再次发送综合 PDF",
                "send_daily_master",
                route,
                button_type="primary",
                element_id="daily_master_pdf",
                run_id=run_id,
            ),
            cls._research_button(
                "查看个股报告",
                "show_daily_reports",
                route,
                element_id="daily_holding_reports",
                run_id=run_id,
            ),
            cls._research_button(
                "按最新数据重新生成",
                "rerun_daily",
                route,
                element_id="daily_rerun",
                run_id=run_id,
            ),
        ]
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "green",
                "title": _plain_text(f"今日组合晨会已完成 · revision {revision}"),
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                    {
                        "tag": "column_set",
                        "flex_mode": "flow",
                        "horizontal_spacing": "8px",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [button],
                            }
                            for button in buttons
                        ],
                    },
                ]
            },
        }

    @classmethod
    def _build_daily_report_picker_card(
        cls,
        reports: list[dict[str, Any]],
        *,
        run_id: str,
        revision: int,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        elements: list[dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": "发送会直接复用已生成 PDF，不会重新分析；只有“重试”会创建新 revision。",
            }
        ]
        for index, report in enumerate(reports[:30]):
            symbol = str(report.get("symbol") or "")
            name = str(report.get("name") or symbol)
            status = str(report.get("status") or "completed")
            artifact_id = str(report.get("artifact_id") or "")
            elements.extend(
                [
                    {
                        "tag": "markdown",
                        "content": f"**{name} · {symbol}**\n状态：{status}",
                    },
                    {
                        "tag": "column_set",
                        "flex_mode": "flow",
                        "horizontal_spacing": "8px",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    cls._research_button(
                                        "发送 PDF",
                                        "send_daily_report",
                                        route,
                                        button_type="primary",
                                        element_id=f"daily_report_{index}",
                                        run_id=run_id,
                                        artifact_id=artifact_id,
                                        symbol=symbol,
                                    )
                                ],
                            },
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    cls._research_button(
                                        "重试此标的",
                                        "retry_daily_holding",
                                        route,
                                        element_id=f"daily_retry_{index}",
                                        run_id=run_id,
                                        symbol=symbol,
                                    )
                                ],
                            },
                        ],
                    },
                ]
            )
        if not reports:
            elements.append({"tag": "markdown", "content": "没有可发送的个股 PDF。"})
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "turquoise",
                "title": _plain_text(f"个股日报 · revision {revision}"),
            },
            "body": {"elements": elements},
        }

    @classmethod
    def _build_daily_skipped_card(
        cls,
        content: str,
        *,
        run_id: str,
        revision: int,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "yellow",
                "title": _plain_text(f"组合晨会已停止 · revision {revision}"),
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": content},
                    cls._research_button(
                        "数据恢复后重新获取",
                        "rerun_daily",
                        route,
                        button_type="primary",
                        element_id="daily_skipped_rerun",
                        run_id=run_id,
                    ),
                ]
            },
        }

    @classmethod
    def _build_daily_failed_card(
        cls,
        content: str,
        *,
        run_id: str,
        revision: int,
        reply_chat_id: str,
        chat_type: str,
        session_key: str | None,
    ) -> dict[str, Any]:
        route = cls._research_route(
            reply_chat_id=reply_chat_id,
            chat_type=chat_type,
            session_key=session_key,
        )
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": content}
        ]
        if run_id:
            elements.append(
                cls._research_button(
                    "手工重试组合晨会",
                    "rerun_daily",
                    route,
                    button_type="primary",
                    element_id="daily_failed_rerun",
                    run_id=run_id,
                )
            )
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "template": "red",
                "title": _plain_text(f"自动组合晨会失败 · revision {revision}"),
            },
            "body": {"elements": elements},
        }

    async def _send_research_card(
        self, *, card: dict[str, Any], reply_chat_id: str, chat_type: str
    ) -> None:
        if not self._client:
            raise RuntimeError("Feishu client is not ready")
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        content = json.dumps(card, ensure_ascii=False)
        loop = asyncio.get_running_loop()
        message_id = await loop.run_in_executor(
            None,
            self._send_message_sync,
            receive_id_type,
            reply_chat_id,
            "interactive",
            content,
        )
        if not message_id:
            raise RuntimeError("Feishu rejected the research card")

    async def _send_research_notice(
        self, *, content: str, reply_chat_id: str, chat_type: str
    ) -> None:
        if not self._client:
            return
        receive_id_type = "chat_id" if chat_type == "group" else "open_id"
        body = json.dumps({"text": content}, ensure_ascii=False)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._send_message_sync,
            receive_id_type,
            reply_chat_id,
            "text",
            body,
        )

    @staticmethod
    def _custom_stock_prompt(code: str) -> str:
        from src.portfolio.analysis import build_custom_stock_prompt

        return build_custom_stock_prompt(code)

    @classmethod
    def _build_research_prompt(cls, payload: dict[str, str]) -> tuple[str, str]:
        from src.portfolio.analysis import build_analysis_prompt, find_holding
        from src.portfolio.state import load_state

        action = payload.get("action", "")
        if action == "premarket":
            return (
                build_analysis_prompt("market", market_phase="premarket"),
                "盘前分析",
            )
        if action == "portfolio":
            return build_analysis_prompt("portfolio"), "持仓分析"
        if action == "holding":
            state = load_state()
            holding = find_holding(state.holdings, payload.get("symbol", ""))
            if holding is None:
                raise ValueError("该标的已不在当前持仓，请重新打开“分析菜单”选择。")
            name = str(holding.get("name") or holding.get("symbol") or "个股")
            return build_analysis_prompt("holding", holding), f"{name}个股分析"
        if action == "custom_stock":
            code = payload.get("code", "")
            if not re.fullmatch(r"\d{6}", code):
                raise ValueError("请输入完整的 6 位证券代码。")
            return cls._custom_stock_prompt(code), f"{code}个股分析"
        raise ValueError("不支持的研究类型。")

    @staticmethod
    def _card_action_response(message: str, *, error: bool = False) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": "error" if error else "success",
                    "content": message,
                }
            }
        )

    def _on_bot_menu(self, data: Any) -> None:
        """Handle the three persistent Feishu bot-menu events without chat bubbles."""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        operator_id = getattr(operator, "operator_id", None)
        sender_id = str(
            getattr(operator_id, "open_id", "")
            or getattr(operator, "open_id", "")
            or ""
        )
        event_key = str(getattr(event, "event_key", "") or "")
        event_id = str(getattr(getattr(data, "header", None), "event_id", "") or "")
        if event_key not in {
            BOT_MENU_RESEARCH_HUB,
            BOT_MENU_NEW_CONVERSATION,
            BOT_MENU_REFRESH_REPORT,
        }:
            return
        if not sender_id or not self.is_allowed(sender_id):
            return
        if event_id and event_id in self._processed_bot_menu_event_ids:
            return
        if event_id:
            self._processed_bot_menu_event_ids[event_id] = None
            while len(self._processed_bot_menu_event_ids) > 1000:
                self._processed_bot_menu_event_ids.popitem(last=False)
        if not self._loop or not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle_bot_menu_action(sender_id, event_key), self._loop
        )

        def _log_bot_menu_failure(completed: Any) -> None:
            try:
                completed.result()
            except Exception:
                self.logger.exception("Error handling Feishu bot menu event")

        future.add_done_callback(_log_bot_menu_failure)

    async def _handle_bot_menu_action(self, sender_id: str, event_key: str) -> None:
        base_session_key = f"feishu:{sender_id}"
        if event_key == BOT_MENU_RESEARCH_HUB:
            await self._send_research_card(
                card=self._build_research_menu_card(
                    reply_chat_id=sender_id,
                    chat_type="p2p",
                    session_key=base_session_key,
                ),
                reply_chat_id=sender_id,
                chat_type="p2p",
            )
            return
        await self._handle_message(
            sender_id=sender_id,
            chat_id=sender_id,
            content="",
            metadata={
                "chat_type": "p2p",
                "msg_type": "bot_menu",
                "research_control": event_key,
                "research_base_session_key": base_session_key,
            },
            session_key=base_session_key,
            is_dm=True,
        )

    def _on_card_action_trigger(self, data: Any) -> Any:
        """Acknowledge card callbacks within three seconds and schedule real work."""
        event = getattr(data, "event", None)
        operator = getattr(event, "operator", None)
        action = getattr(event, "action", None)
        context = getattr(event, "context", None)
        sender_id = str(getattr(operator, "open_id", "") or "")
        value = dict(getattr(action, "value", None) or {})

        if value.get("flow") != _RESEARCH_FLOW or value.get("action") not in _RESEARCH_ACTIONS:
            return self._card_action_response("无法识别这张研究卡片。", error=True)
        if not sender_id or not self.is_allowed(sender_id):
            return self._card_action_response("当前账号尚未获得使用权限。", error=True)

        event_id = str(getattr(getattr(data, "header", None), "event_id", "") or "")
        if event_id and event_id in self._processed_card_action_ids:
            return self._card_action_response("该操作已经提交。")
        if event_id:
            self._processed_card_action_ids[event_id] = None
            while len(self._processed_card_action_ids) > 1000:
                self._processed_card_action_ids.popitem(last=False)

        form_value = dict(getattr(action, "form_value", None) or {})
        if value["action"] == "custom_stock":
            raw_code = str(form_value.get("stock_code") or "").strip()
            if not re.fullmatch(r"\d{6}", raw_code):
                return self._card_action_response("请输入完整的 6 位证券代码。", error=True)
            value["code"] = raw_code

        chat_type = str(value.get("chat_type") or "p2p")
        fallback_chat_id = str(getattr(context, "open_chat_id", "") or "")
        reply_chat_id = str(value.get("reply_chat_id") or "")
        if not reply_chat_id:
            reply_chat_id = fallback_chat_id if chat_type == "group" else sender_id
        value.update(
            {
                "sender_id": sender_id,
                "reply_chat_id": reply_chat_id,
                "chat_type": chat_type,
                "source_message_id": str(
                    getattr(context, "open_message_id", "") or ""
                ),
            }
        )

        if not self._loop or not self._loop.is_running():
            return self._card_action_response("飞书连接正在启动，请稍后重试。", error=True)

        future = asyncio.run_coroutine_threadsafe(
            self._handle_research_card_action(value), self._loop
        )

        def _log_callback_failure(completed: Any) -> None:
            try:
                completed.result()
            except Exception:
                self.logger.exception("Error handling research card action")

        future.add_done_callback(_log_callback_failure)
        if value["action"] in {"show_stock_picker", "show_daily_reports"}:
            return self._card_action_response(
                "正在打开个股日报。"
                if value["action"] == "show_daily_reports"
                else "正在打开个股选择。"
            )
        label = {
            "morning_meeting": "组合晨会",
            "premarket": "盘前分析",
            "portfolio": "持仓分析",
            "holding": "个股分析",
            "custom_stock": "个股分析",
            "send_daily_report": "个股日报发送",
            "send_daily_master": "综合报告发送",
            "retry_daily_holding": "个股日报重试",
            "rerun_daily": "组合晨会重跑",
            "cancel_daily_run": "组合晨会取消",
            "resume_stock_session": "今日 Session 切换",
            "send_stock_daily_report": "今日个股日报发送",
            "continue_stock_daily_report": "今日报告载入",
            "start_stock_research": "完整个股分析",
        }.get(value["action"], "研究任务")
        return self._card_action_response(f"已提交{label}。")

    async def _handle_research_card_action(self, payload: dict[str, str]) -> None:
        reply_chat_id = payload["reply_chat_id"]
        chat_type = payload["chat_type"]
        base_session_key = str(
            payload.get("base_session_key")
            or payload.get("session_key")
            or f"feishu:{reply_chat_id}"
        )
        try:
            if payload["action"] in {
                "resume_stock_session",
                "send_stock_daily_report",
                "continue_stock_daily_report",
                "start_stock_research",
            }:
                analysis_action = str(payload.get("analysis_action") or "holding")
                symbol = str(payload.get("symbol") or "")
                prompt_payload = {
                    "action": analysis_action,
                    "symbol": symbol,
                    "code": re.sub(r"\D", "", symbol)[:6],
                }
                prompt, label = self._build_research_prompt(prompt_payload)
                route = await asyncio.to_thread(
                    build_research_session_route,
                    base_key=base_session_key,
                    action=analysis_action,
                    symbol=symbol,
                    name=str(payload.get("name") or label.removesuffix("个股分析")),
                )
                await self._handle_message(
                    sender_id=payload["sender_id"],
                    chat_id=reply_chat_id,
                    content=prompt if payload["action"] == "start_stock_research" else "",
                    metadata={
                        "message_id": payload.get("source_message_id", ""),
                        "chat_type": chat_type,
                        "msg_type": "interactive",
                        "stock_research_action": payload["action"],
                        "stock_session_id": payload.get("session_id", ""),
                        "daily_run_id": payload.get("run_id", ""),
                        "artifact_id": payload.get("artifact_id", ""),
                        "research_action": analysis_action,
                        "research_label": route.label,
                        **route.metadata(),
                    },
                    session_key=route.route_key,
                    is_dm=chat_type == "p2p",
                )
                return

            if payload["action"] in {
                "show_daily_reports",
                "send_daily_report",
                "send_daily_master",
                "retry_daily_holding",
                "rerun_daily",
                "cancel_daily_run",
            }:
                await self._handle_message(
                    sender_id=payload["sender_id"],
                    chat_id=reply_chat_id,
                    content="",
                    metadata={
                        "message_id": payload.get("source_message_id", ""),
                        "chat_type": chat_type,
                        "msg_type": "interactive",
                        "daily_report_action": payload["action"],
                        "daily_run_id": payload.get("run_id", ""),
                        "artifact_id": payload.get("artifact_id", ""),
                        "symbol": payload.get("symbol", ""),
                        "research_base_session_key": base_session_key,
                    },
                    session_key=base_session_key,
                    is_dm=chat_type == "p2p",
                )
                return

            if payload["action"] == "morning_meeting":
                await self._handle_message(
                    sender_id=payload["sender_id"],
                    chat_id=reply_chat_id,
                    content="启动组合晨会",
                    metadata={
                        "message_id": payload.get("source_message_id", ""),
                        "chat_type": chat_type,
                        "msg_type": "interactive",
                        "daily_portfolio_run": True,
                        "research_action": "morning_meeting",
                        "research_label": "组合晨会",
                        "research_base_session_key": base_session_key,
                    },
                    session_key=base_session_key,
                    is_dm=chat_type == "p2p",
                )
                return

            if payload["action"] == "show_stock_picker":
                from src.portfolio.state import load_state

                state = load_state()
                card = self._build_stock_picker_card(
                    state.holdings,
                    reply_chat_id=reply_chat_id,
                    chat_type=chat_type,
                    session_key=base_session_key,
                )
                await self._send_research_card(
                    card=card,
                    reply_chat_id=reply_chat_id,
                    chat_type=chat_type,
                )
                return

            prompt, label = self._build_research_prompt(payload)
            symbol = str(payload.get("symbol") or payload.get("code") or "")
            route = await asyncio.to_thread(
                build_research_session_route,
                base_key=base_session_key,
                action=payload["action"],
                symbol=symbol,
                name=label.removesuffix("个股分析"),
            )
            await self._handle_message(
                sender_id=payload["sender_id"],
                chat_id=reply_chat_id,
                content=prompt,
                metadata={
                    "message_id": payload.get("source_message_id", ""),
                    "chat_type": chat_type,
                    "msg_type": "interactive",
                    "stock_research_action": (
                        "prepare"
                        if payload["action"] in {"holding", "custom_stock"}
                        else ""
                    ),
                    "research_action": payload["action"],
                    "research_label": route.label,
                    **route.metadata(),
                },
                session_key=route.route_key,
                is_dm=chat_type == "p2p",
            )
        except Exception as exc:
            await self._send_research_notice(
                content=f"无法启动研究：{exc}",
                reply_chat_id=reply_chat_id,
                chat_type=chat_type,
            )

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                self.logger.warning(
                    "Failed to add reaction: code={}, msg={}", response.code, response.msg
                )
                return None
            else:
                self.logger.debug("Added {} reaction to message {}", emoji_type, message_id)
                return response.data.reaction_id if response.data else None
        except Exception as e:
            self.logger.warning("Error adding reaction: {}", e)
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """Add a reaction emoji to a message.

        Returns the reaction_id on success, None on failure.
        When called via a tracked background task, the returned reaction_id
        is stored in ``_reaction_ids`` for later cleanup by ``send_delta``.

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Sync helper for removing reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self._client.im.v1.message_reaction.delete(request)
            if response.success():
                self.logger.debug("Removed reaction {} from message {}", reaction_id, message_id)
            else:
                self.logger.debug(
                    "Failed to remove reaction: code={}, msg={}", response.code, response.msg
                )
        except Exception as e:
            self.logger.debug("Error removing reaction: {}", e)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """
        Remove a reaction emoji from a message (non-blocking).

        Used to clear the "processing" indicator after bot replies.
        """
        if not self._client or not reaction_id:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reaction_sync, message_id, reaction_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback: remove from tracking set and log unhandled exceptions."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.warning("Background task failed: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """Callback: store reaction_id after background add-reaction completes."""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Scope streaming buffers to the inbound message when available."""
        meta = metadata or {}
        return meta.get("message_id") or chat_id

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    # Markdown formatting patterns that should be stripped from plain-text
    # surfaces like table cells and heading text.
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """Strip markdown formatting markers from text for plain display.

        Feishu table cells do not support markdown rendering, so we remove
        the formatting markers to keep the text readable.
        """
        # Remove bold markers
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        # Remove italic markers
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        # Remove strikethrough markers
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each.

        Feishu cards have a hard limit of one table per card (API error 11310).
        When the rendered content contains multiple markdown tables each table is
        placed in a separate card message so every table reaches the user.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": display_text,
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # ── Smart format detection ──────────────────────────────────────────
    # Patterns that indicate "complex" markdown needing card rendering
    _COMPLEX_MD_RE = re.compile(
        r"```"  # fenced code block
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
        r"|^#{1,6}\s+",  # headings
        re.MULTILINE,
    )

    # Simple markdown patterns (bold, italic, strikethrough)
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"  # **bold**
        r"|__.+?__"  # __bold__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
        r"|~~.+?~~",  # ~~strikethrough~~
        re.DOTALL,
    )

    # Markdown link: [text](url)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # Unordered list items
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # Ordered list items
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Max length for plain text format
    _TEXT_MAX_LEN = 200

    # Max length for post (rich text) format; beyond this, use card
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*.

        Returns one of:
        - ``"text"``        – plain text, short and no markdown
        - ``"post"``        – rich text (links only, moderate length)
        - ``"interactive"`` – card with full markdown rendering
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """Convert markdown content to Feishu post message JSON.

        Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
        Each line becomes a paragraph (row) in the post body.
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {
                        "tag": "a",
                        "text": m.group(1),
                        "href": m.group(2),
                    }
                )
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    self.logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    self.logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading image {}", file_path)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    self.logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    self.logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading file {}", file_path)
            return None

    def _download_image_sync(
        self, message_id: str, image_key: str
    ) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading image {}", image_key)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    @staticmethod
    def _safe_media_filename(filename: str | None, fallback: str) -> str:
        """Return a local-only filename for downloaded Feishu media."""
        candidate = filename or fallback
        # Feishu/Lark filenames come from message metadata. Treat both POSIX
        # and Windows separators as path boundaries before applying the shared
        # filename sanitizer so downloads cannot escape the channel media dir.
        candidate = os.path.basename(candidate.replace("\\", "/"))
        candidate = safe_filename(candidate)
        if candidate in ("", ".", ".."):
            return safe_filename(fallback) or uuid.uuid4().hex
        return candidate

    async def _download_and_save_media(
        self, msg_type: str, content_json: dict, message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None
        fallback_filename = uuid.uuid4().hex

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                fallback_filename = f"{image_key[:16]}.jpg"
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = fallback_filename

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                self.logger.warning("{} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                self.logger.warning("{} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            fallback_filename = file_key[:16]
            data, filename = await loop.run_in_executor(
                None, self._download_file_sync, message_id, file_key, msg_type
            )

            if not data:
                self.logger.warning("{} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = fallback_filename

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename
            file_path.write_bytes(data)
            path_str = str(file_path)
            self.logger.debug("Downloaded {} to {}", msg_type, path_str)
            return path_str, f"[{msg_type}: {path_str}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """Fetch the text content of a Feishu message by ID (synchronous).

        Returns a "[Reply to: ...]" context string, or None on failure.
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                self.logger.debug(
                    "could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            self.logger.debug("error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> bool:
        """Reply to an existing Feishu message using the Reply API (synchronous).

        Args:
            reply_in_thread: If True, reply as a thread/topic message
                in the Feishu client.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                self.logger.error(
                    "Failed to reply to message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            self.logger.debug("reply sent to message {}", parent_message_id)
            return True
        except Exception:
            self.logger.exception("Error replying to message {}", parent_message_id)
            return False

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """Return whether a group reply should create a Feishu thread/topic."""
        return metadata.get("chat_type", "group") == "group" and self.config.reply_to_message

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """Return the message_id that should receive a Reply API response."""
        if metadata.get("chat_type", "group") != "group":
            return None
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("thread_id") or self.config.reply_to_message:
            return message_id
        return None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a single message and return the message_id on success."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self.logger.error(
                    "Failed to send {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            msg_id = getattr(response.data, "message_id", None)
            self.logger.debug("{} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception:
            self.logger.exception("Error sending {} message", msg_type)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Create a CardKit streaming card, send it to chat, return card_id.

        When *reply_message_id* is provided the card is delivered via the
        reply API. *reply_in_thread* controls whether Feishu creates a
        thread/topic for that reply. Otherwise the plain create-message API is
        used.
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID}
        ]
        meta = metadata or {}
        daily_run_id = str(meta.get("daily_run_id") or "")
        if daily_run_id:
            route = self._research_route(
                reply_chat_id=chat_id,
                chat_type=str(meta.get("chat_type") or "p2p"),
                session_key=str(
                    meta.get("research_base_session_key") or f"feishu:{chat_id}"
                ),
            )
            elements.append(
                self._research_button(
                    "取消本次运行",
                    "cancel_daily_run",
                    route,
                    element_id="daily_cancel",
                    run_id=daily_run_id,
                )
            )
        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {"elements": elements},
        }
        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                self.logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id, "interactive", card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = self._send_message_sync(
                        receive_id_type, chat_id, "interactive", card_content,
                    ) is not None
                if sent:
                    return card_id
                self.logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            self.logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """Stream-update the markdown element on a CardKit card (typewriter effect)."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(_STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                self.logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _set_streaming_mode_sync(self, card_id: str, enabled: bool, sequence: int) -> bool:
        """Set CardKit streaming_mode using a strictly increasing sequence."""
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": enabled}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                self.logger.warning(
                    "Failed to set streaming={} on card {}: code={}, msg={}",
                    enabled,
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error setting streaming={} on card {}: {}", enabled, card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """Turn off CardKit streaming_mode so the chat list preview exits the streaming placeholder.

        Per Feishu docs, streaming cards keep a generating-style summary in the session list until
        streaming_mode is set to false via card settings (after final content update).
        Sequence must strictly exceed the previous card OpenAPI operation on this entity.
        """
        return self._set_streaming_mode_sync(card_id, False, sequence)

    def _stream_update_text_with_reopen_sync(
        self,
        card_id: str,
        content: str,
        sequence: int,
    ) -> tuple[bool, int]:
        if self._stream_update_text_sync(card_id, content, sequence):
            return True, sequence
        sequence += 1
        if not self._set_streaming_mode_sync(card_id, True, sequence):
            return False, sequence
        sequence += 1
        return self._stream_update_text_sync(card_id, content, sequence), sequence

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Progressive streaming via CardKit: create card on first delta, stream-update on subsequent.

        Supported metadata keys:
            _stream_end: Finalize the streaming card.
            _tool_hint:  Delta is a formatted tool hint (for display only).
            message_id:  Original message id (used with _stream_end for reaction cleanup).
            chat_type:   "group" or "p2p" — controls reply-in-thread for streaming cards.
        """
        if not self._client:
            return
        meta = metadata or {}
        stream_key = self._stream_key(chat_id, meta)
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        # --- stream end: final update or fallback ---
        if meta.get("_stream_end"):
            message_id = meta.get("message_id")
            # Only finalize the OnIt -> DONE reaction transition on the truly
            # final stream end. _resuming=True means the agent will keep
            # working (more tool-call rounds), so leave the reaction state
            # in place — otherwise the OnIt indicator disappears prematurely
            # and the DONE reaction fires after every tool call.
            if message_id and not meta.get("_resuming"):
                reaction_id = self._reaction_ids.pop(message_id, None)
                if reaction_id:
                    await self._remove_reaction(message_id, reaction_id)
                # Add completion emoji if configured
                if self.config.done_emoji:
                    await self._add_reaction(message_id, self.config.done_emoji)

            buf = self._stream_bufs.pop(stream_key, None)
            if not buf or not buf.text:
                return
            # Try to finalize via streaming card; if that fails (e.g.
            # streaming mode was closed by Feishu due to timeout), fall
            # back to sending a regular interactive card.
            if buf.card_id:
                buf.sequence += 1
                ok, buf.sequence = await loop.run_in_executor(
                    None,
                    self._stream_update_text_with_reopen_sync,
                    buf.card_id,
                    buf.text,
                    buf.sequence,
                )
                if ok:
                    buf.sequence += 1
                    closed = await loop.run_in_executor(
                        None,
                        self._close_streaming_mode_sync,
                        buf.card_id,
                        buf.sequence,
                    )
                    if not closed:
                        buf.sequence += 1
                        await loop.run_in_executor(
                            None,
                            self._close_streaming_mode_sync,
                            buf.card_id,
                            buf.sequence,
                        )
                    return
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                self.logger.warning(
                    "Streaming card {} final update failed, falling back to regular card",
                    buf.card_id,
                )
            for chunk in self._split_elements_by_table_limit(
                self._build_card_elements(buf.text)
            ):
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": chunk},
                    ensure_ascii=False,
                )
                # Fallback replies stay in existing topics, but only create a
                # new topic when reply-to-message is enabled.
                fallback_msg_id = self._thread_reply_target(meta)
                if fallback_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            fallback_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(meta),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, rid_type, chat_id, "interactive", card
                    )
            return

        # --- accumulate delta ---
        buf = self._stream_bufs.get(stream_key)
        if buf is None:
            buf = _FeishuStreamBuf()
            self._stream_bufs[stream_key] = buf
        buf.text += delta
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            # Use the Reply API for existing topics, and only create new topics
            # when reply-to-message is enabled.
            use_reply_in_thread = self._should_use_reply_in_thread(meta)
            reply_msg_id = self._thread_reply_target(meta)
            card_id = await loop.run_in_executor(
                None,
                lambda: self._create_streaming_card_sync(
                    rid_type,
                    chat_id,
                    reply_msg_id,
                    reply_in_thread=use_reply_in_thread,
                    metadata=meta,
                ),
            )
            if card_id:
                ok, sequence = await loop.run_in_executor(
                    None, self._stream_update_text_with_reopen_sync, card_id, buf.text, 1
                )
                if ok:
                    buf.card_id = card_id
                    buf.sequence = sequence
                    buf.last_edit = now
                else:
                    await loop.run_in_executor(
                        None, self._close_streaming_mode_sync, card_id, sequence + 1
                    )
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            ok, buf.sequence = await loop.run_in_executor(
                None,
                self._stream_update_text_with_reopen_sync,
                buf.card_id,
                buf.text,
                buf.sequence + 1,
            )
            if ok:
                buf.last_edit = now
            else:
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                buf.card_id = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            self.logger.warning("client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            if msg.metadata.get("_research_hub"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                await self._send_research_card(
                    card=self._build_research_menu_card(
                        reply_chat_id=msg.chat_id,
                        chat_type=chat_type,
                        session_key=base_key,
                    ),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                )
                return

            if msg.metadata.get("_stock_research_context"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                await self._send_research_card(
                    card=self._build_stock_reuse_card(
                        dict(msg.metadata.get("stock_research_context") or {}),
                        reply_chat_id=msg.chat_id,
                        chat_type=chat_type,
                        session_key=base_key,
                    ),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                )
                return

            if msg.metadata.get("_daily_report_picker"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                await self._send_research_card(
                    card=self._build_daily_report_picker_card(
                        list(msg.metadata.get("holding_reports") or []),
                        run_id=str(msg.metadata.get("daily_run_id") or ""),
                        revision=int(msg.metadata.get("daily_run_revision") or 1),
                        reply_chat_id=msg.chat_id,
                        chat_type=chat_type,
                        session_key=base_key,
                    ),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                )
                return

            if msg.metadata.get("portfolio_daily_skipped"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                await self._send_research_card(
                    card=self._build_daily_skipped_card(
                        msg.content,
                        run_id=str(msg.metadata.get("daily_run_id") or ""),
                        revision=int(msg.metadata.get("daily_run_revision") or 1),
                        reply_chat_id=msg.chat_id,
                        chat_type=chat_type,
                        session_key=base_key,
                    ),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                )
                return

            if msg.metadata.get("portfolio_daily_failed"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                await self._send_research_card(
                    card=self._build_daily_failed_card(
                        msg.content,
                        run_id=str(msg.metadata.get("daily_run_id") or ""),
                        revision=int(msg.metadata.get("daily_run_revision") or 1),
                        reply_chat_id=msg.chat_id,
                        chat_type=chat_type,
                        session_key=base_key,
                    ),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                )
                return

            # Handle tool hint messages.  When a streaming card is active for
            # this chat, inline the hint into the card instead of sending a
            # separate message so the user experience stays cohesive.
            if msg.metadata.get("_tool_hint"):
                hint = (msg.content or "").strip()
                if not hint:
                    return
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if buf and buf.card_id:
                    # Delegate to send_delta so tool hints get the same
                    # throttling (and card creation) as regular text deltas.
                    await self.send_delta(
                        msg.chat_id,
                        "\n\n" + self._format_tool_hint_delta(hint) + "\n\n",
                    )
                    return
                # No active streaming card — send as a regular interactive card
                # with the same 🔧 prefix style. Existing topics stay threaded;
                # new topics are created only when reply-to-message is enabled.
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": [
                        {"tag": "markdown", "content": self._format_tool_hint_delta(hint)},
                    ]},
                    ensure_ascii=False,
                )
                _th_msg_id = self._thread_reply_target(msg.metadata)
                if _th_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            _th_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, receive_id_type, msg.chat_id, "interactive", card
                    )
                return

            # Determine whether the first message should quote the user's message.
            # Only the very first send (media or text) in this call uses reply; subsequent
            # chunks/media fall back to plain create to avoid redundant quote bubbles.
            # Always target message_id — the Feishu Reply API keeps replies in the
            # same topic automatically when the target message is inside a topic.
            reply_message_id: str | None = None
            _msg_id = msg.metadata.get("message_id")
            has_thread_id = msg.metadata.get("thread_id")
            if self.config.reply_to_message and not msg.metadata.get("_progress", False):
                reply_message_id = _msg_id
            # For topic group messages, always reply to keep context in thread
            elif has_thread_id:
                reply_message_id = _msg_id

            first_send = True  # tracks whether the reply has already been used

            def _do_send(m_type: str, content: str) -> None:
                """Send via reply (first message) or create (subsequent).

                Group chats only set reply_in_thread=True when
                reply_to_message is enabled; otherwise a Reply API call for an
                existing topic must not create a new topic.
                """
                nonlocal first_send
                if reply_message_id:
                    # If we're in a topic, always use reply to stay in the topic
                    if has_thread_id:
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    elif first_send:
                        # If we're not in a topic but replying to message, only first uses reply
                        first_send = False
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    # Fall back to regular send if reply fails
                self._send_message_sync(receive_id_type, msg.chat_id, m_type, content)

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    self.logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        # Feishu's OpenAPI names video messages "media".
                        # Use "audio" for audio, "media" for video, "file" for documents.
                        # Feishu requires these specific msg_types for inline playback.
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.metadata.get("portfolio_daily_complete"):
                chat_type = str(msg.metadata.get("chat_type") or "p2p")
                base_key = str(
                    msg.metadata.get("research_base_session_key")
                    or f"feishu:{msg.chat_id}"
                )
                card = self._build_daily_complete_card(
                    msg.content,
                    run_id=str(msg.metadata.get("daily_run_id") or ""),
                    revision=int(msg.metadata.get("daily_run_revision") or 1),
                    reply_chat_id=msg.chat_id,
                    chat_type=chat_type,
                    session_key=base_key,
                )
                await loop.run_in_executor(
                    None,
                    _do_send,
                    "interactive",
                    json.dumps(card, ensure_ascii=False),
                )
                return

            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    # Short plain text – send as simple text message
                    text_body = json.dumps({"text": msg.content.strip()}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    # Medium content with links – send as rich-text post
                    post_body = self._markdown_to_post(msg.content)
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    # Complex / long content – send as interactive card
                    elements = self._build_card_elements(msg.content)
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

        except Exception:
            self.logger.exception("Error sending message")
            raise

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            self.logger.debug("raw message: {}", message.content)
            self.logger.debug("mentions: {}", getattr(message, "mentions", None))

            message_id = message.message_id

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                self.logger.debug("skipping group message (not mentioned)")
                return

            # Deduplication check
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Early permission check — avoid side effects for unauthorized users.
            # Group chats are silently ignored; DMs get a pairing code.
            if not self.is_allowed(sender_id):
                if chat_type == "p2p":
                    # content="" because the pairing reply is generated by
                    # BaseChannel._handle_message, not from the original message.
                    await self._handle_message(
                        sender_id=sender_id,
                        chat_id=sender_id,
                        content="",
                        is_dm=True,
                    )
                return

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._strip_leading_bot_mention(text, mentions)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Ordinary top-level group messages share one chat Session. Only real
            # Feishu topic/thread messages carry ``thread_id`` and are isolated.
            # Using ``message_id`` here would incorrectly turn every normal group
            # message into a brand-new conversation.
            if chat_type == "group":
                if self.config.topic_isolation and thread_id:
                    session_key = f"feishu:{chat_id}:thread:{thread_id}"
                else:
                    session_key = f"feishu:{chat_id}"
            else:
                session_key = None

            reply_to = chat_id if chat_type == "group" else sender_id
            base_session_key = session_key or f"feishu:{reply_to}"
            if msg_type == "text" and _is_new_research_request(content):
                card = self._build_research_menu_card(
                    reply_chat_id=reply_to,
                    chat_type=chat_type,
                    session_key=session_key,
                )
                await self._send_research_card(
                    card=card,
                    reply_chat_id=reply_to,
                    chat_type=chat_type,
                )
                return

            control_action = (
                _research_control_action(content) if msg_type == "text" else None
            )
            if control_action:
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=reply_to,
                    content="",
                    metadata={
                        "message_id": message_id,
                        "chat_type": chat_type,
                        "msg_type": msg_type,
                        "parent_id": parent_id,
                        "root_id": root_id,
                        "thread_id": thread_id,
                        "research_control": control_action,
                        "research_base_session_key": base_session_key,
                    },
                    session_key=base_session_key,
                    is_dm=chat_type == "p2p",
                )
                return

            research_metadata: dict[str, Any] = {}
            pdf_requested, pdf_from_previous = (
                _pdf_delivery_request(content) if msg_type == "text" else (False, False)
            )
            pdf_metadata: dict[str, Any] = {}
            if pdf_requested:
                pdf_metadata = {
                    "pdf_delivery_requested": True,
                    "pdf_from_previous_report": pdf_from_previous,
                }
                if not pdf_from_previous:
                    content = (
                        f"{content}\n\n"
                        "请只输出完整的 Markdown 报告正文；PDF 附件由飞书频道自动生成和发送，"
                        "不要声称无法发送附件，也不要只返回本地文件路径。"
                    )
            outbound_session_key = session_key
            direct_action = (
                _direct_research_action(content) if msg_type == "text" else None
            )
            if direct_action == "show_stock_picker":
                from src.portfolio.state import load_state

                card = self._build_stock_picker_card(
                    load_state().holdings,
                    reply_chat_id=reply_to,
                    chat_type=chat_type,
                    session_key=base_session_key,
                )
                await self._send_research_card(
                    card=card,
                    reply_chat_id=reply_to,
                    chat_type=chat_type,
                )
                return
            if direct_action == "morning_meeting":
                content = "启动组合晨会"
                research_metadata = {
                    "daily_portfolio_run": True,
                    "research_action": "morning_meeting",
                    "research_label": "组合晨会",
                }
            elif direct_action in {"premarket", "portfolio"}:
                content, label = self._build_research_prompt(
                    {"action": direct_action}
                )
                route = await asyncio.to_thread(
                    build_research_session_route,
                    base_key=base_session_key,
                    action=direct_action,
                )
                research_metadata = {
                    "research_action": direct_action,
                    "research_label": route.label or label,
                    **route.metadata(),
                }
                outbound_session_key = route.route_key

            # Forward to message bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                    **pdf_metadata,
                    **research_metadata,
                },
                session_key=outbound_session_key,
                is_dm=chat_type == "p2p",
            )

        except Exception:
            self.logger.exception("Error processing message")

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        self.logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """Format a tool hint string with the 🔧 prefix for each line."""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(
            f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip()
        )
