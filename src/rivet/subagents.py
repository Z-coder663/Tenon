from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Callable

from .config import Config
from .errors import OperationCancelled, SessionError, ToolError
from .prompt import subagent_system_prompt
from .types import EventHandler, JsonObject, ModelClient
from .workspace import Workspace


ClientFactory = Callable[[], ModelClient]
SUBAGENT_MODES = {"explore", "review"}


@dataclass(frozen=True)
class SubAgentReport:
    agent_id: str
    label: str
    mode: str
    task: str
    status: str
    summary: str
    steps: int
    reason: str
    evidence: JsonObject
    risks: tuple[str, ...]

    def payload(self) -> JsonObject:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "mode": self.mode,
            "task": self.task,
            "status": self.status,
            "summary": self.summary,
            "steps": self.steps,
            "reason": self.reason,
            "evidence": copy.deepcopy(self.evidence),
            "risks": list(self.risks),
        }


class SubAgentManager:
    """Run bounded child agents and expose only structured reports to the parent."""

    def __init__(
        self,
        config: Config,
        client_factory: ClientFactory,
        workspace: Workspace,
        *,
        event_handler: EventHandler | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.workspace = workspace
        self.events = event_handler or (lambda _event, _data: None)
        self.cancel_event = cancel_event or threading.Event()
        self.max_per_turn = min(2, config.max_subagents_per_turn)
        self.parallelism = min(2, config.subagent_parallelism, self.max_per_turn)
        self._lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._sequence = 0
        self._turn_count = 0
        self._active: dict[str, JsonObject] = {}
        self._active_agents: dict[str, Any] = {}
        self._history: list[JsonObject] = []

    def begin_turn(self) -> None:
        with self._lock:
            self._turn_count = 0

    def delegate(
        self, task: str, mode: str, label: str = ""
    ) -> JsonObject:
        assignment = self._reserve(task, mode, label)
        return self._run_assignment(assignment).payload()

    def delegate_many(self, tasks: list[JsonObject]) -> JsonObject:
        if not tasks:
            raise ToolError("at least one sub-agent task is required")
        if len(tasks) > 2:
            raise ToolError("parallel delegation accepts at most two tasks")
        normalized: list[tuple[str, str, str]] = []
        for item in tasks:
            task = str(item.get("task") or "").strip()
            mode = str(item.get("mode") or "").strip().lower()
            label = str(item.get("label") or "").strip()
            if not task:
                raise ToolError("sub-agent task must not be empty")
            if mode not in SUBAGENT_MODES:
                raise ToolError("parallel sub-agents must use explore or review mode")
            normalized.append((task, mode, label))
        with self._lock:
            remaining = self.max_per_turn - self._turn_count
            if len(normalized) > remaining:
                raise ToolError(
                    "sub-agent limit reached for this turn: "
                    f"{self.max_per_turn}"
                )
            assignments = []
            for task, mode, label in normalized:
                self._turn_count += 1
                self._sequence += 1
                assignments.append(
                    {
                        "agent_id": f"subagent-{self._sequence}",
                        "task": task,
                        "mode": mode,
                        "label": label or self._default_label(task),
                    }
                )
        worker_count = min(len(assignments), self.parallelism)
        reports: dict[str, SubAgentReport] = {}
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="rivet-subagent"
        ) as executor:
            futures = {
                executor.submit(self._run_assignment, assignment): assignment["agent_id"]
                for assignment in assignments
            }
            for future in as_completed(futures):
                report = future.result()
                reports[report.agent_id] = report
        ordered = [reports[item["agent_id"]].payload() for item in assignments]
        return {
            "parallel": True,
            "report_count": len(ordered),
            "reports": ordered,
        }

    def snapshot(self) -> JsonObject:
        with self._lock:
            active = [copy.deepcopy(value) for value in self._active.values()]
            history = copy.deepcopy(self._history[-20:])
        return {"active": active, "history": history}

    def export_state(self) -> JsonObject:
        with self._lock:
            return {
                "sequence": self._sequence,
                "history": copy.deepcopy(self._history[-100:]),
            }

    def restore_state(self, payload: Any) -> None:
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise SessionError("saved sub-agent state must be an object")
        sequence = payload.get("sequence", 0)
        history = payload.get("history", [])
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 0
        ):
            raise SessionError("saved sub-agent sequence must be non-negative")
        if (
            not isinstance(history, list)
            or len(history) > 100
            or any(not isinstance(item, dict) for item in history)
        ):
            raise SessionError("saved sub-agent history must be a list of objects")
        compatible_history = [
            copy.deepcopy(item)
            for item in history
            if str(item.get("mode") or "").lower() in SUBAGENT_MODES
        ]
        with self._lock:
            self._sequence = sequence
            self._history = compatible_history
            self._active.clear()
            self._active_agents.clear()

    def cancel_active(self) -> None:
        with self._lock:
            agents = list(self._active_agents.values())
        for agent in agents:
            agent.request_cancel()

    def _reserve(
        self,
        task: str,
        mode: str,
        label: str,
    ) -> JsonObject:
        task = task.strip()
        mode = mode.strip().lower()
        label = label.strip()
        if not task:
            raise ToolError("sub-agent task must not be empty")
        if mode not in SUBAGENT_MODES:
            raise ToolError(f"unsupported sub-agent mode: {mode}")
        with self._lock:
            if self._turn_count >= self.max_per_turn:
                raise ToolError(
                    "sub-agent limit reached for this turn: "
                    f"{self.max_per_turn}"
                )
            self._turn_count += 1
            self._sequence += 1
            agent_id = f"subagent-{self._sequence}"
        return {
            "agent_id": agent_id,
            "task": task,
            "mode": mode,
            "label": label or self._default_label(task),
        }

    def _run_assignment(self, assignment: JsonObject) -> SubAgentReport:
        from .agent import Agent

        agent_id = str(assignment["agent_id"])
        task = str(assignment["task"])
        mode = str(assignment["mode"])
        label = str(assignment["label"])
        active = {
            "agent_id": agent_id,
            "label": label,
            "mode": mode,
            "task": task,
            "status": "running",
            "step": 0,
            "tool": None,
        }
        with self._lock:
            self._active[agent_id] = active
        self._emit("subagent_started", copy.deepcopy(active))

        child_config = replace(self.config, max_steps=self.config.subagent_max_steps)

        def child_event(event: str, data: JsonObject) -> None:
            progress: JsonObject | None = None
            with self._lock:
                current = self._active.get(agent_id)
                if current is None:
                    return
                if event == "model_start":
                    current["step"] = data.get("step", 0)
                    current["tool"] = None
                elif event == "tool_start":
                    current["tool"] = data.get("name")
                elif event == "tool_end":
                    current["tool"] = None
                else:
                    return
                progress = copy.deepcopy(current)
            self._emit("subagent_progress", progress)

        try:
            if self.cancel_event.is_set():
                raise OperationCancelled("operation cancelled by user")
            child = Agent(
                child_config,
                self.client_factory(),
                event_handler=child_event,
                workspace=self.workspace,
                tool_scope="read_only",
                enable_delegation=False,
                cancel_event=self.cancel_event,
                system_prompt_text=subagent_system_prompt(self.config.workspace, mode),
            )
            with self._lock:
                self._active_agents[agent_id] = child
            result = child.run(task)
            report = self._report_from_result(assignment, result)
        except OperationCancelled:
            report = self._failure_report(assignment, "cancelled", "cancelled")
        except Exception as exc:
            report = self._failure_report(
                assignment,
                f"{type(exc).__name__}: {exc}",
                "subagent_error",
            )
        finally:
            with self._lock:
                self._active_agents.pop(agent_id, None)

        payload = report.payload()
        with self._lock:
            self._active.pop(agent_id, None)
            self._history.append(copy.deepcopy(payload))
            self._history = self._history[-100:]
        self._emit("subagent_finished", copy.deepcopy(payload))
        return report

    def _emit(self, event: str, data: JsonObject) -> None:
        with self._event_lock:
            self.events(event, data)

    @staticmethod
    def _report_from_result(assignment: JsonObject, result: Any) -> SubAgentReport:
        state = result.state if isinstance(result.state, dict) else {}
        inspected = state.get("inspected_files", [])
        inspected_files = [item for item in inspected if isinstance(item, str)]
        risks: list[str] = []
        if not result.success:
            risks.append(f"sub-agent did not complete: {result.reason}")
        status = (
            "cancelled"
            if result.reason == "cancelled"
            else "completed" if result.success else "failed"
        )
        return SubAgentReport(
            agent_id=str(assignment["agent_id"]),
            label=str(assignment["label"]),
            mode=str(assignment["mode"]),
            task=str(assignment["task"]),
            status=status,
            summary=result.final,
            steps=result.steps,
            reason=result.reason,
            evidence={"inspected_files": inspected_files},
            risks=tuple(risks),
        )

    @staticmethod
    def _failure_report(
        assignment: JsonObject, summary: str, reason: str
    ) -> SubAgentReport:
        status = "cancelled" if reason == "cancelled" else "failed"
        return SubAgentReport(
            agent_id=str(assignment["agent_id"]),
            label=str(assignment["label"]),
            mode=str(assignment["mode"]),
            task=str(assignment["task"]),
            status=status,
            summary=summary,
            steps=0,
            reason=reason,
            evidence={"inspected_files": []},
            risks=(summary,),
        )

    @staticmethod
    def _default_label(task: str) -> str:
        compact = " ".join(task.split())
        return compact if len(compact) <= 36 else compact[:33] + "..."
