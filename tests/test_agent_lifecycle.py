from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rivet.agent import Agent
from rivet.config import Config
from rivet.errors import ModelError, OperationCancelled
from rivet.session import SessionStore
from rivet.types import ModelReply, ToolCall


class SequenceClient:
    def __init__(self, *items: ModelReply | Exception) -> None:
        self.items = list(items)

    def complete(self, _messages, _tools):
        if not self.items:
            raise AssertionError("the fake model received an unexpected request")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(call_id: str, *, line: int = 1) -> ToolCall:
    return ToolCall(
        call_id,
        "read_file",
        json.dumps(
            {"path": "sample.py", "start_line": line, "end_line": line}
        ),
    )


class AgentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "sample.py").write_text(
            "first = 1\nsecond = 2\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def agent(self, client, *, events=None, max_steps: int = 6) -> Agent:
        config = Config(
            self.root,
            None,
            "http://unused.invalid/v1",
            "fake-model",
            max_steps=max_steps,
        )
        return Agent(config, client, event_handler=events)

    @staticmethod
    def tool_messages(agent: Agent):
        return [message for message in agent.messages if message.get("role") == "tool"]

    def test_normal_multi_tool_batch_remains_complete(self) -> None:
        client = SequenceClient(
            ModelReply("", (tool_call("a", line=1), tool_call("b", line=2))),
            ModelReply("done"),
        )
        agent = self.agent(client)

        result = agent.run("inspect the sample")

        self.assertTrue(result.success)
        self.assertEqual(["a", "b"], [m["tool_call_id"] for m in self.tool_messages(agent)])
        agent.context.validate_active_tool_exchanges()

    def test_repeated_call_stop_settles_unexecuted_calls(self) -> None:
        calls = tuple(tool_call(str(index)) for index in range(4))
        agent = self.agent(SequenceClient(ModelReply("", calls)))
        original_execute = agent.tools.execute
        executed: list[str] = []

        def counting_execute(name: str, arguments: str) -> str:
            executed.append(name)
            return original_execute(name, arguments)

        agent.tools.execute = counting_execute

        result = agent.run("inspect repeatedly")

        self.assertEqual("repeated_tool_call", result.reason)
        self.assertEqual(3, len(executed))
        messages = self.tool_messages(agent)
        self.assertEqual(["0", "1", "2", "3"], [m["tool_call_id"] for m in messages])
        skipped = json.loads(messages[-1]["content"])
        self.assertEqual("SKIPPED", skipped["code"])
        self.assertTrue(skipped["skipped"])
        self.assertEqual("not_executed", skipped["execution_state"])
        agent.context.validate_active_tool_exchanges()

    def test_model_error_returns_result_and_allows_next_turn(self) -> None:
        agent = self.agent(
            SequenceClient(ModelError("synthetic provider failure"), ModelReply("recovered"))
        )

        failed = agent.run("first turn")
        recovered = agent.run("second turn")

        self.assertFalse(failed.success)
        self.assertEqual("model_error", failed.reason)
        self.assertEqual(1, failed.steps)
        self.assertTrue(recovered.success)
        self.assertEqual(2, agent.total_steps)
        self.assertIs(agent.last_result, recovered)

    def test_model_error_after_tool_step_can_be_saved_and_restored(self) -> None:
        agent = self.agent(
            SequenceClient(
                ModelReply("", (tool_call("read"),)),
                ModelError("synthetic second-step failure"),
            )
        )

        result = agent.run("inspect then fail")

        self.assertEqual("model_error", result.reason)
        self.assertEqual(2, result.steps)
        store = SessionStore(self.root, model=agent.config.model)
        path = store.save(agent, result)
        restored = self.agent(SequenceClient(ModelReply("unused")))
        restored.restore_session_state(store.load(path.stem).agent_state)
        self.assertEqual("model_error", restored.last_result.reason)
        restored.context.validate_active_tool_exchanges()

    def test_context_compaction_cancellation_returns_cancelled_result(self) -> None:
        agent = self.agent(SequenceClient(ModelReply("ready")))
        self.assertTrue(agent.run("initial turn").success)
        agent.context.max_chars = 1_000
        for _ in range(12):
            agent.context.append({"role": "assistant", "content": "x" * 1_000})

        def cancel_summary(*_args):
            raise OperationCancelled("synthetic compaction cancellation")

        agent.context._summarizer = cancel_summary
        agent.client = SequenceClient(ModelReply("must not be used"))

        result = agent.run("continue")

        self.assertEqual("cancelled", result.reason)
        self.assertEqual(1, result.steps)
        agent.context.validate_active_tool_exchanges()

    def test_tool_cancellation_marks_current_unknown_and_rest_not_executed(self) -> None:
        agent = self.agent(
            SequenceClient(ModelReply("", (tool_call("a"), tool_call("b", line=2))))
        )
        attempts = 0

        def cancel_execute(_name: str, _arguments: str) -> str:
            nonlocal attempts
            attempts += 1
            raise OperationCancelled("synthetic tool cancellation")

        agent.tools.execute = cancel_execute

        result = agent.run("inspect")

        self.assertEqual("cancelled", result.reason)
        self.assertEqual(1, attempts)
        messages = self.tool_messages(agent)
        current = json.loads(messages[0]["content"])
        pending = json.loads(messages[1]["content"])
        self.assertEqual("CANCELLED", current["code"])
        self.assertTrue(current["result_unknown"])
        self.assertEqual("unknown", current["execution_state"])
        self.assertEqual("CANCELLED", pending["code"])
        self.assertTrue(pending["skipped"])
        self.assertEqual("not_executed", pending["execution_state"])
        agent.context.validate_active_tool_exchanges()

    def test_event_callback_failure_does_not_break_tool_protocol(self) -> None:
        def failing_events(event, _data):
            if event == "tool_end":
                raise RuntimeError("synthetic renderer failure")

        agent = self.agent(
            SequenceClient(
                ModelReply("", (tool_call("a"), tool_call("b", line=2))),
                ModelReply("done"),
            ),
            events=failing_events,
        )

        result = agent.run("inspect")

        self.assertTrue(result.success)
        self.assertEqual(2, len(self.tool_messages(agent)))
        self.assertEqual(2, len(agent.status()["event_failures"]))
        agent.context.validate_active_tool_exchanges()

    def test_unknown_tool_result_stops_without_replaying_or_running_rest(self) -> None:
        agent = self.agent(
            SequenceClient(ModelReply("", (tool_call("a"), tool_call("b", line=2))))
        )
        attempts = 0

        def fail_execute(_name: str, _arguments: str) -> str:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("synthetic execution boundary failure")

        agent.tools.execute = fail_execute

        result = agent.run("inspect")

        self.assertEqual("runtime_error", result.reason)
        self.assertEqual(1, attempts)
        messages = self.tool_messages(agent)
        self.assertEqual(2, len(messages))
        unknown = json.loads(messages[0]["content"])
        skipped = json.loads(messages[1]["content"])
        self.assertEqual("RESULT_UNKNOWN", unknown["code"])
        self.assertTrue(unknown["result_unknown"])
        self.assertEqual("unknown", unknown["execution_state"])
        self.assertEqual("SKIPPED", skipped["code"])
        self.assertEqual("not_executed", skipped["execution_state"])
        agent.context.validate_active_tool_exchanges()

    def test_duplicate_model_tool_call_ids_fail_before_execution(self) -> None:
        duplicate = (tool_call("same"), tool_call("same", line=2))
        agent = self.agent(SequenceClient(ModelReply("", duplicate)))

        result = agent.run("inspect")

        self.assertEqual("model_error", result.reason)
        self.assertEqual([], self.tool_messages(agent))


if __name__ == "__main__":
    unittest.main()
