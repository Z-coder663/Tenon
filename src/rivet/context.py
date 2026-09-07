from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

from .errors import OperationCancelled, SessionError, ToolError
from .types import JsonObject, Message


SummaryHandler = Callable[[str, list[Message], str], str]


STRUCTURED_SUMMARY_INSTRUCTIONS = """You compress prior coding-agent history.
Return only a factual summary with exactly these headings, in this order:

## Current Goal
## Constraints
## Key Decisions
## Code Changes
## Verification
## Pending Tasks
## Important References

Rules:
- Never omit an explicit user constraint or requirement.
- Preserve file names, function names, class names, commands, and error text verbatim when useful.
- Preserve failed approaches and why they failed.
- Omit irrelevant conversation and social chatter.
- Do not invent or complete uncertain information.
- Merge the existing structured summary with the newly archived messages.
- Keep the result concise enough to replace the archived messages.
"""


class ContextManager:
    """Layered context: structured summary, recent atomic units, archived history."""

    SUMMARY_NAME = "rivet_context"
    SUMMARY_PREFIX = "[Structured Summary of archived history]"
    SUMMARY_SECTIONS = (
        "Current Goal",
        "Constraints",
        "Key Decisions",
        "Code Changes",
        "Verification",
        "Pending Tasks",
        "Important References",
    )
    DEFAULT_RECENT_UNITS = 8
    MAX_SUMMARY_CHARS = 12_000
    SEARCH_UNIT_CHARS = 2_200

    def __init__(
        self,
        system: str,
        task: str,
        max_chars: int,
        *,
        summarizer: SummaryHandler | None = None,
        recent_units: int = DEFAULT_RECENT_UNITS,
    ) -> None:
        self.max_chars = max_chars
        self.recent_units = self._validate_recent_units(recent_units)
        self._summarizer = summarizer
        self.messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        self.archived_units: list[list[Message]] = []
        self.compaction_count = 0
        self.last_compaction: JsonObject = {}

    @classmethod
    def restore(
        cls,
        system: str,
        conversation: list[Message],
        max_chars: int,
        *,
        summarizer: SummaryHandler | None = None,
        context_state: Any = None,
        recent_units: int = DEFAULT_RECENT_UNITS,
    ) -> "ContextManager":
        """Restore provider messages while replacing the machine-specific system prompt."""
        if not conversation:
            raise SessionError("saved conversation has no messages")
        restored: list[Message] = []
        for index, message in enumerate(conversation):
            restored.append(cls._validated_saved_message(message, index + 1))

        first_regular = next(
            (
                message
                for message in restored
                if not (
                    message.get("role") == "system"
                    and message.get("name") == cls.SUMMARY_NAME
                )
            ),
            None,
        )
        starts_with_summary = bool(
            restored
            and restored[0].get("role") == "system"
            and restored[0].get("name") == cls.SUMMARY_NAME
        )
        if first_regular is None or (
            first_regular.get("role") != "user" and not starts_with_summary
        ):
            raise SessionError("saved conversation must begin with a user message")

        instance = cls.__new__(cls)
        instance.max_chars = max_chars
        instance.recent_units = cls._validate_recent_units(recent_units)
        instance._summarizer = summarizer
        instance.messages = [{"role": "system", "content": system}, *restored]
        instance.archived_units = []
        instance.compaction_count = 0
        instance.last_compaction = {}
        instance._restore_state(context_state)
        return instance

    def export_conversation(self) -> list[Message]:
        """Exclude the system prompt because it contains the local workspace path."""
        return copy.deepcopy(self.messages[1:])

    def export_state(self) -> JsonObject:
        """Persist compressed raw history inside the existing session payload."""
        return {
            "version": 1,
            "recent_units": self.recent_units,
            "compaction_count": self.compaction_count,
            "archived_units": copy.deepcopy(self.archived_units),
        }

    def append(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def size_chars(self) -> int:
        """Return the same approximate size used by context compaction."""
        return self._size(self.messages)

    @property
    def archived_message_count(self) -> int:
        return sum(len(unit) for unit in self.archived_units)

    def compact(self, *, force: bool = False) -> bool:
        before_size = self._size(self.messages)
        self.last_compaction = {
            "compacted": False,
            "before_chars": before_size,
            "after_chars": before_size,
            "archived_messages": self.archived_message_count,
        }
        if not force and before_size <= self.max_chars:
            self.last_compaction["reason"] = "below_threshold"
            return False

        fixed = self.messages[:1]
        body: list[Message] = []
        previous_summary = ""
        for message in self.messages[1:]:
            if (
                message.get("role") == "system"
                and message.get("name") == self.SUMMARY_NAME
            ):
                previous_summary = self._strip_summary_prefix(
                    str(message.get("content") or "")
                )
                continue
            body.append(message)

        units = self._group_units(body)
        if len(units) <= 1:
            self.last_compaction["reason"] = "not_enough_history"
            return False

        keep_indices = set(range(max(0, len(units) - self.recent_units), len(units)))
        latest_user_index = next(
            (
                index
                for index in range(len(units) - 1, -1, -1)
                if units[index][0].get("role") == "user"
            ),
            None,
        )
        if latest_user_index is not None:
            keep_indices.add(latest_user_index)

        dropped = [
            unit for index, unit in enumerate(units) if index not in keep_indices
        ]
        if not dropped:
            self.last_compaction["reason"] = "not_enough_history"
            return False
        kept = [unit for index, unit in enumerate(units) if index in keep_indices]
        dropped_messages = [message for unit in dropped for message in unit]
        current_goal = ""
        if latest_user_index is not None:
            current_goal = self._message_text(units[latest_user_index][0], 4_000)
        summary, summary_source = self._build_summary(
            previous_summary, dropped_messages, current_goal
        )
        summary_message: Message = {
            "role": "system",
            "name": self.SUMMARY_NAME,
            "content": (
                f"{self.SUMMARY_PREFIX}\n{summary}\n\n"
                "If a missing detail matters, use search_history with specific keywords."
            ),
        }
        compacted = (
            fixed + [summary_message] + [message for unit in kept for message in unit]
        )
        after_size = self._size(compacted)
        if after_size >= before_size:
            self.last_compaction.update(
                {"reason": "summary_not_smaller", "summary_source": summary_source}
            )
            return False

        self.archived_units.extend(copy.deepcopy(dropped))
        self.messages = compacted
        self.compaction_count += 1
        self.last_compaction = {
            "compacted": True,
            "reason": "compacted",
            "before_chars": before_size,
            "after_chars": after_size,
            "before_messages": len(body) + 1,
            "after_messages": len(compacted),
            "archived_now": len(dropped_messages),
            "archived_messages": self.archived_message_count,
            "retained_units": len(kept),
            "summary_source": summary_source,
        }
        return True

    def search_history(self, query: str, max_results: int = 5) -> JsonObject:
        """Keyword-search raw messages removed from the active model context."""
        normalized = " ".join(query.split()).casefold()
        if not normalized:
            raise ToolError("history query must not be empty")
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            raise ToolError("max_results must be an integer")
        if not 1 <= max_results <= 10:
            raise ToolError("max_results must be between 1 and 10")
        terms = tuple(dict.fromkeys(part for part in normalized.split(" ") if part))
        ranked: list[tuple[int, int, list[Message]]] = []
        for index, unit in enumerate(self.archived_units):
            searchable = json.dumps(
                unit, ensure_ascii=False, separators=(",", ":")
            ).casefold()
            score = searchable.count(normalized) * 8
            score += sum(min(searchable.count(term), 6) for term in terms)
            if score > 0:
                ranked.append((score, index, unit))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)

        matches: list[JsonObject] = []
        for score, index, unit in ranked[:max_results]:
            entry: JsonObject = {
                "archive_unit": index + 1,
                "score": score,
                "messages": self._display_unit(unit),
            }
            if index > 0:
                entry["context_before"] = self._display_unit(
                    self.archived_units[index - 1], budget=900
                )
            if index + 1 < len(self.archived_units):
                entry["context_after"] = self._display_unit(
                    self.archived_units[index + 1], budget=900
                )
            matches.append(entry)
        return {
            "query": query,
            "count": len(matches),
            "archived_units": len(self.archived_units),
            "archived_messages": self.archived_message_count,
            "matches": matches,
        }

    def _restore_state(self, payload: Any) -> None:
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise SessionError("saved context state must be an object")
        version = payload.get("version", 1)
        if version != 1:
            raise SessionError(f"unsupported saved context state version: {version}")
        recent_units = payload.get("recent_units", self.recent_units)
        self.recent_units = self._validate_recent_units(recent_units)
        compaction_count = payload.get("compaction_count", 0)
        if (
            not isinstance(compaction_count, int)
            or isinstance(compaction_count, bool)
            or compaction_count < 0
        ):
            raise SessionError("saved context compaction count is invalid")
        raw_units = payload.get("archived_units", [])
        if not isinstance(raw_units, list) or len(raw_units) > 100_000:
            raise SessionError("saved archived history must be a bounded list")
        restored_units: list[list[Message]] = []
        message_index = 0
        for raw_unit in raw_units:
            if not isinstance(raw_unit, list) or not raw_unit:
                raise SessionError("saved archived history contains an invalid unit")
            unit: list[Message] = []
            for message in raw_unit:
                message_index += 1
                validated = self._validated_saved_message(message, message_index)
                if validated.get("role") == "system":
                    raise SessionError(
                        "saved archived history contains a system message"
                    )
                unit.append(validated)
            restored_units.append(unit)
        self.archived_units = restored_units
        self.compaction_count = compaction_count

    def _build_summary(
        self,
        previous_summary: str,
        dropped_messages: list[Message],
        current_goal: str,
    ) -> tuple[str, str]:
        if self._summarizer is not None:
            try:
                candidate = self._summarizer(
                    previous_summary, copy.deepcopy(dropped_messages), current_goal
                )
                normalized = self._normalize_structured_summary(candidate)
                if normalized is not None:
                    return normalized, "model"
            except OperationCancelled:
                raise
            except Exception:
                pass
        return (
            self._fallback_summary(previous_summary, dropped_messages, current_goal),
            "fallback",
        )

    @classmethod
    def _normalize_structured_summary(cls, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]).strip()
        if not text or len(text) > cls.MAX_SUMMARY_CHARS:
            return None
        positions: list[int] = []
        for section in cls.SUMMARY_SECTIONS:
            marker = f"## {section}"
            if text.count(marker) != 1:
                return None
            position = text.find(marker)
            if position < 0:
                return None
            positions.append(position)
        if positions != sorted(positions):
            return None
        return text

    @classmethod
    def _fallback_summary(
        cls, previous_summary: str, messages: list[Message], current_goal: str
    ) -> str:
        previous = cls._summary_sections(previous_summary)
        buckets: dict[str, list[str]] = {
            section: list(previous.get(section, [])) for section in cls.SUMMARY_SECTIONS
        }
        user_texts: list[str] = []
        if current_goal:
            for line in re.split(r"[\r\n。；;]+", current_goal):
                if re.search(
                    r"不要|不得|必须|只允许|限制|without|must|do not|never|only",
                    line,
                    flags=re.IGNORECASE,
                ):
                    cls._append_unique(buckets["Constraints"], line.strip())
        for message in messages:
            role = str(message.get("role") or "unknown")
            content = cls._message_text(message, 650)
            if role == "user" and content:
                user_texts.append(content)
                for line in re.split(r"[\r\n。；;]+", content):
                    if re.search(
                        r"不要|不得|必须|只允许|限制|without|must|do not|never|only",
                        line,
                        flags=re.IGNORECASE,
                    ):
                        cls._append_unique(buckets["Constraints"], line.strip())
            elif role == "assistant" and content:
                cls._append_unique(buckets["Key Decisions"], content)

            if role == "assistant" and message.get("tool_calls"):
                for call in message.get("tool_calls", []):
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "unknown")
                    arguments = str(function.get("arguments") or "{}")[:650]
                    detail = f"{name}({arguments})"
                    target = {
                        "write_file": "Code Changes",
                        "replace_text": "Code Changes",
                        "run_command": "Verification",
                        "update_plan": "Pending Tasks",
                    }.get(name, "Important References")
                    cls._append_unique(buckets[target], detail)
            elif role == "tool" and content:
                name = str(message.get("name") or "tool")
                target = {
                    "write_file": "Code Changes",
                    "replace_text": "Code Changes",
                    "run_command": "Verification",
                    "update_plan": "Pending Tasks",
                }.get(name, "Important References")
                cls._append_unique(buckets[target], f"{name}: {content}")

        selected_goal = current_goal or (user_texts[-1] if user_texts else "")
        if selected_goal:
            cls._append_unique(buckets["Current Goal"], selected_goal, prepend=True)
        if not buckets["Pending Tasks"]:
            buckets["Pending Tasks"].append(
                "Refer to the recent uncompressed messages."
            )

        parts: list[str] = []
        for section in cls.SUMMARY_SECTIONS:
            entries = buckets[section]
            section_lines: list[str] = []
            used = 0
            for entry in entries:
                clean = " ".join(str(entry).split())
                if not clean:
                    continue
                remaining = 1_200 - used
                if remaining <= 0:
                    break
                clean = clean[:remaining]
                section_lines.append(f"- {clean}")
                used += len(clean) + 2
            parts.extend(
                [
                    f"## {section}",
                    *(section_lines or ["- No confirmed information."]),
                    "",
                ]
            )
        return "\n".join(parts).strip()[: cls.MAX_SUMMARY_CHARS]

    @classmethod
    def _summary_sections(cls, summary: str) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        current: str | None = None
        for raw_line in cls._strip_summary_prefix(summary).splitlines():
            line = raw_line.strip()
            if line.startswith("## ") and line[3:] in cls.SUMMARY_SECTIONS:
                current = line[3:]
                result.setdefault(current, [])
            elif current and line and not line.startswith("["):
                result[current].append(line.removeprefix("- ").strip())
        return result

    @staticmethod
    def _append_unique(values: list[str], value: str, *, prepend: bool = False) -> None:
        clean = " ".join(value.split())
        if not clean or clean in values:
            return
        if prepend:
            values.insert(0, clean)
        else:
            values.append(clean)

    @classmethod
    def _display_unit(
        cls, unit: list[Message], *, budget: int = SEARCH_UNIT_CHARS
    ) -> list[JsonObject]:
        displayed: list[JsonObject] = []
        remaining = budget
        for message in unit:
            if remaining <= 0:
                break
            item: JsonObject = {"role": str(message.get("role") or "unknown")}
            if message.get("name"):
                item["name"] = str(message["name"])
            if message.get("tool_call_id"):
                item["tool_call_id"] = str(message["tool_call_id"])
            text = cls._message_text(message, min(700, remaining))
            if text:
                item["content"] = text
                remaining -= len(text)
            calls = message.get("tool_calls")
            if isinstance(calls, list) and remaining > 0:
                summaries: list[JsonObject] = []
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = str(function.get("arguments") or "")[
                        : min(400, remaining)
                    ]
                    summaries.append(
                        {
                            "name": str(function.get("name") or "unknown"),
                            "arguments": arguments,
                        }
                    )
                    remaining -= len(arguments)
                if summaries:
                    item["tool_calls"] = summaries
            displayed.append(item)
        return displayed

    @staticmethod
    def _message_text(message: Message, limit: int) -> str:
        content = message.get("content")
        if content is None:
            return ""
        if isinstance(content, str):
            text = content
        else:
            try:
                text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                text = str(content)
        compact = " ".join(text.split())
        return compact[:limit]

    @staticmethod
    def _size(messages: list[Message]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _group_units(messages: list[Message]) -> list[list[Message]]:
        """Keep each assistant tool call and all contiguous observations atomic."""
        units: list[list[Message]] = []
        index = 0
        while index < len(messages):
            current = messages[index]
            unit = [current]
            if current.get("role") == "assistant" and current.get("tool_calls"):
                expected = {
                    call.get("id")
                    for call in current.get("tool_calls", [])
                    if isinstance(call, dict) and call.get("id")
                }
                index += 1
                while index < len(messages):
                    candidate = messages[index]
                    if (
                        candidate.get("role") != "tool"
                        or candidate.get("tool_call_id") not in expected
                    ):
                        break
                    unit.append(candidate)
                    index += 1
                units.append(unit)
                continue
            units.append(unit)
            index += 1
        return units

    @classmethod
    def _strip_summary_prefix(cls, value: str) -> str:
        text = value.strip()
        if text.startswith(cls.SUMMARY_PREFIX):
            text = text[len(cls.SUMMARY_PREFIX) :].lstrip()
        suffix = (
            "If a missing detail matters, use search_history with specific keywords."
        )
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip()
        return text

    @staticmethod
    def _validate_recent_units(value: Any) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 6 <= value <= 10
        ):
            raise SessionError("recent context units must be between 6 and 10")
        return value

    @classmethod
    def _validated_saved_message(cls, message: Any, index: int) -> Message:
        if not isinstance(message, dict):
            raise SessionError(f"saved message {index} is not an object")
        role = message.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            raise SessionError(f"saved message {index} has an invalid role")
        if role == "system" and message.get("name") != cls.SUMMARY_NAME:
            raise SessionError(
                "saved conversation contains an untrusted system message"
            )
        return copy.deepcopy(message)
