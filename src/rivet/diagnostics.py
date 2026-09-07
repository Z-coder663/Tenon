from __future__ import annotations

import json
import sys
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .errors import RivetError
from .types import JsonObject, Message, ModelClient, ModelReply


CHECK_TOOL: JsonObject = {
    "type": "function",
    "function": {
        "name": "compatibility_echo",
        "description": (
            "Compatibility test tool. Call it exactly once with the value requested "
            "by the user. It has no external side effects."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
}


def run_model_check(
    config: Config,
    client: ModelClient,
    *,
    output: TextIO | None = None,
) -> int:
    """Run three small requests that cover streaming and a tool round trip."""
    stream = output or sys.stdout
    print("Rivet model compatibility check", file=stream)
    print(f"  protocol    {config.protocol}", file=stream)
    print(f"  model       {config.model}", file=stream)
    print(f"  endpoint    {_safe_endpoint(config.endpoint)}", file=stream)
    print(f"  auth        {config.auth_style}", file=stream)
    print(f"  api key     {'configured' if config.api_key else 'not used'}", file=stream)
    print(
        f"  env file    {config.env_file.name if config.env_file else 'not loaded'}",
        file=stream,
    )
    option_names = ", ".join(sorted(config.extra_body)) or "none"
    print(f"  body options {option_names}", file=stream)
    print("  requests    3 small API calls", file=stream)

    try:
        streaming = _check_streaming(client)
        print(
            "  [ok] streaming response"
            + ("" if streaming else " (provider used Rivet's JSON fallback)"),
            file=stream,
        )
        first_reply, first_messages = _check_tool_call(client)
        extensions = ", ".join(sorted(first_reply.extensions)) or "none"
        print(f"  [ok] function tool call | replay fields: {extensions}", file=stream)
        _check_tool_round_trip(client, first_reply, first_messages)
        print("  [ok] tool result round trip", file=stream)
    except (RivetError, ValueError) as exc:
        print(f"  [failed] {exc}", file=stream)
        return 1

    print("Compatible: this model can run Rivet's current agent loop.", file=stream)
    return 0


def _check_streaming(client: ModelClient) -> bool:
    complete_stream = getattr(client, "complete_stream", None)
    if not callable(complete_stream):
        raise ValueError("selected protocol client does not support streaming")
    deltas: list[str] = []
    reply = complete_stream(
        [
            {"role": "system", "content": "Follow the compatibility check exactly."},
            {
                "role": "user",
                "content": "Reply with a short plain-text acknowledgement. Do not call tools.",
            },
        ],
        [],
        deltas.append,
    )
    if not reply.content.strip():
        raise ValueError("model returned no text for the streaming check")
    return bool(deltas)


def _check_tool_call(client: ModelClient) -> tuple[ModelReply, list[Message]]:
    messages: list[Message] = [
        {"role": "system", "content": "Follow the compatibility check exactly."},
        {
            "role": "user",
            "content": (
                "Call compatibility_echo exactly once with value RIVET_TOOL_OK. "
                "Do not answer the request yourself."
            ),
        },
    ]
    reply = client.complete(messages, [CHECK_TOOL])
    if len(reply.tool_calls) != 1:
        raise ValueError("model did not produce exactly one function tool call")
    call = reply.tool_calls[0]
    if call.name != "compatibility_echo":
        raise ValueError(f"model called an unexpected tool: {call.name}")
    try:
        arguments: Any = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        raise ValueError("model produced invalid JSON tool arguments") from exc
    if not isinstance(arguments, dict) or arguments.get("value") != "RIVET_TOOL_OK":
        raise ValueError("model did not preserve the requested tool argument")
    return reply, messages


def _check_tool_round_trip(
    client: ModelClient, first_reply: ModelReply, messages: list[Message]
) -> None:
    if not first_reply.raw_message:
        raise ValueError("protocol client did not retain the assistant tool message")
    call = first_reply.tool_calls[0]
    follow_up = [
        *messages,
        first_reply.raw_message,
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": json.dumps({"ok": True, "value": "RIVET_TOOL_OK"}),
        },
    ]
    reply = client.complete(follow_up, [CHECK_TOOL])
    if reply.tool_calls:
        raise ValueError("model repeated the compatibility tool after receiving its result")
    if not reply.content.strip():
        raise ValueError("model returned no final text after the tool result")


def _safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
