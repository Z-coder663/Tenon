from __future__ import annotations

import unittest

from rivet.context import ContextManager
from rivet.errors import SessionError


def assistant_calls(*call_ids: str):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
            }
            for call_id in call_ids
        ],
    }


def tool_result(call_id: str):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "read_file",
        "content": '{"ok":true}',
    }


class ContextProtocolTests(unittest.TestCase):
    def test_valid_tool_exchange_restores(self) -> None:
        manager = ContextManager.restore(
            "system",
            [
                {"role": "user", "content": "inspect"},
                assistant_calls("a", "b"),
                tool_result("a"),
                tool_result("b"),
                {"role": "assistant", "content": "done"},
            ],
            10_000,
        )
        manager.validate_active_tool_exchanges()

    def test_restore_rejects_missing_tool_result(self) -> None:
        with self.assertRaisesRegex(SessionError, "missing tool result"):
            ContextManager.restore(
                "system",
                [
                    {"role": "user", "content": "inspect"},
                    assistant_calls("a", "b"),
                    tool_result("a"),
                ],
                10_000,
            )

    def test_restore_rejects_orphan_tool_result(self) -> None:
        with self.assertRaisesRegex(SessionError, "orphan tool result"):
            ContextManager.restore(
                "system",
                [
                    {"role": "user", "content": "inspect"},
                    tool_result("orphan"),
                ],
                10_000,
            )

    def test_restore_rejects_duplicate_call_ids(self) -> None:
        with self.assertRaisesRegex(SessionError, "duplicate tool call IDs"):
            ContextManager.restore(
                "system",
                [
                    {"role": "user", "content": "inspect"},
                    assistant_calls("same", "same"),
                    tool_result("same"),
                ],
                10_000,
            )

    def test_export_rejects_runtime_corruption(self) -> None:
        manager = ContextManager("system", "inspect", 10_000)
        manager.append(assistant_calls("missing"))
        with self.assertRaisesRegex(SessionError, "missing tool result"):
            manager.export_conversation()


if __name__ == "__main__":
    unittest.main()
