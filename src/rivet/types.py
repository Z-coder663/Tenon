from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


JsonObject = dict[str, Any]
Message = dict[str, Any]
EventHandler = Callable[[str, JsonObject], None]
TextDeltaHandler = Callable[[str], None]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw_message: Message = field(default_factory=dict)
    extensions: JsonObject = field(default_factory=dict)


class ModelClient(Protocol):
    def complete(self, messages: list[Message], tools: list[JsonObject]) -> ModelReply:
        """Return the next assistant message."""

