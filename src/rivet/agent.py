from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .config import Config
from .context import ContextManager, STRUCTURED_SUMMARY_INSTRUCTIONS
from .errors import OperationCancelled, SessionError
from .plan import PlanState
from .prompt import system_prompt
from .skills import SkillRegistry
from .subagents import SubAgentManager
from .tools import Approver, ToolRegistry
from .types import EventHandler, JsonObject, Message, ModelClient, ModelReply, ToolCall
from .workspace import Workspace


@dataclass
class TaskState:
    """Program-owned evidence collected throughout one user conversation."""

    inspected_files: set[str] = field(default_factory=set)
    changed_files: set[str] = field(default_factory=set)
    commands: list[JsonObject] = field(default_factory=list)
    operation_index: int = 0
    last_mutation_operation: int = 0
    last_successful_command_operation: int = 0
    workspace_tracking_complete: bool = True

    @classmethod
    def restore(cls, payload: Any) -> "TaskState":
        if not isinstance(payload, dict):
            raise SessionError("saved task state must be an object")

        def string_set(name: str) -> set[str]:
            value = payload.get(name, [])
            if (
                not isinstance(value, list)
                or len(value) > 10_000
                or any(not isinstance(item, str) for item in value)
            ):
                raise SessionError(f"saved {name} must be a list of strings")
            return set(value)

        def counter(name: str) -> int:
            value = payload.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SessionError(f"saved {name} must be a non-negative integer")
            return value

        commands = payload.get("commands", [])
        if (
            not isinstance(commands, list)
            or len(commands) > 10_000
            or any(not isinstance(command, dict) for command in commands)
        ):
            raise SessionError("saved commands must be a list of objects")
        tracking = payload.get("workspace_tracking_complete", True)
        if not isinstance(tracking, bool):
            raise SessionError("saved workspace tracking flag must be boolean")

        operation_index = counter("operation_index")
        last_mutation = counter("last_mutation_operation")
        last_successful = counter("last_successful_command_operation")
        if last_mutation > operation_index or last_successful > operation_index:
            raise SessionError("saved task operation indexes are inconsistent")
        return cls(
            inspected_files=string_set("inspected_files"),
            changed_files=string_set("changed_files"),
            commands=copy.deepcopy(commands),
            operation_index=operation_index,
            last_mutation_operation=last_mutation,
            last_successful_command_operation=last_successful,
            workspace_tracking_complete=tracking,
        )

    def record_tool_result(self, name: str, result: str) -> None:
        self.operation_index += 1
        try:
            payload: Any = json.loads(result)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or not payload.get("ok"):
            return

        path = payload.get("path")
        if name == "read_file" and isinstance(path, str):
            self.inspected_files.add(path)
        elif name == "search_text":
            for match in payload.get("matches", []):
                match_path = match.get("path") if isinstance(match, dict) else None
                if isinstance(match_path, str):
                    self.inspected_files.add(match_path)
        elif name in {"write_file", "replace_text"} and isinstance(path, str):
            self.changed_files.add(path)
            self.last_mutation_operation = self.operation_index
        elif name == "run_command":
            file_changes = payload.get("file_changes", [])
            changed_by_command: list[JsonObject] = []
            if isinstance(file_changes, list):
                for change in file_changes:
                    if not isinstance(change, dict):
                        continue
                    change_path = change.get("path")
                    if isinstance(change_path, str):
                        self.changed_files.add(change_path)
                        changed_by_command.append(change)
            if changed_by_command:
                self.last_mutation_operation = self.operation_index
            if payload.get("tracking_complete") is False:
                self.workspace_tracking_complete = False
            command_record: JsonObject = {
                "command": str(payload.get("command") or ""),
                "exit_code": payload.get("exit_code"),
                "timed_out": bool(payload.get("timed_out")),
                "cancelled": bool(payload.get("cancelled")),
                "verification": bool(payload.get("verification")),
                "purpose": str(payload.get("purpose") or "auto"),
                "file_changes": changed_by_command,
                "file_change_count": payload.get(
                    "file_change_count", len(changed_by_command)
                ),
                "file_changes_truncated": bool(payload.get("file_changes_truncated")),
                "tracking_complete": payload.get("tracking_complete") is not False,
                "stdout": str(payload.get("stdout") or "")[:8_000],
                "stderr": str(payload.get("stderr") or "")[:8_000],
                "duration_ms": payload.get("duration_ms"),
            }
            self.commands.append(command_record)
            if (
                command_record["verification"]
                and command_record["exit_code"] == 0
                and not command_record["timed_out"]
                and not command_record["cancelled"]
            ):
                self.last_successful_command_operation = self.operation_index

    @property
    def verification_required(self) -> bool:
        return bool(self.changed_files)

    @property
    def verification_passed(self) -> bool:
        return (
            not self.verification_required
            or self.last_successful_command_operation > self.last_mutation_operation
        )

    def snapshot(self) -> JsonObject:
        return {
            "inspected_files": sorted(self.inspected_files),
            "changed_files": sorted(self.changed_files),
            "commands": list(self.commands),
            "verification_required": self.verification_required,
            "verification_passed": self.verification_passed,
            "workspace_tracking_complete": self.workspace_tracking_complete,
        }

    def export_state(self) -> JsonObject:
        return {
            "inspected_files": sorted(self.inspected_files),
            "changed_files": sorted(self.changed_files),
            "commands": copy.deepcopy(self.commands),
            "operation_index": self.operation_index,
            "last_mutation_operation": self.last_mutation_operation,
            "last_successful_command_operation": self.last_successful_command_operation,
            "workspace_tracking_complete": self.workspace_tracking_complete,
        }

    def record_external_changes(self, paths: list[str]) -> None:
        if not paths:
            return
        self.operation_index += 1
        self.last_mutation_operation = self.operation_index
        self.changed_files.update(paths)

    def record_revert(self, reverted: list[str], remaining: list[str]) -> None:
        if not reverted:
            return
        self.operation_index += 1
        self.changed_files = {item for item in remaining if isinstance(item, str)}
        if self.changed_files:
            self.last_mutation_operation = self.operation_index


@dataclass(frozen=True)
class AgentResult:
    success: bool
    final: str
    steps: int
    reason: str
    messages: tuple[Message, ...]
    state: JsonObject = field(default_factory=dict)


class Agent:
    """A stateful user conversation containing one or more programming turns."""

    def __init__(
        self,
        config: Config,
        client: ModelClient,
        *,
        event_handler: EventHandler | None = None,
        approver: Approver | None = None,
        client_factory: Callable[[], ModelClient] | None = None,
        workspace: Workspace | None = None,
        tool_scope: str = "full",
        enable_delegation: bool = True,
        cancel_event: threading.Event | None = None,
        system_prompt_text: str | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self._parallel_delegation = client_factory is not None
        self.client_factory = client_factory or (lambda: self.client)
        self.events = event_handler or (lambda _event, _data: None)
        self.approver = approver
        self._shared_workspace = workspace
        self._tool_scope = tool_scope
        self._enable_delegation = enable_delegation
        self._cancel_event = cancel_event or threading.Event()
        self._owns_cancel_event = cancel_event is None
        self.skills = (
            SkillRegistry(self.config.workspace, event_handler=self.events)
            if tool_scope == "full" and system_prompt_text is None
            else None
        )
        self._system_prompt = system_prompt_text or system_prompt(
            self.config.workspace,
            (
                self.skills.catalog_prompt()
                if self.skills is not None
                else "- No skills are currently available."
            ),
        )
        self._run_state_lock = threading.Lock()
        self._running = False
        self.reset()

    def reset(self) -> None:
        """Start a new conversation and a new in-memory workspace diff baseline."""
        if self.skills is not None:
            self.skills.reset_session()
            self._system_prompt = system_prompt(
                self.config.workspace, self.skills.catalog_prompt()
            )
        self.plan = PlanState()
        workspace = self._shared_workspace or Workspace(
            self.config.workspace,
            max_output_chars=self.config.max_tool_output_chars,
            cancel_event=self._cancel_event,
        )
        self.tools, self.subagents = self._build_runtime(self.plan, workspace)
        self.context: ContextManager | None = None
        self.transcript: list[JsonObject] = []
        self.state = TaskState()
        self.tasks: list[str] = []
        self.turns = 0
        self.total_steps = 0
        self.last_result: AgentResult | None = None

    @property
    def messages(self) -> tuple[Message, ...]:
        if self.context is None:
            return ()
        return tuple(self.context.messages)

    def status(self) -> JsonObject:
        context_status = (
            {
                "archived_messages": self.context.archived_message_count,
                "compactions": self.context.compaction_count,
                "recent_units": self.context.recent_units,
            }
            if self.context is not None
            else {"archived_messages": 0, "compactions": 0, "recent_units": 8}
        )
        return {
            "turns": self.turns,
            "total_steps": self.total_steps,
            "messages": len(self.messages),
            "context_chars": self.context.size_chars if self.context is not None else 0,
            "context_history": context_status,
            "approval_mode": self.config.approval_mode,
            "recovery": self.recovery_snapshot(),
            "operations": self.tools.workspace.operation_history(),
            "plan": self.plan.snapshot(),
            "subagents": (
                self.subagents.snapshot()
                if self.subagents
                else {
                    "active": [],
                    "history": [],
                }
            ),
            "skills": (
                self.skills.snapshot()
                if self.skills is not None
                else {
                    "available": [],
                    "active": [],
                    "history": [],
                    "errors": [],
                }
            ),
            **self.state.snapshot(),
        }

    def plan_snapshot(self) -> JsonObject:
        return self.plan.snapshot()

    def skill_snapshot(self) -> JsonObject:
        if self.skills is None:
            return {"available": [], "active": [], "history": [], "errors": []}
        return self.skills.snapshot()

    def set_approval_mode(self, mode: str) -> JsonObject:
        """Change the mutation approval policy while the Agent is idle."""
        normalized = mode.strip().lower()
        if normalized not in {"safe", "ask", "never"}:
            raise SessionError("approval mode must be safe, ask, or never")
        with self._run_state_lock:
            if self._running:
                raise SessionError("wait for the current turn to finish")
            changed = normalized != self.config.approval_mode
            if changed:
                updated = replace(self.config, approval_mode=normalized)
                self.config = updated
                self.tools.config = updated
                if self.subagents is not None:
                    self.subagents.config = updated
        return {"mode": normalized, "changed": changed}

    def show_diff(self, path: str | None = None) -> JsonObject:
        return self.tools.workspace.show_diff(path)

    def revert_changes(self, path: str | None = None) -> JsonObject:
        result = self.tools.workspace.revert_changes(path)
        reverted = result.get("reverted", [])
        remaining = result.get("remaining", [])
        self.state.record_revert(
            reverted if isinstance(reverted, list) else [],
            remaining if isinstance(remaining, list) else [],
        )
        return result

    def undo_operation(self, operation_id: int) -> JsonObject:
        """Undo all workspace changes produced by one completed conversation turn."""
        result = self.tools.workspace.undo_operation(operation_id)
        reverted = result.get("files", [])
        remaining = result.get("remaining", [])
        self.state.record_revert(
            reverted if isinstance(reverted, list) else [],
            remaining if isinstance(remaining, list) else [],
        )
        return result

    def compact_context(self) -> JsonObject:
        """Force one safe context compaction without discarding the UI transcript."""
        if self.context is None:
            return {
                "compacted": False,
                "reason": "empty_context",
                "before_chars": 0,
                "after_chars": 0,
                "before_messages": 0,
                "after_messages": 0,
            }
        self.context.compact(force=True)
        return dict(self.context.last_compaction)

    def recovery_snapshot(self) -> JsonObject:
        """Describe whether the last failed turn can continue or safely restart."""
        result = self.last_result
        if result is None or result.success or not self.tasks:
            return {"available": False}

        operation = next(
            (
                item
                for item in reversed(self.tools.workspace.operation_history())
                if item.get("turn") == self.turns
            ),
            None,
        )
        changed_at_failure = result.state.get("changed_files", [])
        can_retry = operation is None and not changed_at_failure
        operation_id: int | None = None
        blocked_reason = ""
        if operation is not None:
            status = operation.get("status")
            if status == "undone":
                can_retry = True
            elif operation.get("can_undo") is True:
                can_retry = True
                raw_id = operation.get("id")
                operation_id = raw_id if isinstance(raw_id, int) else None
            else:
                blocked_reason = str(
                    operation.get("blocked_reason") or "无法安全恢复失败前的文件状态"
                )
        elif changed_at_failure:
            blocked_reason = "未找到完整的失败轮次撤销点，无法保证安全重试"
        return {
            "available": True,
            "turn": self.turns,
            "task": self.tasks[-1],
            "reason": result.reason,
            "final": result.final,
            "can_continue": True,
            "can_retry": can_retry,
            "retry_operation_id": operation_id,
            "retry_blocked_reason": blocked_reason,
        }

    def prepare_retry(self, operation_id: int | None) -> JsonObject:
        """Restore the failed turn checkpoint and reset its unfinished plan."""
        recovery = self.recovery_snapshot()
        if recovery.get("available") is not True:
            raise SessionError("there is no failed turn to retry")
        if recovery.get("can_retry") is not True:
            raise SessionError(
                str(
                    recovery.get("retry_blocked_reason")
                    or "this turn cannot be retried"
                )
            )
        expected = recovery.get("retry_operation_id")
        if expected is not None and operation_id != expected:
            raise SessionError("the retry checkpoint is no longer current")
        restored: JsonObject = {"files": [], "file_count": 0}
        if isinstance(expected, int):
            restored = self.undo_operation(expected)
        self.plan.clear()
        return restored

    def export_session_state(self) -> JsonObject:
        if self.context is None or self.turns <= 0:
            raise SessionError("there is no completed conversation to save")
        return {
            "tasks": list(self.tasks),
            "turns": self.turns,
            "total_steps": self.total_steps,
            "conversation": self.context.export_conversation(),
            "context_state": self.context.export_state(),
            "transcript": copy.deepcopy(self.transcript),
            "last_result": self._saved_result(),
            "plan_state": self.plan.export_state(),
            "task_state": self.state.export_state(),
            "workspace_state": self.tools.workspace.export_diff_state(),
            "subagent_state": self.subagents.export_state() if self.subagents else None,
            "skill_state": (
                self.skills.export_state() if self.skills is not None else None
            ),
        }

    def restore_session_state(self, payload: Any) -> list[str]:
        """Replace this Agent's state with one validated saved conversation."""
        if not isinstance(payload, dict):
            raise SessionError("saved agent state must be an object")
        tasks = payload.get("tasks")
        if (
            not isinstance(tasks, list)
            or not tasks
            or len(tasks) > 10_000
            or any(not isinstance(task, str) or not task.strip() for task in tasks)
        ):
            raise SessionError("saved tasks must be a non-empty list of strings")

        turns = self._saved_counter(payload, "turns")
        total_steps = self._saved_counter(payload, "total_steps")
        if turns != len(tasks):
            raise SessionError("saved turn count does not match the task history")
        conversation = payload.get("conversation")
        if not isinstance(conversation, list):
            raise SessionError("saved conversation must be a list")

        restored_context = ContextManager.restore(
            self._system_prompt,
            conversation,
            self.config.max_context_chars,
            summarizer=self._summarize_context,
            context_state=payload.get("context_state"),
        )
        restored_transcript = self._restore_transcript(
            payload.get("transcript"), conversation, tasks, turns
        )
        restored_plan = PlanState.restore(payload.get("plan_state"))
        restored_state = TaskState.restore(payload.get("task_state"))
        restored_workspace = Workspace(
            self.config.workspace,
            max_output_chars=self.config.max_tool_output_chars,
            cancel_event=self._cancel_event,
        )
        try:
            drifted = restored_workspace.restore_diff_state(
                payload.get("workspace_state")
            )
        except Exception as exc:
            if isinstance(exc, SessionError):
                raise
            raise SessionError(f"saved workspace state is invalid: {exc}") from exc
        restored_state.record_external_changes(drifted)

        restored_tools, restored_subagents = self._build_runtime(
            restored_plan, restored_workspace
        )
        if restored_subagents is not None:
            restored_subagents.restore_state(payload.get("subagent_state"))
        if self.skills is not None:
            try:
                self.skills.restore_state(payload.get("skill_state"))
            except ValueError as exc:
                raise SessionError(f"saved skill state is invalid: {exc}") from exc

        self.tools = restored_tools
        self.subagents = restored_subagents
        self.plan = restored_plan
        self.context = restored_context
        self.transcript = restored_transcript
        self.state = restored_state
        self.tasks = list(tasks)
        self.turns = turns
        self.total_steps = total_steps
        self.last_result = self._restore_last_result(payload.get("last_result"))
        return drifted

    def run(self, task: str) -> AgentResult:
        """Run one user turn while retaining earlier messages and workspace evidence."""
        checkpoint_started = False
        with self._run_state_lock:
            if self._running:
                raise RuntimeError("agent is already running")
            if self._owns_cancel_event:
                self._cancel_event.clear()
            reset_cancel = getattr(self.client, "reset_cancel", None)
            if callable(reset_cancel):
                reset_cancel()
            self._running = True
            if self.skills is not None:
                self.skills.begin_turn(self.turns + 1)
            if self.subagents is not None:
                self.subagents.begin_turn()
        try:
            if task.strip() and self._tool_scope == "full":
                self.tools.workspace.begin_turn_operation(self.turns + 1, task)
                checkpoint_started = True
            return self._run_turn(task)
        finally:
            if checkpoint_started:
                try:
                    self.tools.workspace.finish_turn_operation()
                except Exception as exc:
                    self.events(
                        "checkpoint_error",
                        {"message": str(exc), "turn": self.turns},
                    )
            with self._run_state_lock:
                self._running = False

    def request_cancel(self) -> bool:
        """Request cooperative cancellation of the active model or tool operation."""
        with self._run_state_lock:
            if not self._running:
                return False
            self._cancel_event.set()
        cancel_client = getattr(self.client, "cancel", None)
        if callable(cancel_client):
            cancel_client()
        if self.subagents is not None:
            self.subagents.cancel_active()
        return True

    def record_failure(self, final: str, reason: str = "runtime_error") -> AgentResult:
        """Turn a provider/runtime exception into a persisted recoverable result."""
        if self.context is None or self.turns <= 0:
            raise SessionError("there is no active conversation turn to recover")
        message = final.strip() or "The task stopped because of a runtime error."
        self.context.append({"role": "assistant", "content": message})
        return self._finish(
            False,
            message,
            0,
            reason,
            tuple(self.context.messages),
            self._result_state(self.state),
        )

    def _run_turn(self, task: str) -> AgentResult:
        if not task.strip():
            return AgentResult(
                False,
                "Task is empty.",
                0,
                "empty_task",
                self.messages,
                self._result_state(self.state),
            )

        normalized_task = task.strip()
        if self.plan.terminal:
            self.plan.clear()
        if self.context is None:
            self.context = ContextManager(
                self._system_prompt,
                normalized_task,
                self.config.max_context_chars,
                summarizer=self._summarize_context,
            )
        else:
            self.context.append({"role": "user", "content": normalized_task})

        self.tasks.append(normalized_task)
        self.turns += 1
        self.last_result = None
        context = self.context
        state = self.state
        turn = self.turns
        self._append_transcript("user", normalized_task, turn)
        turn_start_operation = state.operation_index
        verification_pending_at_start = (
            state.verification_required and not state.verification_passed
        )
        previous_observation: str | None = None
        repeat_count = 0
        empty_replies = 0
        completion_reprompts = 0
        plan_completion_reprompts = 0

        for step in range(1, self.config.max_steps + 1):
            compacted = context.compact()
            if compacted:
                self.events(
                    "context_compacted",
                    {
                        "messages": len(context.messages),
                        "turn": turn,
                        **context.last_compaction,
                    },
                )
            self.events("model_start", {"step": step, "turn": turn})
            try:
                self._raise_if_cancelled()
                reply, streamed = self._complete_model(context.messages, step, turn)
                self._raise_if_cancelled()
            except (KeyboardInterrupt, OperationCancelled):
                return self._cancelled_result(
                    context, state, step, turn, "model request"
                )
            assistant_message = self._assistant_message(
                reply.content, reply.tool_calls, reply.extensions
            )
            context.append(assistant_message)
            if reply.content.strip():
                self._append_transcript("assistant", reply.content.strip(), turn)

            if reply.content.strip():
                self.events(
                    "assistant_text",
                    {
                        "text": reply.content,
                        "turn": turn,
                        "has_tool_calls": bool(reply.tool_calls),
                        "streamed": streamed,
                    },
                )

            if reply.tool_calls:
                empty_replies = 0
                for call_index, call in enumerate(reply.tool_calls):
                    self.events(
                        "tool_start",
                        {
                            "step": step,
                            "turn": turn,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    try:
                        self._raise_if_cancelled()
                        result = self.tools.execute(call.name, call.arguments)
                    except (KeyboardInterrupt, OperationCancelled):
                        result = self._cancelled_tool_payload(call.name)
                    state.record_tool_result(call.name, result)
                    context.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": result,
                        }
                    )
                    self.events(
                        "tool_end",
                        {
                            "step": step,
                            "turn": turn,
                            "name": call.name,
                            "result": result,
                        },
                    )
                    if self._tool_was_cancelled(result) or self._cancel_event.is_set():
                        for pending in reply.tool_calls[call_index + 1 :]:
                            context.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": pending.id,
                                    "name": pending.name,
                                    "content": self._cancelled_tool_payload(
                                        pending.name, skipped=True
                                    ),
                                }
                            )
                        return self._cancelled_result(
                            context, state, step, turn, f"tool {call.name}"
                        )
                    observation = self._observation_signature(call, result)
                    if observation == previous_observation:
                        repeat_count += 1
                    else:
                        previous_observation = observation
                        repeat_count = 1
                    if repeat_count >= 3:
                        final = (
                            "Stopped after the same tool call produced the same result "
                            f"three times: {call.name}."
                        )
                        self.events(
                            "stopped",
                            {
                                "reason": "repeated_tool_call",
                                "tool": call.name,
                                "turn": turn,
                            },
                        )
                        return self._finish(
                            False,
                            final,
                            step,
                            "repeated_tool_call",
                            tuple(context.messages),
                            self._result_state(state),
                        )
                continue

            if reply.content.strip():
                if self.plan.active and not self.plan.terminal:
                    plan_completion_reprompts += 1
                    if plan_completion_reprompts >= 2:
                        final = (
                            "The model tried to finish while the active plan still had "
                            "pending or in-progress steps."
                        )
                        self.events(
                            "stopped",
                            {"reason": "incomplete_plan", "turn": turn},
                        )
                        return self._finish(
                            False,
                            final,
                            step,
                            "incomplete_plan",
                            tuple(context.messages),
                            self._result_state(state),
                        )
                    self.events(
                        "plan_completion_required",
                        {"plan": self.plan.snapshot(), "step": step, "turn": turn},
                    )
                    context.append(
                        {
                            "role": "user",
                            "content": (
                                "Your active plan still has pending or in-progress steps. "
                                "Continue the work, then call update_plan so every step is "
                                "completed or genuinely blocked before the final answer."
                            ),
                        }
                    )
                    continue
                if state.verification_required and not state.verification_passed:
                    completion_reprompts += 1
                    if completion_reprompts >= 2:
                        final = (
                            "Files were changed, but no successful verification command "
                            "was run after the latest change."
                        )
                        self.events(
                            "stopped",
                            {"reason": "unverified_changes", "turn": turn},
                        )
                        return self._finish(
                            False,
                            final,
                            step,
                            "unverified_changes",
                            tuple(context.messages),
                            self._result_state(state),
                        )
                    self.events(
                        "verification_required",
                        {
                            "files": sorted(state.changed_files),
                            "step": step,
                            "turn": turn,
                        },
                    )
                    context.append(
                        {
                            "role": "user",
                            "content": (
                                "You changed files but have not run a successful verification "
                                "command after the latest change. Inspect the diff, run the "
                                "narrowest relevant check, and only then give a final answer."
                            ),
                        }
                    )
                    continue
                if self.plan.blocked:
                    self.events("stopped", {"reason": "blocked", "turn": turn})
                    return self._finish(
                        False,
                        reply.content.strip(),
                        step,
                        "blocked",
                        tuple(context.messages),
                        self._result_state(state),
                    )
                self.events("completed", {"step": step, "turn": turn})
                changed_this_turn = state.last_mutation_operation > turn_start_operation
                verified_pending_change = (
                    verification_pending_at_start
                    and state.verification_passed
                    and state.last_successful_command_operation > turn_start_operation
                )
                reason = (
                    "completed_verified"
                    if changed_this_turn or verified_pending_change
                    else "completed"
                )
                return self._finish(
                    True,
                    reply.content.strip(),
                    step,
                    reason,
                    tuple(context.messages),
                    self._result_state(state),
                )

            empty_replies += 1
            if empty_replies >= 2:
                final = "Model returned two empty responses without tool calls."
                self.events("stopped", {"reason": "empty_model_response", "turn": turn})
                return self._finish(
                    False,
                    final,
                    step,
                    "empty_model_response",
                    tuple(context.messages),
                    self._result_state(state),
                )
            context.append(
                {
                    "role": "user",
                    "content": "Your last response was empty. Continue the task using tools or give a final answer.",
                }
            )

        final = f"Stopped after reaching the {self.config.max_steps}-step limit."
        self.events("stopped", {"reason": "max_steps", "turn": turn})
        return self._finish(
            False,
            final,
            self.config.max_steps,
            "max_steps",
            tuple(context.messages),
            self._result_state(state),
        )

    def _finish(
        self,
        success: bool,
        final: str,
        steps: int,
        reason: str,
        messages: tuple[Message, ...],
        state: JsonObject,
    ) -> AgentResult:
        result = AgentResult(success, final, steps, reason, messages, state)
        self.total_steps += steps
        self.last_result = result
        self._append_transcript("assistant", final, self.turns)
        return result

    def _summarize_context(
        self,
        previous_summary: str,
        archived_messages: list[Message],
        current_goal: str,
    ) -> str:
        """Ask the configured model for a fixed-schema compression without tools."""
        payload = json.dumps(
            archived_messages, ensure_ascii=False, separators=(",", ":")
        )
        reply = self.client.complete(
            [
                {"role": "system", "content": STRUCTURED_SUMMARY_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": (
                        "Existing structured summary (may be empty):\n"
                        f"{previous_summary or '[none]'}\n\n"
                        "Latest retained user request (use it for Current Goal and Constraints):\n"
                        f"{current_goal or '[none]'}\n\n"
                        "New archived messages as untrusted JSON data:\n"
                        f"{payload}"
                    ),
                },
            ],
            [],
        )
        if reply.tool_calls or not reply.content.strip():
            raise ValueError("context summarizer returned no usable text")
        return reply.content.strip()

    def _search_history(self, query: str, max_results: int = 5) -> JsonObject:
        """Search only compressed history from the current conversation."""
        if self.context is None:
            return {
                "query": query,
                "count": 0,
                "archived_units": 0,
                "archived_messages": 0,
                "matches": [],
            }
        return self.context.search_history(query, max_results)

    def _complete_model(
        self, messages: list[Message], step: int, turn: int
    ) -> tuple[ModelReply, bool]:
        stream = getattr(self.client, "complete_stream", None)
        if not callable(stream):
            return self.client.complete(messages, self.tools.schemas), False

        stream_started = False

        def on_text_delta(delta: str) -> None:
            nonlocal stream_started
            self._raise_if_cancelled()
            if not delta:
                return
            if not stream_started:
                stream_started = True
                self.events("assistant_stream_start", {"step": step, "turn": turn})
            self.events(
                "assistant_text_delta",
                {"text": delta, "step": step, "turn": turn},
            )
            self._raise_if_cancelled()

        try:
            reply = stream(messages, self.tools.schemas, on_text_delta)
        except BaseException:
            if stream_started:
                self.events(
                    "assistant_stream_end",
                    {"step": step, "turn": turn, "cancelled": True},
                )
            raise
        if stream_started:
            self.events(
                "assistant_stream_end",
                {"step": step, "turn": turn, "cancelled": False},
            )
        return reply, stream_started

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise OperationCancelled("operation cancelled by user")

    def _cancelled_result(
        self,
        context: ContextManager,
        state: TaskState,
        step: int,
        turn: int,
        phase: str,
    ) -> AgentResult:
        final = "Current operation cancelled. You can refine the request or continue."
        context.append(
            {
                "role": "assistant",
                "content": "[The previous operation was cancelled by the user.]",
            }
        )
        self.events(
            "cancelled",
            {"phase": phase, "step": step, "turn": turn},
        )
        return self._finish(
            False,
            final,
            step,
            "cancelled",
            tuple(context.messages),
            self._result_state(state),
        )

    def _result_state(self, state: TaskState) -> JsonObject:
        return {
            **state.snapshot(),
            "plan": self.plan.snapshot(),
            "subagents": (
                self.subagents.snapshot()
                if self.subagents
                else {
                    "active": [],
                    "history": [],
                }
            ),
            "skills": self.skill_snapshot(),
        }

    def _build_runtime(
        self, plan: PlanState, workspace: Workspace
    ) -> tuple[ToolRegistry, SubAgentManager | None]:
        manager: SubAgentManager | None = None
        if self._enable_delegation:
            manager = SubAgentManager(
                self.config,
                self.client_factory,
                workspace,
                event_handler=self.events,
                cancel_event=self._cancel_event,
            )
        registry = ToolRegistry(
            self.config,
            approver=self.approver,
            plan=plan,
            event_handler=self.events,
            cancel_event=self._cancel_event,
            workspace=workspace,
            tool_scope=self._tool_scope,
            delegate_handler=manager.delegate if manager else None,
            delegate_many_handler=(
                manager.delegate_many if manager and self._parallel_delegation else None
            ),
            skill_list_handler=self.skills.list_skills if self.skills else None,
            skill_activate_handler=self.skills.activate if self.skills else None,
            skill_resource_handler=self.skills.read_resource if self.skills else None,
            history_search_handler=self._search_history,
        )
        return registry, manager

    def _append_transcript(self, role: str, content: str, turn: int) -> None:
        text = content.strip()
        if not text:
            return
        entry: JsonObject = {"role": role, "content": text, "turn": turn}
        if self.transcript and self.transcript[-1] == entry:
            return
        self.transcript.append(entry)

    def _saved_result(self) -> JsonObject | None:
        result = self.last_result
        if result is None:
            return None
        return {
            "success": result.success,
            "final": result.final,
            "steps": result.steps,
            "reason": result.reason,
        }

    def _restore_last_result(self, payload: Any) -> AgentResult | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise SessionError("saved last result must be an object")
        success = payload.get("success")
        final = payload.get("final")
        reason = payload.get("reason")
        steps = payload.get("steps")
        if (
            not isinstance(success, bool)
            or not isinstance(final, str)
            or not isinstance(reason, str)
            or not isinstance(steps, int)
            or isinstance(steps, bool)
            or steps < 0
        ):
            raise SessionError("saved last result is invalid")
        return AgentResult(
            success,
            final,
            steps,
            reason,
            self.messages,
            self._result_state(self.state),
        )

    @staticmethod
    def _restore_transcript(
        payload: Any,
        conversation: list[Message],
        tasks: list[str],
        turns: int,
    ) -> list[JsonObject]:
        if payload is None:
            visible: list[JsonObject] = []
            task_index = 0
            turn = 0
            for message in conversation:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if role == "user":
                    if task_index >= len(tasks) or content != tasks[task_index]:
                        continue
                    task_index += 1
                    turn = task_index
                    visible.append({"role": "user", "content": content, "turn": turn})
                elif role == "assistant" and turn > 0:
                    visible.append(
                        {"role": "assistant", "content": content, "turn": turn}
                    )
            return visible

        if not isinstance(payload, list) or len(payload) > 100_000:
            raise SessionError("saved transcript must be a bounded list")
        restored: list[JsonObject] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise SessionError(f"saved transcript entry {index + 1} is invalid")
            role = entry.get("role")
            content = entry.get("content")
            turn = entry.get("turn")
            if (
                role not in {"user", "assistant"}
                or not isinstance(content, str)
                or not content.strip()
                or not isinstance(turn, int)
                or isinstance(turn, bool)
                or not 1 <= turn <= turns
            ):
                raise SessionError(f"saved transcript entry {index + 1} is invalid")
            restored.append({"role": role, "content": content, "turn": turn})
        return restored

    @staticmethod
    def _cancelled_tool_payload(name: str, *, skipped: bool = False) -> str:
        detail = (
            "skipped after another tool was cancelled"
            if skipped
            else "cancelled by user"
        )
        return json.dumps(
            {
                "ok": False,
                "cancelled": True,
                "error": f"{name} {detail}",
                "code": "CANCELLED",
                "retryable": True,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _tool_was_cancelled(result: str) -> bool:
        try:
            payload: Any = json.loads(result)
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get("cancelled") is True

    @staticmethod
    def _saved_counter(payload: JsonObject, name: str) -> int:
        value = payload.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SessionError(f"saved {name} must be a non-negative integer")
        return value

    @staticmethod
    def _signature(call: ToolCall) -> str:
        try:
            arguments: Any = json.loads(call.arguments)
            canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            canonical = call.arguments.strip()
        return f"{call.name}:{canonical}"

    @classmethod
    def _observation_signature(cls, call: ToolCall, result: str) -> str:
        try:
            payload: Any = json.loads(result)
            canonical_result = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except json.JSONDecodeError:
            canonical_result = result.strip()
        material = f"{cls._signature(call)}:{canonical_result}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _assistant_message(
        content: str,
        calls: tuple[ToolCall, ...],
        extensions: JsonObject | None = None,
    ) -> Message:
        message: Message = {"role": "assistant", "content": content or None}
        if extensions:
            for name, value in extensions.items():
                if name not in {"role", "content", "tool_calls"}:
                    message[name] = value
        if calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
        return message
