from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .errors import RivetError, ToolError
from .outcomes import (
    command_outcome,
    denied_outcome,
    failed_outcome,
    rejected_outcome,
    successful_outcome,
)
from .plan import PlanState
from .types import EventHandler, JsonObject
from .workspace import Workspace


Approver = Callable[[str, str], bool]
SkillHandler = Callable[..., JsonObject]
HistorySearchHandler = Callable[..., JsonObject]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: JsonObject
    handler: Callable[..., dict[str, Any]]
    mutating: bool = False

    def api_schema(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(
        self,
        config: Config,
        approver: Approver | None = None,
        *,
        plan: PlanState | None = None,
        event_handler: EventHandler | None = None,
        cancel_event: threading.Event | None = None,
        workspace: Workspace | None = None,
        skill_list_handler: SkillHandler | None = None,
        skill_activate_handler: SkillHandler | None = None,
        skill_resource_handler: SkillHandler | None = None,
        history_search_handler: HistorySearchHandler | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace or Workspace(
            config.workspace,
            max_output_chars=config.max_tool_output_chars,
            cancel_event=cancel_event,
        )
        self.approver = approver
        self.plan = plan if plan is not None else PlanState()
        self.events = event_handler or (lambda _event, _data: None)
        self.skill_list_handler = skill_list_handler
        self.skill_activate_handler = skill_activate_handler
        self.skill_resource_handler = skill_resource_handler
        self.history_search_handler = history_search_handler
        self._tools = {tool.name: tool for tool in self._build_tools()}

    @property
    def schemas(self) -> list[JsonObject]:
        return [tool.api_schema() for tool in self._tools.values()]

    def execute(self, name: str, raw_arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return rejected_outcome(f"unknown tool: {name}", code="UNKNOWN_TOOL")
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return rejected_outcome(
                f"arguments are not valid JSON: {exc.msg}", code="INVALID_ARGUMENT"
            )
        if not isinstance(arguments, dict):
            return rejected_outcome(
                "tool arguments must be a JSON object", code="INVALID_ARGUMENT"
            )

        validation_error = self._validate_value(tool.parameters, arguments)
        if validation_error is not None:
            message, field = validation_error
            return rejected_outcome(message, code="INVALID_ARGUMENT", field=field)

        if tool.mutating:
            if self.config.approval_mode == "never":
                return denied_outcome(
                    "mutating tools are disabled by approval mode",
                    code="APPROVAL_REQUIRED",
                )
            review_reason = None
            if name == "run_command":
                review_reason = self.workspace.command_review_reason(
                    arguments["command"]
                )
            needs_approval = self.config.approval_mode == "ask" or (
                self.config.approval_mode == "safe" and review_reason is not None
            )
            if needs_approval:
                summary = json.dumps(arguments, ensure_ascii=False)[:500]
                if review_reason:
                    summary = f"[{review_reason}] {summary}"
                if self.approver is None or not self.approver(name, summary):
                    return denied_outcome(
                        "operation was not approved by the user",
                        code="APPROVAL_REQUIRED",
                    )

        try:
            result = tool.handler(**arguments)
            if name == "run_command":
                return command_outcome(result)
            return successful_outcome(result)
        except (RivetError, OSError) as exc:
            return failed_outcome(str(exc), code="TOOL_ERROR", retryable=True)
        except Exception as exc:  # defensive boundary: never crash the agent loop
            return failed_outcome(
                f"unexpected {type(exc).__name__}: {exc}",
                code="INTERNAL_ERROR",
                retryable=False,
                result_unknown=True,
            )

    @classmethod
    def _validate_value(
        cls, schema: JsonObject, value: Any, field: str = ""
    ) -> tuple[str, str | None] | None:
        expected = schema.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if isinstance(expected, str) and not type_matches.get(expected, True):
            label = field or "arguments"
            return f"{label} must be of type {expected}", field or None

        if expected == "object" and isinstance(value, dict):
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            for name in required:
                if name not in value:
                    child = f"{field}.{name}" if field else name
                    return f"missing required argument: {child}", child
            if schema.get("additionalProperties") is False:
                extras = sorted(set(value) - set(properties))
                if extras:
                    child = f"{field}.{extras[0]}" if field else extras[0]
                    return f"unexpected argument: {child}", child
            for name, item in value.items():
                item_schema = properties.get(name)
                if isinstance(item_schema, dict):
                    child = f"{field}.{name}" if field else name
                    error = cls._validate_value(item_schema, item, child)
                    if error is not None:
                        return error

        if expected == "array" and isinstance(value, list):
            minimum = schema.get("minItems")
            maximum = schema.get("maxItems")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"{field} must contain at least {minimum} item(s)", field
            if isinstance(maximum, int) and len(value) > maximum:
                return f"{field} must contain at most {maximum} item(s)", field
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    item_field = f"{field}[{index}]" if field else f"[{index}]"
                    error = cls._validate_value(item_schema, item, item_field)
                    if error is not None:
                        return error

        if expected == "string" and isinstance(value, str):
            minimum = schema.get("minLength")
            maximum = schema.get("maxLength")
            if isinstance(minimum, int) and len(value) < minimum:
                return f"{field} must contain at least {minimum} character(s)", field
            if isinstance(maximum, int) and len(value) > maximum:
                return f"{field} must not exceed {maximum} characters", field

        if (
            expected == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
        ):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                return f"{field} must be at least {minimum}", field
            if isinstance(maximum, int) and value > maximum:
                return f"{field} must be at most {maximum}", field

        choices = schema.get("enum")
        if isinstance(choices, list) and value not in choices:
            return f"{field} must be one of: {', '.join(map(str, choices))}", field
        return None

    def _build_tools(self) -> tuple[ToolSpec, ...]:
        object_schema = {"type": "object", "additionalProperties": False}
        tools = [
            ToolSpec(
                "update_plan",
                "Create or update a concise progress plan for a multi-stage task. "
                "Keep at most one step in progress, update it after meaningful progress, "
                "and make every step completed or blocked before the final answer.",
                {
                    **object_schema,
                    "properties": {
                        "explanation": {
                            "type": "string",
                            "maxLength": 1000,
                            "description": "Short reason for creating or revising the plan",
                        },
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 20,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "step": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 240,
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": [
                                            "pending",
                                            "in_progress",
                                            "completed",
                                            "blocked",
                                        ],
                                    },
                                },
                                "required": ["step", "status"],
                            },
                        },
                    },
                    "required": ["steps"],
                },
                self._update_plan,
            ),
            ToolSpec(
                "list_files",
                "List workspace files and directories recursively. Hidden build/cache directories are skipped.",
                {
                    **object_schema,
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": "Workspace-relative directory",
                        },
                        "depth": {"type": "integer", "minimum": 0, "maximum": 8},
                        "max_entries": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                        },
                    },
                },
                self.workspace.list_files,
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file with stable one-based line numbers.",
                {
                    **object_schema,
                    "properties": {
                        "path": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                self.workspace.read_file,
            ),
            ToolSpec(
                "search_text",
                "Search UTF-8 workspace files for literal text and return matching lines.",
                {
                    **object_schema,
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 10000},
                        "path": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "file_glob": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": "For example *.py",
                        },
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "case_sensitive": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
                self.workspace.search_text,
            ),
        ]
        if self.history_search_handler is not None:
            tools.append(
                ToolSpec(
                    "search_history",
                    "Keyword-search raw messages archived by context compression in the "
                    "current session. Use it only when the structured summary lacks an "
                    "important historical detail. This is read-only and uses no embeddings "
                    "or vector database.",
                    {
                        **object_schema,
                        "properties": {
                            "query": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500,
                            },
                            "max_results": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                            },
                        },
                        "required": ["query"],
                    },
                    self.history_search_handler,
                )
            )
        tools.extend(
            [
                ToolSpec(
                    "write_file",
                    "Create or replace a UTF-8 file atomically. Parent directories are created.",
                    {
                        **object_schema,
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                            "content": {"type": "string", "maxLength": 2000000},
                        },
                        "required": ["path", "content"],
                    },
                    self.workspace.write_file,
                    mutating=True,
                ),
                ToolSpec(
                    "replace_text",
                    "Atomically replace exact text in a UTF-8 file. Fails if the old text is absent.",
                    {
                        **object_schema,
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                            "old": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 500000,
                            },
                            "new": {"type": "string", "maxLength": 500000},
                            "count": {"type": "integer", "minimum": 1},
                        },
                        "required": ["path", "old", "new"],
                    },
                    self.workspace.replace_text,
                    mutating=True,
                ),
                ToolSpec(
                    "show_diff",
                    "Show unified diffs for files changed by Rivet during the current task.",
                    {
                        **object_schema,
                        "properties": {
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                            "context_lines": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 20,
                            },
                        },
                    },
                    self.workspace.show_diff,
                ),
                ToolSpec(
                    "run_command",
                    "Run a shell command in the workspace. Output and exit status are bounded; "
                    "created, modified, and deleted workspace files are detected and reported.",
                    {
                        **object_schema,
                        "properties": {
                            "command": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 10000,
                            },
                            "timeout": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 600,
                            },
                            "purpose": {
                                "type": "string",
                                "enum": ["auto", "inspect", "verify"],
                                "description": (
                                    "Use verify only when the command checks the latest changes; "
                                    "inspect commands never satisfy completion evidence."
                                ),
                            },
                        },
                        "required": ["command"],
                    },
                    lambda command, timeout=self.config.command_timeout, purpose="auto": self.workspace.run_command(
                        command, timeout, purpose
                    ),
                    mutating=True,
                ),
            ]
        )
        if self.skill_list_handler is not None:
            tools.append(
                ToolSpec(
                    "list_skills",
                    "List the available reusable skills and their activation state. Skill "
                    "instructions are intentionally omitted until activate_skill is called.",
                    object_schema,
                    self.skill_list_handler,
                )
            )
        if self.skill_activate_handler is not None:
            tools.append(
                ToolSpec(
                    "activate_skill",
                    "Load one relevant SKILL.md workflow into the current turn. Use this when "
                    "the user names a skill or the catalog shows a clear task match.",
                    {
                        **object_schema,
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            }
                        },
                        "required": ["name"],
                    },
                    self.skill_activate_handler,
                )
            )
        if self.skill_resource_handler is not None:
            tools.append(
                ToolSpec(
                    "read_skill_resource",
                    "Read one UTF-8 resource explicitly listed by an already active skill. "
                    "This never executes the resource.",
                    {
                        **object_schema,
                        "properties": {
                            "skill": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                            "path": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                        },
                        "required": ["skill", "path"],
                    },
                    self.skill_resource_handler,
                )
            )
        return tuple(tools)

    def _update_plan(
        self, steps: list[JsonObject], explanation: str = ""
    ) -> JsonObject:
        result = self.plan.update(steps, explanation)
        if result["changed"]:
            self.events("plan_updated", result)
        return result
