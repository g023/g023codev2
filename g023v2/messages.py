"""Agent inboxes and transcript helpers. No HTTP."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from g023v2.constants import MAX_PENDING_MESSAGES, MESSAGE_QUEUE_PRUNE_THRESHOLD


def user_message(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def assistant_message(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "output_text", "text": text}]}


def reasoning_item(text: str) -> dict[str, Any]:
    """Responses API reasoning item with a reasoning_text part.

    Must be passed back on later requests whenever `tools` are sent.
    """
    return {
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": text}],
    }


@dataclass
class AgentMessage:
    id: str
    sender: str
    recipient: str
    content: str
    kind: str = "message"
    priority: int = 0
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"


class MessageQueue:
    def __init__(self, owner: str, max_pending: int = MAX_PENDING_MESSAGES):
        self.owner = owner
        self.max_pending = max_pending
        self.messages: list[AgentMessage] = []
        self.lock = threading.Lock()

    def enqueue(self, msg: AgentMessage) -> str:
        with self.lock:
            pending = sum(1 for m in self.messages if m.status == "pending")
            if pending >= self.max_pending:
                raise RuntimeError(f"queue for {self.owner} is full ({self.max_pending} pending)")
            self.messages.append(msg)
            return msg.id

    def edit(self, msg_id: str, new_content: str) -> bool:
        with self.lock:
            for m in self.messages:
                if m.id == msg_id and m.status == "pending":
                    m.content = new_content
                    return True
        return False

    def delete(self, msg_id: str) -> bool:
        with self.lock:
            for m in self.messages:
                if m.id == msg_id and m.status == "pending":
                    m.status = "deleted"
                    return True
        return False

    def _prune_locked(self) -> None:
        if len(self.messages) <= MESSAGE_QUEUE_PRUNE_THRESHOLD:
            return
        self.messages = [m for m in self.messages if m.status == "pending"]

    def drain(self) -> list[AgentMessage]:
        with self.lock:
            ready = [m for m in self.messages if m.status == "pending"]
            ready.sort(key=lambda m: (-m.priority, m.timestamp))
            for m in ready:
                m.status = "delivered"
            self._prune_locked()
            return ready

    def pending(self) -> list[AgentMessage]:
        with self.lock:
            return [m for m in self.messages if m.status == "pending"]

    def all_messages(self) -> list[AgentMessage]:
        with self.lock:
            return list(self.messages)
