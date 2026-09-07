from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .errors import SessionError, ToolError
from .types import JsonObject


PLAN_STATUSES = {"pending", "in_progress", "completed", "blocked"}
MAX_PLAN_STEPS = 20
MAX_STEP_CHARS = 240
MAX_EXPLANATION_CHARS = 1000


@dataclass
class PlanState:
    """Validated, provider-neutral progress state for the current task."""

    steps: list[JsonObject] = field(default_factory=list)
    explanation: str = ""
    revision: int = 0

    @classmethod
    def restore(cls, payload: Any) -> "PlanState":
        if payload is None:
            return cls()
        if not isinstance(payload, dict):
            raise SessionError("saved plan state must be an object")
        revision = payload.get("revision", 0)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise SessionError("saved plan revision must be a non-negative integer")
        explanation = payload.get("explanation", "")
        if not isinstance(explanation, str) or len(explanation) > MAX_EXPLANATION_CHARS:
            raise SessionError("saved plan explanation is invalid")
        try:
            steps = cls._normalize_steps(payload.get("steps", []))
        except ToolError as exc:
            raise SessionError(f"saved plan is invalid: {exc}") from exc
        return cls(steps=steps, explanation=explanation.strip(), revision=revision)

    def update(self, steps: list[JsonObject], explanation: str = "") -> JsonObject:
        normalized = self._normalize_steps(steps)
        if not isinstance(explanation, str):
            raise ToolError("plan explanation must be text")
        explanation = explanation.strip()
        if len(explanation) > MAX_EXPLANATION_CHARS:
            raise ToolError(
                f"plan explanation must not exceed {MAX_EXPLANATION_CHARS} characters"
            )

        changed = normalized != self.steps or explanation != self.explanation
        if changed:
            self.steps = normalized
            self.explanation = explanation
            self.revision += 1
        return {"changed": changed, **self.snapshot()}

    def clear(self) -> None:
        if self.steps or self.explanation:
            self.steps = []
            self.explanation = ""
            self.revision += 1

    @property
    def active(self) -> bool:
        return bool(self.steps)

    @property
    def terminal(self) -> bool:
        return self.active and all(
            step["status"] in {"completed", "blocked"} for step in self.steps
        )

    @property
    def blocked(self) -> bool:
        return any(step["status"] == "blocked" for step in self.steps)

    def snapshot(self) -> JsonObject:
        counts = {
            status: sum(step["status"] == status for step in self.steps)
            for status in sorted(PLAN_STATUSES)
        }
        return {
            "active": self.active,
            "terminal": self.terminal,
            "blocked": self.blocked,
            "revision": self.revision,
            "explanation": self.explanation,
            "steps": copy.deepcopy(self.steps),
            "counts": counts,
        }

    def export_state(self) -> JsonObject:
        return {
            "steps": copy.deepcopy(self.steps),
            "explanation": self.explanation,
            "revision": self.revision,
        }

    @staticmethod
    def _normalize_steps(value: Any) -> list[JsonObject]:
        if not isinstance(value, list):
            raise ToolError("plan steps must be a list")
        if len(value) > MAX_PLAN_STEPS:
            raise ToolError(f"a plan may contain at most {MAX_PLAN_STEPS} steps")

        normalized: list[JsonObject] = []
        seen: set[str] = set()
        in_progress = 0
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                raise ToolError(f"plan step {index} must be an object")
            if set(item) != {"step", "status"}:
                raise ToolError(f"plan step {index} must contain only step and status")
            text = item.get("step")
            status = item.get("status")
            if not isinstance(text, str) or not text.strip():
                raise ToolError(f"plan step {index} must contain text")
            text = " ".join(text.split())
            if len(text) > MAX_STEP_CHARS:
                raise ToolError(
                    f"plan step {index} must not exceed {MAX_STEP_CHARS} characters"
                )
            if text.casefold() in seen:
                raise ToolError(f"plan step {index} duplicates an earlier step")
            if status not in PLAN_STATUSES:
                raise ToolError(f"plan step {index} has an invalid status")
            seen.add(text.casefold())
            in_progress += status == "in_progress"
            normalized.append({"step": text, "status": status})

        if in_progress > 1:
            raise ToolError("a plan may have at most one in-progress step")
        return normalized
