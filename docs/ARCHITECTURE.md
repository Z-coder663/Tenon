# Rivet architecture

Rivet is deliberately small: the model proposes actions, but every action is parsed,
bounded, executed, observed, and recorded by code in this repository. It uses no agent
framework and no server-hosted file or execution tool.

## Data flow

```text
repeated user turns
        |
      Agent -------- ContextManager ---- tool schemas ----> ModelClient
        ^  ^               ^                                     |
        |  |               |                                     v
 PlanState |         tool result <---- ReAct loop <---- normalized model reply
        ^  |                    |
        |  |                    v
        |  |             SubAgentManager ---- isolated child Agent(s)
        |  +------------- SkillRegistry ---- progressive SKILL.md disclosure
        | SessionStore
        |  ^
        |  |
        +- versioned JSON
                           ^                 |
                           |                 v
                ClientFactory     ToolRegistry ---- Workspace ---- local files and subprocesses
```

The loop is implemented in `src/rivet/agent.py`. `Agent` owns the message history, tool
registry, diff baseline, and accumulated execution evidence for one terminal conversation.
Each model response becomes an
assistant message. Function calls are JSON-decoded and dispatched by `ToolRegistry`;
the resulting JSON is appended as a `tool` message with the original call ID. A plain
assistant message completes the current user turn, but the session remains available for
follow-up instructions. Only `/exit` ends the process; `Agent.reset()` implements `/new` by
clearing conversation state and creating a fresh tool/diff boundary.

## Responsibility map

| Requirement | Implementation |
| --- | --- |
| Conversation/session | `Agent` retains user turns, assistant messages, tool results, diff baseline, and execution evidence |
| Provider configuration | `Config` resolves CLI, process environment, `.env`, endpoints, authentication, and bounded extensions |
| Protocol selection | `ClientFactory` creates a `ModelClient`; provider names never enter the Agent loop |
| Task planning | `PlanState` validates explicit step status and exposes it through `update_plan`, `/plan`, and TUI events |
| Session persistence | `SessionStore` atomically saves and validates versioned, workspace-scoped state |
| Context bounding | `ContextManager` preserves the system prompt, first task, recent call-result units, and a deterministic checkpoint |
| Terminal presentation | `Console` in `tui.py` renders compact semantic events with ANSI and plain-text modes |
| Browser presentation | `WebRuntime` streams the same semantic events to a local two-column chat workspace |
| Multi-agent orchestration | `SubAgentManager` creates bounded child Agents, enforces depth and mode limits, and returns structured reports |
| Reusable workflows | `SkillRegistry` discovers scoped `SKILL.md` files, discloses instructions on demand, and records usage |
| Tool definitions | JSON-schema function tools in `ToolRegistry` with local validation and permission-scoped visibility |
| Local execution | `Workspace` reads, searches, atomically edits, and runs bounded subprocesses |
| Output parsing | `OpenAICompatibleClient` validates JSON replies, rebuilds SSE/tool deltas, and retains configured replay fields |
| Completion evidence | `TaskState` records inspected/changed files and command outcomes; edits require a later successful check |
| Termination | Verified final text, max steps, two empty replies, or three identical call-result observations |
| Error handling | Structured tool errors, HTTP retry/backoff, timeouts, truncation, and secret redaction |

## Model provider boundary

The Agent depends only on the small `ModelClient` protocol. `ClientFactory` selects a protocol
adapter from validated configuration, so model brands do not appear in the ReAct loop. The
current `openai_chat` adapter works with services that implement Chat Completions, SSE, and
function tools. A genuinely different wire protocol can be added as another adapter without
changing planning, tools, session persistence, or the terminal interface.

Configuration follows `command line > process environment > .env > defaults`. The built-in
`.env` reader does not mutate the process environment or perform shell expansion. It supports
a complete endpoint or a base URL plus path, bearer/raw API-key/no authentication, bounded
provider request JSON, service headers, and a list of assistant response fields that must be
replayed on later requests. Core message, tool, stream, and HTTP content-negotiation fields
cannot be replaced by provider extensions. Credentials and header values are redacted from
diagnostics and errors.

Some reasoning-capable APIs require fields such as `reasoning_content` from an assistant tool
call to be sent back with the later tool result. The protocol adapter captures only configured
replay fields, keeps them in the normalized assistant message, and never sends them to the TUI
as visible answer text. `rivet --check-model` exercises a streamed reply, a function call, and
a tool-result follow-up against the selected endpoint before an interactive session starts.

## Local web interface

`rivet --web` starts a dependency-free HTTP server on `127.0.0.1` and opens a two-column chat
workspace: saved sessions on the left and the active conversation in the center. Planning,
changed files, verification state, and tool activity live in an on-demand inspector rather
than competing with the primary conversation. The same `Agent`, `SessionStore`, tool registry,
and approval policy are shared with the terminal interface.

Model and tool events are returned as newline-delimited JSON over one streamed request. A
mutating tool that needs confirmation emits an approval event and waits on a bounded local
condition until the browser allows or rejects it. New-session, resume, status, and diff actions
use small JSON endpoints. A separate cancel endpoint can interrupt the streamed model request,
an approval wait, or a running command without closing the browser session. Assistant text is
rendered through a safe DOM-based Markdown subset with copyable code blocks, while unified
diffs are parsed into per-file, line-numbered views. No provider credential is serialized to
the browser.

The top bar can switch `safe`, `ask`, and `never` while the Agent is idle. The token-protected
settings endpoint replaces the immutable runtime configuration and updates the Agent and active
tool registry together; it refuses changes during a turn or approval wait. This is a process
preference rather than session data, so a restart returns to the CLI or `.env` default. The TUI
exposes the same operation through `/permissions [mode]`.

The server is intentionally local-only: it rejects non-loopback host/origin values, requires a
random per-process request token, sends no CORS permission, and applies a restrictive content
security policy. It is not a hosted control plane and does not make the selected workspace
remotely accessible.

## Tools

- `update_plan`: validated progress state for multi-stage work; not a workspace mutation.
- `list_files`: bounded traversal with noisy cache/build directories omitted.
- `read_file`: UTF-8 text with stable line numbers and binary rejection.
- `search_text`: literal, glob-aware repository search.
- `write_file`: atomic create/replace inside the workspace.
- `replace_text`: exact, count-checked atomic edit that fails safely on stale context.
- `show_diff`: unified diff against each file's first in-session state.
- `run_command`: cancellable subprocess group in the workspace with timeout, bounded output,
  stripped credential variables, a blocklist for obviously destructive/external commands,
  and before/after workspace snapshots that detect indirect file changes.
- `delegate_task`: one bounded exploration or review assignment with an
  isolated context and structured result.
- `delegate_readonly_tasks`: exactly two independent exploration/review assignments executed
  concurrently with separate model clients.
- `list_skills`: skill metadata and activation state without instruction bodies.
- `activate_skill`: disclose one relevant `SKILL.md` workflow for the current turn.
- `read_skill_resource`: read one listed UTF-8 resource from an already active skill.

Tool schemas are sent to the model and enforced again by `ToolRegistry`. Required fields,
unknown fields, scalar types, ranges, and string sizes are rejected before approval or local
execution. Errors include a stable code, field name when relevant, and retryability hint.

## Skills

`SkillRegistry` discovers immediate child directories under the packaged built-in skill root,
the user's `~/.rivet/skills`, and the current project's `.rivet/skills`; later scopes override
same-named earlier ones. Startup puts only validated names, descriptions, and source labels in
the system catalog. The model must call `activate_skill` before instructions enter the ReAct
context, and `read_skill_resource` accepts only resources pre-listed for that active skill.

Skill names and front matter are bounded and validated without a YAML dependency. Symlinked,
oversized, binary, path-escaping, or malformed content is rejected. Reading a resource never
executes it, and skill text remains subordinate to system safety, user intent, workspace
permissions, and the normal approval gate. Each turn starts with no active skill; activation
history is persisted so resumed sessions and both interfaces can show what was used.

## Multi-agent orchestration

`SubAgentManager` is invoked through ordinary tool calls, so delegation does not bypass the
ReAct loop. A child receives a fresh `ContextManager`, a smaller step budget, a mode-specific
system prompt, and the same workspace diff baseline. The parent receives only a bounded report
containing status, summary, inspected files, command evidence, changed files, verification,
and risks. Child transcripts are not copied into the parent's context.

`explore` and `review` registries expose only planning, listing, reading, searching, and diff
tools. Children are created with delegation disabled, which fixes orchestration depth at one.
A per-turn count prevents runaway fan-out; exactly two independent read-only tasks may use a
bounded thread pool. Each parallel child gets its own protocol client so cancellation and
streamed responses do not share HTTP state.

All child Agents share the parent's `Workspace` object only for a consistent read-only view of
the current diff baseline. They cannot edit files or run commands; the main Agent owns every
mutation, integration decision, and verification result.
The active child set and recent reports are available through `Agent.status()`, persisted with
the session, and rendered as semantic status events in both interfaces. Cancelling the parent
sets the shared cancellation event and closes every active child's model request.

## Task planning

`PlanState` is program-owned rather than inferred from assistant prose. For a multi-stage task,
the model calls `update_plan` with concise steps whose status is `pending`, `in_progress`,
`completed`, or `blocked`; local validation limits plan size, rejects duplicate steps, and
allows at most one in-progress item. Repeating an unchanged plan produces an unchanged result,
so loop detection still recognizes no progress. A final answer is rejected once when an active
plan remains unfinished, then stopped explicitly if the model still fails to update it.

Plan updates emit semantic TUI events and require no workspace approval because they do not
change files. `/plan` renders the current state, `/status` shows its summary, and session files
persist the validated plan alongside conversation and execution evidence. A cancelled or
resumed task keeps unfinished progress; a new turn clears a terminal plan, while `/new` resets
all planning state. A plan containing a genuinely blocked terminal step finishes with a blocked
result instead of reporting success.

## Completion evidence

The model does not decide success by text alone after editing. `TaskState` records operations
that actually succeeded. A file mutation invalidates earlier command evidence; a later command
must finish with exit code zero before the task can return `completed_verified`. The first
premature final answer receives a corrective prompt, and the second stops as
`unverified_changes`. Read-only analysis tasks may still finish without a command.
`run_command` classifies common test/build/lint commands automatically and also accepts an
explicit `purpose` of `inspect` or `verify` for project-specific checks; only successful
verification commands satisfy completion evidence.

Before each command, Rivet hashes up to 10,000 non-cache workspace files and retains a
bounded amount of original UTF-8 text. A second snapshot detects created, modified, and
deleted files, registers them in `TaskState`, and feeds their original content into
`show_diff`. A command that changes files cannot verify those same changes in the same
operation; a later successful check is required. Binary, oversized, unreadable, or
snapshot-budget-limited changes remain visible by path with an explicit unavailable/limited
marker instead of silently disappearing.

Repeated-call protection hashes both the canonical tool call and its returned observation.
This stops genuine no-progress loops while allowing a repeated read whose content changed.

## Streaming and cancellation

`OpenAICompatibleClient.complete_stream()` requests Chat Completions SSE. Text from
`delta.content` is forwarded immediately, while `delta.tool_calls` fragments are accumulated
by their numeric index. Configured replay-field strings, arrays, and object fragments are
accumulated separately and never rendered. IDs, function names, and JSON argument strings are
validated only after the stream completes. Empty usage chunks and SSE keep-alive comments are
ignored, `[DONE]` and finish reasons close the stream, and a truncated or malformed stream
fails explicitly. A compatible gateway that rejects streaming before emitting any content is
retried through the existing JSON request.

The Agent does not append an assistant message until a model stream is complete. `Ctrl+C` in
the TUI or the Web stop action during a request discards partial text and records a short
cancellation marker instead. If a tool is interrupted, synthetic cancellation results complete
every advertised tool-call ID, so subsequent API requests never contain orphaned calls.
`run_command` uses a process group; Windows assigns the shell and descendants to a Job Object
and retains `taskkill /T /F` as a fallback, while POSIX sends signals to the process group.
After termination, the normal workspace snapshot comparison still records partial file side
effects. A cancelled verification command can never satisfy completion evidence. At an idle
prompt, `Ctrl+C` clears the input and keeps the session open.

## Multi-turn session and context management

Launching `rivet` starts a persistent input loop. A later user
request is appended to the same history after the preceding assistant final answer, so
references such as "the function you just changed" have the required conversational context.
Each user turn receives a fresh step budget and loop-protection counters, while successful
tool evidence and the workspace diff remain session-wide. `/plan`, `/status`, `/diff`, `/sessions`,
`/resume`, `/skills`, `/new`, and `/exit` expose the essential session controls. `/resume` without an
argument loads the most recently updated valid session; an ID from `/sessions` selects a
specific one. `/new` resets the Agent and creates a new `ToolRegistry`, so conversation
history and the in-memory diff baseline do not leak across conversations.

`SessionStore` atomically updates one versioned JSON file after each completed turn under
`.rivet/sessions`, which is ignored by Git. It persists provider messages, the current plan,
exact internal operation counters, command evidence, and the original file baselines needed by `/diff`.
The primary system prompt is reconstructed rather than saved because it contains the local
workspace path; API credentials and endpoint configuration are never part of session state.
A hash of the normalized workspace path prevents a session copied from another project from
being resumed accidentally. Saved identities of tracked files are checked during restore;
changes made while Rivet was closed are registered as new mutations and invalidate older
verification evidence. Malformed, oversized, path-escaping, unsupported-version, and
cross-workspace files are rejected. Transcript-only files from version 1.1 are upgraded with
an explicitly limited diff state.

Character count is used as a provider-neutral upper-bound heuristic. When history grows
past the configured limit, old messages are grouped into semantic units. An assistant
message containing tool calls and all matching tool results are indivisible; this avoids
orphaned `tool_call_id` values. Old units become a short factual checkpoint, while recent
units remain verbatim. Tool output itself is head-tail truncated so diagnostics at both
ends survive.

## Failure policy

| Failure | Response |
| --- | --- |
| 408/409/429/5xx or transient network error | Exponential backoff, then a sanitized `ModelError` |
| Malformed model JSON or schema | Stop with an explicit protocol error |
| Malformed/unknown tool call | Return `{ok:false,error:...}` to the model so it can recover |
| Path traversal or binary text read | Reject before access |
| Command timeout | Return partial output and `timed_out:true` |
| User cancellation | Stop the active turn, preserve completed evidence, save the session, and return to input |
| Stale exact edit | Reject; model must re-read instead of corrupting the file |
| Final answer after an unverified edit | Reprompt once, then stop as `unverified_changes` |
| Empty model reply | One corrective reprompt, then stop |
| Repeating loop | Stop on the third identical call-result observation |
| Runaway task | Hard `max_steps` limit |

## Security boundary and trade-offs

Path tools resolve symlinks and reject locations outside the selected workspace. `.env` and
session state are ignored by Git. API keys are sent only in the configured model HTTP header,
never logged, and credentials plus custom header values are redacted from endpoint errors.
Credential-shaped environment variables are removed from subprocesses. `safe`, `ask`, and
read-only `never` approval modes make the write boundary visible.

`run_command` is intentionally capable and is not an OS sandbox. Destructive commands remain
blocked, while package installation, network access, and Git history/branch mutations require
approval even in `safe` mode. For hostile repositories,
run Rivet in a disposable container or VM and use `--approval ask`. The project favors a
small transparent core over provider-specific features. Chat Completions is widely
supported by compatible gateways; a second protocol can be added behind the `ModelClient`
interface without changing the agent loop.

