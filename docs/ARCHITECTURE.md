# CAworker architecture

CAworker is a stateful, terminal-only coding agent. One main Agent owns repository inspection, planning, edits, command execution, verification, and the final response.

## Runtime flow

```text
TUI input
  -> Agent.run
  -> ContextManager builds the active model view
  -> ModelClient returns text and/or tool calls
  -> ToolRegistry validates arguments and permissions
  -> Workspace executes the operation
  -> tool observation is appended to context
  -> model continues or returns a final answer
  -> SessionStore saves the completed turn
```

## Components

| Component | Responsibility |
| --- | --- |
| `cli.py` | CLI parsing and persistent terminal session loop |
| `tui.py` | Terminal input, streaming output, events, approvals and status rendering |
| `agent.py` | Agent Loop, completion gates, task evidence, cancellation and state export/restore |
| `types.py` | Provider-neutral message, reply, tool-call and client protocols |
| `client.py` / `provider.py` | OpenAI-compatible HTTP/SSE transport and client construction |
| `tools.py` | Tool definitions, local schema validation, approval and dispatch |
| `workspace.py` | Workspace-bounded files, commands, Diff, snapshots and undo |
| `plan.py` | Validated plan state and transitions |
| `context.py` | Active context, structured compression, raw archive and history search |
| `session.py` | Versioned, workspace-scoped JSON session persistence |
| `skills.py` / `prompt.py` | Skill discovery, activation and system prompt construction |

## Tool boundary

The main Agent receives these tools:

- `update_plan`
- `list_files`, `read_file`, `search_text`, `search_history`
- `write_file`, `replace_text`, `show_diff`
- `run_command`
- `list_skills`, `activate_skill`, `read_skill_resource`

Every tool schema is sent to the model and enforced again locally. Unknown fields, missing required values, invalid types and out-of-range values are rejected before execution. Tool failures are returned as observations so the model can revise its approach.

Each assistant tool-call batch is settled before a turn returns. Executed calls receive their real observation, calls deliberately left unexecuted receive `SKIPPED`, and a dispatch-boundary failure that cannot prove whether a side effect occurred receives `RESULT_UNKNOWN`. Result-unknown calls are not replayed automatically. Context validation rejects duplicate IDs, orphan results and incomplete exchanges before state export, restore or another model request.

Mutating tools follow `safe`, `ask`, or `never` approval mode. Workspace paths are resolved before use and must remain inside the selected root. Commands run with a timeout, cancellation support, a dangerous-command blocklist and bounded returned output.

## Completion evidence

`TaskState` records inspected files, changed files and command results. A new mutation invalidates earlier verification. The Agent requires a later successful verification command before completing a turn that changed files. Command-side file changes are detected through before/after workspace snapshots.

This mechanism proves that a configured check ran successfully after the latest edit. It does not prove that the chosen test fully covers the change.

## Planning

`PlanState` stores explicit steps with `pending`, `in_progress`, `completed`, or `blocked` status. It limits plan length, rejects duplicate steps and permits at most one in-progress item. An active unfinished plan prevents the Agent from returning a successful final answer.

## Context

`ContextManager` keeps the system prompt, current conversation view, structured summary, recent raw messages and archived raw units. Assistant tool calls and their contiguous tool results are grouped as indivisible units during compression. `search_history` retrieves archived material by keyword without embeddings or a vector database.

## Sessions

The TUI saves a versioned JSON session under the workspace `.rivet/sessions` directory after a completed turn. Stored state includes conversation, plan, context archive, task evidence, Diff baselines and the last result. API credentials and endpoint configuration are excluded.

Restore validates the workspace fingerprint and saved structures. External changes to tracked files invalidate prior verification evidence. A restored legacy session may contain fields no longer used by the current runtime; unknown fields are ignored.

## Skills

`SkillRegistry` discovers built-in, user and project Skills. Only validated metadata is present in the initial system prompt; full instructions enter context after `activate_skill`. Skill resources must be declared, UTF-8 text, bounded in size and inside the Skill directory. Skill content cannot bypass normal workspace and permission rules.

## Streaming and cancellation

`OpenAICompatibleClient` reconstructs streamed text, tool calls and configured replay fields from SSE. The Agent appends an assistant message after a complete model reply. `Ctrl+C` requests cooperative cancellation of model or command execution and returns control to the TUI without exiting the whole session.

Compaction, model, protocol and unexpected tool-boundary failures return a normalized `AgentResult` after settling any pending calls. TUI event callbacks run outside the execution bookkeeping boundary; callback failures are retained in Agent status for diagnosis and do not corrupt the conversation.

## Known reliability limits

The current Runtime still needs uniform typed tool outcomes, broader loop/no-progress detection, a hard request budget, execution-time checkpoints and wider permission/command/recovery behavior coverage. These are tracked in `CAWORKER_GAP_ANALYSIS.md`.
