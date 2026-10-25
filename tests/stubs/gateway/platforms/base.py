"""Minimal stand-in for hermes-agent's gateway/platforms/base.py -- only the
surface nextcloud_talk's adapter.py actually uses. Chunking logic is a
simplified (not code-fence-aware) equivalent of the real
BasePlatformAdapter.truncate_message; good enough to prove the adapter calls
it with the right max_length and handles multi-chunk sends correctly.

``ProcessingOutcome`` and ``BasePlatformAdapter.on_processing_start`` /
``on_processing_complete`` below are copied verbatim (logic, not comments)
from the real ``gateway/platforms/base.py`` / ``gateway/platforms/event.py``
(the "Discord 👀/✅/❌ reactions" lifecycle hooks) -- nextcloud_talk's working
-reaction feature relies on the real class's ``on_processing_complete``
default implementation, so this stub has to reproduce it exactly, not just
enough surface to import.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageType(Enum):
    TEXT = "text"


class ProcessingOutcome(Enum):
    """Result classification for message-processing lifecycle hooks."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class SessionSource:
    platform: Any
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType = MessageType.TEXT
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    channel_prompt: Optional[str] = None
    internal: bool = False
    timestamp: Any = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reply_expected: Optional[bool] = None


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False


class BasePlatformAdapter:
    # ── Processing lifecycle hooks (Discord 👀/✅/❌ reactions). Adapters exposing
    # ``_add_reaction(chat_id, message_id, emoji)`` / ``_remove_reaction(chat_id, message_id)``
    # can just set the emoji attributes; left ``None`` the hook is a no-op.
    _OK_EMOJI: Optional[str] = None
    _FAIL_EMOJI: Optional[str] = None

    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self._running = False
        self._background_tasks = set()
        self.handled_events: List[MessageEvent] = []

    @property
    def name(self) -> str:
        """Short platform name used in adapter log lines (e.g. "[%s] ...", self.name)."""
        return getattr(self.platform, "value", None) or str(self.platform)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Hook called when background processing begins."""

    async def on_processing_complete(self, event: MessageEvent, outcome: "ProcessingOutcome") -> None:
        """Hook called when background processing completes. Default: opt-in reaction ack -- with
        ``_OK_EMOJI``/``_FAIL_EMOJI`` set and ``_add_reaction``/``_remove_reaction`` present, swap
        the in-progress reaction for the outcome one. Remove-then-add is deterministic whether the
        platform replaces or stacks a sender's reactions. CANCELLED leaves it unreacted."""
        if self._OK_EMOJI is None and self._FAIL_EMOJI is None:
            return
        add: Any = getattr(self, "_add_reaction", None)
        remove: Any = getattr(self, "_remove_reaction", None)
        enabled = getattr(self, "_reactions_enabled", None)
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if (not callable(add) or not callable(remove) or (callable(enabled) and not enabled())
                or not chat_id or not message_id):
            return
        await remove(chat_id, message_id)
        emoji = {ProcessingOutcome.SUCCESS: self._OK_EMOJI,
                 ProcessingOutcome.FAILURE: self._FAIL_EMOJI}.get(outcome)
        if emoji:
            await add(chat_id, message_id, emoji)

    def _mark_connected(self):
        self._running = True

    def _mark_disconnected(self):
        self._running = False

    def build_source(
        self,
        chat_id,
        chat_name=None,
        chat_type="dm",
        user_id=None,
        user_name=None,
        thread_id=None,
        **_ignored,
    ) -> SessionSource:
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id else None,
        )

    async def handle_message(self, event: MessageEvent) -> None:
        """Test double: record the event instead of running the real agent loop."""
        self.handled_events.append(event)

    @staticmethod
    def truncate_message(content: str, max_length: int = 4096, len_fn=None) -> List[str]:
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]
        chunks = []
        remaining = content
        while _len(remaining) > max_length:
            chunks.append(remaining[:max_length])
            remaining = remaining[max_length:]
        if remaining:
            chunks.append(remaining)
        return chunks
