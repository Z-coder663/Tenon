from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .types import JsonObject


SUCCEEDED = "succeeded"
REJECTED = "rejected"
DENIED = "denied"
FAILED = "failed"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"
SKIPPED = "skipped"
UNKNOWN = "unknown"

EXECUTED = "executed"
NOT_EXECUTED = "not_executed"
EXECUTION_UNKNOWN = "unknown"

TOOL_STATUSES = frozenset(
    {SUCCEEDED, REJECTED, DENIED, FAILED, TIMED_OUT, CANCELLED, SKIPPED, UNKNOWN}
)
EXECUTION_STATES = frozenset({EXECUTED, NOT_EXECUTED, EXECUTION_UNKNOWN})


@dataclass(frozen=True)
class ToolOutcome:
    """Canonical tool result semantics with a legacy-compatible JSON envelope."""

    status: str
    code: str
    execution_state: str
    retryable: bool
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"invalid tool outcome status: {self.status}")
        if self.execution_state not in EXECUTION_STATES:
            raise ValueError(f"invalid tool execution state: {self.execution_state}")
        if not self.code:
            raise ValueError("tool outcome code must not be empty")

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCEEDED

    @property
    def result_unknown(self) -> bool:
        return self.status == UNKNOWN or self.execution_state == EXECUTION_UNKNOWN

    def to_payload(
        self,
        data: JsonObject | None = None,
        *,
        legacy_ok: bool | None = None,
    ) -> JsonObject:
        payload = dict(data or {})
        payload.update(
            {
                "ok": self.succeeded if legacy_ok is None else legacy_ok,
                "status": self.status,
                "code": self.code,
                "execution_state": self.execution_state,
                "retryable": self.retryable,
            }
        )
        if self.error:
            payload["error"] = self.error
        return payload

    def to_json(
        self,
        data: JsonObject | None = None,
        *,
        legacy_ok: bool | None = None,
    ) -> str:
        return json.dumps(
            self.to_payload(data, legacy_ok=legacy_ok), ensure_ascii=False
        )

    @classmethod
    def from_json(cls, raw_result: str) -> tuple["ToolOutcome", JsonObject] | None:
        try:
            payload: Any = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return cls.from_payload(payload), payload

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "ToolOutcome":
        raw_status = payload.get("status")
        status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in TOOL_STATUSES
            else cls._legacy_status(payload)
        )
        raw_execution = payload.get("execution_state")
        execution_state = (
            raw_execution
            if isinstance(raw_execution, str) and raw_execution in EXECUTION_STATES
            else cls._legacy_execution_state(payload, status)
        )
        raw_code = payload.get("code")
        code = (
            raw_code
            if isinstance(raw_code, str) and raw_code
            else cls._legacy_code(payload, status)
        )
        raw_retryable = payload.get("retryable")
        retryable = (
            raw_retryable
            if isinstance(raw_retryable, bool)
            else status in {REJECTED, FAILED, TIMED_OUT}
        )
        raw_error = payload.get("error")
        error = str(raw_error) if raw_error is not None and raw_error != "" else None
        return cls(status, code, execution_state, retryable, error)

    @staticmethod
    def _legacy_status(payload: JsonObject) -> str:
        code = payload.get("code")
        if payload.get("result_unknown") is True or code == "RESULT_UNKNOWN":
            return UNKNOWN
        if payload.get("skipped") is True or code == "SKIPPED":
            return SKIPPED
        if payload.get("cancelled") is True or code == "CANCELLED":
            return CANCELLED
        if payload.get("timed_out") is True:
            return TIMED_OUT
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return FAILED
        if code == "APPROVAL_REQUIRED":
            return DENIED
        if code in {"INVALID_ARGUMENT", "UNKNOWN_TOOL"}:
            return REJECTED
        return SUCCEEDED if payload.get("ok") is True else FAILED

    @staticmethod
    def _legacy_execution_state(payload: JsonObject, status: str) -> str:
        if status == UNKNOWN or payload.get("result_unknown") is True:
            return EXECUTION_UNKNOWN
        if status in {REJECTED, DENIED, SKIPPED}:
            return NOT_EXECUTED
        return EXECUTED

    @staticmethod
    def _legacy_code(payload: JsonObject, status: str) -> str:
        if status == SUCCEEDED:
            return "SUCCESS"
        if status == TIMED_OUT:
            return "COMMAND_TIMEOUT"
        if status == CANCELLED:
            return "CANCELLED"
        if status == SKIPPED:
            return "SKIPPED"
        if status == UNKNOWN:
            return "RESULT_UNKNOWN"
        exit_code = payload.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return "COMMAND_FAILED"
        return "TOOL_ERROR"


def successful_outcome(data: JsonObject | None = None) -> str:
    return ToolOutcome(SUCCEEDED, "SUCCESS", EXECUTED, False).to_json(data)


def rejected_outcome(
    message: str,
    *,
    code: str = "INVALID_ARGUMENT",
    field: str | None = None,
) -> str:
    data: JsonObject = {}
    if field:
        data["field"] = field
    return ToolOutcome(REJECTED, code, NOT_EXECUTED, True, message).to_json(data)


def denied_outcome(message: str, *, code: str = "APPROVAL_REQUIRED") -> str:
    return ToolOutcome(DENIED, code, NOT_EXECUTED, False, message).to_json()


def failed_outcome(
    message: str,
    *,
    code: str = "TOOL_ERROR",
    retryable: bool = True,
    result_unknown: bool = False,
) -> str:
    status = UNKNOWN if result_unknown else FAILED
    execution_state = EXECUTION_UNKNOWN if result_unknown else EXECUTED
    data = {"result_unknown": True} if result_unknown else None
    return ToolOutcome(status, code, execution_state, retryable, message).to_json(data)


def command_outcome(data: JsonObject) -> str:
    if data.get("cancelled") is True:
        outcome = ToolOutcome(
            CANCELLED,
            "COMMAND_CANCELLED",
            EXECUTED,
            False,
            "command was cancelled",
        )
    elif data.get("timed_out") is True:
        outcome = ToolOutcome(
            TIMED_OUT,
            "COMMAND_TIMEOUT",
            EXECUTED,
            True,
            "command timed out",
        )
    else:
        exit_code = data.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            outcome = ToolOutcome(
                FAILED,
                "COMMAND_FAILED",
                EXECUTED,
                True,
                f"command exited with code {exit_code}",
            )
        elif exit_code == 0:
            outcome = ToolOutcome(SUCCEEDED, "SUCCESS", EXECUTED, False)
        else:
            outcome = ToolOutcome(
                UNKNOWN,
                "INTERNAL_ERROR",
                EXECUTION_UNKNOWN,
                False,
                "command returned without a valid exit status",
            )
            data = {**data, "result_unknown": True}
    # Historically any command that returned a process observation used ok=true.
    return outcome.to_json(data, legacy_ok=True)
