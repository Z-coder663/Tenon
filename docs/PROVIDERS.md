# Model providers

Rivet keeps provider configuration outside the Agent loop. Version 1.5 ships one protocol
client, `openai_chat`, for services that implement Chat Completions, SSE, and function tools.
Native non-compatible protocols can be added through `ModelClient` without changing tools,
planning, sessions, or the TUI.

## Configuration sources

At startup Rivet discovers configuration in this order: `--env-file`,
`RIVET_ENV_FILE`, `~/.rivet/.env`, `.env` in an editable Rivet source checkout,
then `.env` in the launch directory. Values are resolved in this order:

```text
command line > process environment > discovered env file > defaults
```

The parser is built in and does not export file values into the process environment. It
supports comments, `export NAME=VALUE`, and quoted values, but deliberately performs no
shell expansion. Keep `.env` local; it is ignored by Git.

## Generic OpenAI-compatible service

```dotenv
RIVET_PROTOCOL=openai_chat
RIVET_BASE_URL=https://provider.example/v1
RIVET_ENDPOINT_PATH=/chat/completions
RIVET_MODEL=provider-model-name
RIVET_API_KEY=replace-locally
RIVET_AUTH_STYLE=bearer
RIVET_EXTRA_BODY_JSON={}
RIVET_EXTRA_HEADERS_JSON={}
RIVET_REPLAY_FIELDS_JSON=["reasoning_content"]
```

Set `RIVET_ENDPOINT` to a complete URL when a service does not use the usual base/path
layout. Core body fields (`model`, `messages`, `tools`, `tool_choice`, and `stream`) cannot be
overridden through `RIVET_EXTRA_BODY_JSON`.

## DeepSeek example

DeepSeek is an interoperability example, not a hard-coded provider:

```dotenv
RIVET_PROTOCOL=openai_chat
RIVET_BASE_URL=https://api.deepseek.com
RIVET_MODEL=deepseek-v4-flash
RIVET_API_KEY=replace-locally
RIVET_AUTH_STYLE=bearer
RIVET_EXTRA_BODY_JSON={"thinking":{"type":"enabled"},"reasoning_effort":"low"}
RIVET_REPLAY_FIELDS_JSON=["reasoning_content"]
```

Thinking mode can be enabled in the extra body. If a compatible service returns
`reasoning_content`, Rivet retains it only as replayable protocol state; it is not rendered in
the TUI. Model availability changes over time, so confirm the current identifier in the
provider's documentation.

## API-key header and local services

For a service that uses a raw API-key header:

```dotenv
RIVET_AUTH_STYLE=api-key
RIVET_API_KEY_HEADER=X-API-Key
RIVET_API_KEY=replace-locally
```

For an unauthenticated local OpenAI-compatible server:

```dotenv
RIVET_BASE_URL=http://localhost:11434/v1
RIVET_MODEL=local-model-name
RIVET_AUTH_STYLE=none
RIVET_API_KEY=
```

Service headers can be supplied as a JSON object, for example
`RIVET_EXTRA_HEADERS_JSON={"X-Client":"rivet"}`. Header values and API keys are treated as
sensitive and are never printed by the compatibility check.

## Compatibility check

Run this before opening an interactive session:

```text
rivet --check-model
```

The command makes three small requests: a streamed text response, one forced-by-prompt
function call, and a tool-result round trip. It reports only capability status and sanitized
configuration metadata. A model that cannot reliably produce function calls cannot operate
the current coding-agent loop even if ordinary chat completion works.
