"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncio


# Optional OutboundMessage.metadata key for structured, channel-agnostic UI
# payloads. Value is JSON-serializable with at least ``kind``; rich clients may
# render it and other channels may ignore unknown keys.
OUTBOUND_META_AGENT_UI = "_agent_ui"

# Internal-only inbound metadata used by in-process channels to ask the agent
# loop to update runtime state without going through a user session.
INBOUND_META_RUNTIME_CONTROL = "_runtime_control"
RUNTIME_CONTROL_ACK = "_ack"
RUNTIME_CONTROL_MCP_RELOAD = "mcp_reload"


class DeliveryRejectedError(RuntimeError):
    """The provider returned a definitive rejection before accepting a message."""


class DeliveryUncertainError(RuntimeError):
    """The provider may have accepted a message, but no durable receipt was obtained."""


@dataclass(frozen=True)
class DeliveryReceipt:
    """Provider acknowledgement for one externally visible delivery.

    ``delivered`` means that the provider accepted the request and returned a
    stable message identifier.  It deliberately does not claim that the human
    recipient has read the message.
    """

    provider: str
    remote_message_id: str
    provider_request_id: str | None
    accepted_at: str
    status: str = "delivered"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "remote_message_id": self.remote_message_id,
            "provider_request_id": self.provider_request_id,
            "accepted_at": self.accepted_at,
            "status": self.status,
        }


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp, etc.
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    source_event_id: str | None = None  # Provider event/message id used for durable deduplication
    acceptance: asyncio.Future[dict[str, Any]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"

    def accept(self, result: dict[str, Any] | None = None) -> None:
        """Confirm that this inbound event crossed its durable handling boundary."""
        if self.acceptance is not None and not self.acceptance.done():
            self.acceptance.set_result(dict(result or {}))

    def reject(self, exc: BaseException) -> None:
        """Reject an inbound event so its provider can retry it."""
        if self.acceptance is not None and not self.acceptance.done():
            self.acceptance.set_exception(exc)


@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``metadata`` can carry routing (``message_id``, …), trace flags
    (``_progress``), and optional ``OUTBOUND_META_AGENT_UI`` blobs for
    rich clients; non-WebUI channels may ignore unknown keys.
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[str]] = field(default_factory=list)
