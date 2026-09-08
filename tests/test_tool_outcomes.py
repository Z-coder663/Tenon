from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rivet.agent import TaskState
from rivet.config import Config
from rivet.outcomes import (
    CANCELLED,
    DENIED,
    EXECUTED,
    EXECUTION_UNKNOWN,
    FAILED,
    NOT_EXECUTED,
    REJECTED,
    SUCCEEDED,
    TIMED_OUT,
    UNKNOWN,
    ToolOutcome,
)
from rivet.tools import ToolRegistry


class ToolOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "sample.py").write_text("value = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def registry(self, *, approval_mode: str = "safe") -> ToolRegistry:
        return ToolRegistry(
            Config(
                self.root,
                None,
                "http://unused.invalid/v1",
                "fake-model",
                approval_mode=approval_mode,
            )
        )

    @staticmethod
    def parsed(raw: str) -> tuple[ToolOutcome, dict]:
        parsed = ToolOutcome.from_json(raw)
        if parsed is None:
            raise AssertionError(f"invalid tool outcome: {raw}")
        return parsed

    @staticmethod
    def python_command(source: str) -> str:
        arguments = [sys.executable, "-c", source]
        if os.name == "nt":
            return subprocess.list2cmdline(arguments)
        return " ".join(shlex.quote(item) for item in arguments)

    def test_success_has_canonical_semantics(self) -> None:
        raw = self.registry().execute("read_file", '{"path":"sample.py"}')

        outcome, payload = self.parsed(raw)

        self.assertEqual(SUCCEEDED, outcome.status)
        self.assertEqual("SUCCESS", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)
        self.assertFalse(outcome.retryable)
        self.assertTrue(payload["ok"])
        self.assertEqual("sample.py", payload["path"])

    def test_invalid_arguments_are_rejected_before_execution(self) -> None:
        outcome, payload = self.parsed(
            self.registry().execute("read_file", '{"unexpected":true}')
        )

        self.assertEqual(REJECTED, outcome.status)
        self.assertEqual("INVALID_ARGUMENT", outcome.code)
        self.assertEqual(NOT_EXECUTED, outcome.execution_state)
        self.assertTrue(outcome.retryable)
        self.assertFalse(payload["ok"])

    def test_permission_denial_is_not_retryable(self) -> None:
        raw = self.registry(approval_mode="never").execute(
            "write_file", '{"path":"new.py","content":"value = 2\\n"}'
        )

        outcome, payload = self.parsed(raw)

        self.assertEqual(DENIED, outcome.status)
        self.assertEqual("APPROVAL_REQUIRED", outcome.code)
        self.assertEqual(NOT_EXECUTED, outcome.execution_state)
        self.assertFalse(outcome.retryable)
        self.assertFalse(payload["ok"])
        self.assertFalse((self.root / "new.py").exists())

    def test_expected_tool_error_is_a_known_execution_failure(self) -> None:
        outcome, _payload = self.parsed(
            self.registry().execute("read_file", '{"path":"missing.py"}')
        )

        self.assertEqual(FAILED, outcome.status)
        self.assertEqual("TOOL_ERROR", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)
        self.assertTrue(outcome.retryable)

    def test_unexpected_handler_error_has_unknown_result(self) -> None:
        registry = self.registry()
        spec = registry._tools["read_file"]

        def fail_handler(**_arguments):
            raise RuntimeError("synthetic handler failure")

        registry._tools["read_file"] = replace(spec, handler=fail_handler)
        outcome, payload = self.parsed(
            registry.execute("read_file", '{"path":"sample.py"}')
        )

        self.assertEqual(UNKNOWN, outcome.status)
        self.assertEqual("INTERNAL_ERROR", outcome.code)
        self.assertEqual(EXECUTION_UNKNOWN, outcome.execution_state)
        self.assertFalse(outcome.retryable)
        self.assertTrue(payload["result_unknown"])

    def test_command_nonzero_exit_is_an_explicit_failure(self) -> None:
        registry = self.registry()
        spec = registry._tools["run_command"]
        registry._tools["run_command"] = replace(
            spec,
            handler=lambda **_arguments: {
                "command": "synthetic failure",
                "exit_code": 7,
                "timed_out": False,
                "cancelled": False,
                "verification": True,
                "purpose": "verify",
                "stdout": "",
                "stderr": "failed",
                "file_changes": [],
                "tracking_complete": True,
            },
        )

        outcome, payload = self.parsed(
            registry.execute(
                "run_command",
                '{"command":"synthetic failure","timeout":10,"purpose":"verify"}',
            )
        )

        self.assertEqual(FAILED, outcome.status)
        self.assertEqual("COMMAND_FAILED", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)
        self.assertTrue(outcome.retryable)
        self.assertTrue(payload["ok"])
        state = TaskState()
        state.record_tool_result("run_command", json.dumps(payload))
        self.assertEqual(1, len(state.commands))
        self.assertEqual(0, state.last_successful_command_operation)

    def test_command_timeout_is_distinct_from_process_failure(self) -> None:
        registry = self.registry()
        spec = registry._tools["run_command"]
        registry._tools["run_command"] = replace(
            spec,
            handler=lambda **_arguments: {
                "command": "synthetic timeout",
                "exit_code": None,
                "timed_out": True,
                "cancelled": False,
                "verification": False,
                "purpose": "inspect",
                "stdout": "",
                "stderr": "",
                "file_changes": [],
                "tracking_complete": True,
            },
        )

        outcome, _payload = self.parsed(
            registry.execute(
                "run_command",
                '{"command":"synthetic timeout","timeout":10,"purpose":"inspect"}',
            )
        )

        self.assertEqual(TIMED_OUT, outcome.status)
        self.assertEqual("COMMAND_TIMEOUT", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)
        self.assertTrue(outcome.retryable)

    def test_observed_command_cancellation_is_not_result_unknown(self) -> None:
        registry = self.registry()
        spec = registry._tools["run_command"]
        registry._tools["run_command"] = replace(
            spec,
            handler=lambda **_arguments: {
                "command": "synthetic cancellation",
                "exit_code": None,
                "timed_out": False,
                "cancelled": True,
                "verification": False,
                "purpose": "inspect",
                "stdout": "partial output",
                "stderr": "",
                "file_changes": [],
                "tracking_complete": True,
            },
        )

        outcome, payload = self.parsed(
            registry.execute(
                "run_command",
                '{"command":"synthetic cancellation","timeout":10,"purpose":"inspect"}',
            )
        )

        self.assertEqual(CANCELLED, outcome.status)
        self.assertEqual("COMMAND_CANCELLED", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)
        self.assertFalse(outcome.result_unknown)
        self.assertTrue(payload["cancelled"])

    def test_real_nonzero_process_uses_command_failure_outcome(self) -> None:
        command = self.python_command("raise SystemExit(9)")
        raw = self.registry().execute(
            "run_command",
            json.dumps({"command": command, "timeout": 10, "purpose": "inspect"}),
        )

        outcome, payload = self.parsed(raw)

        self.assertEqual(FAILED, outcome.status)
        self.assertEqual("COMMAND_FAILED", outcome.code)
        self.assertEqual(9, payload["exit_code"])
        self.assertFalse(payload["timed_out"])

    def test_legacy_results_remain_parseable(self) -> None:
        outcome = ToolOutcome.from_payload(
            {
                "ok": True,
                "exit_code": 2,
                "timed_out": False,
                "cancelled": False,
            }
        )

        self.assertEqual(FAILED, outcome.status)
        self.assertEqual("COMMAND_FAILED", outcome.code)
        self.assertEqual(EXECUTED, outcome.execution_state)


if __name__ == "__main__":
    unittest.main()
