# CAworker Gap Analysis — TUI-only baseline

## Scope

This audit describes the current Tenon repository as a terminal-only, single-Agent coding runtime. CAworker intentionally uses one main Agent for exploration, implementation, verification, and review. Additional interfaces and Agent roles are outside the current scope.

## Current Architecture

```text
User
  -> TUI / CLI
  -> Agent Loop
     -> ContextManager
     -> ModelClient
     -> ToolRegistry
        -> PlanState
        -> SkillRegistry
        -> Workspace
  -> SessionStore
```

The main execution path is concentrated in `Agent.run()` and `_run_turn()`. Model calls are provider-neutral through `ModelClient`. `ToolRegistry` validates arguments and approval before invoking handlers. `Workspace` owns filesystem and command boundaries. TUI saves the Agent state through `SessionStore` after each returned turn.

## Existing Features

- Multi-step Agent Loop with text and Function Calling.
- OpenAI-compatible HTTP and SSE client with retry/backoff.
- Central Tool Registry and local schema validation.
- Workspace-bounded read, search, write, exact edit, Diff and command tools.
- Explicit plan state with completion gating.
- Modification and post-edit verification evidence.
- JSON Session persistence and Resume.
- Structured context compression, raw archive and history search.
- safe, ask and never approval modes.
- Built-in, user and project Skill discovery and activation.
- Streaming terminal output, cancellation and session commands.
- Complete tool-call settlement for normal, skipped, cancelled and result-unknown operations.
- Normalized `AgentResult` returns for model, protocol, compaction and tool-boundary failures.
- Tool-exchange validation before restore, export and model requests.
- Event callback failure isolation with diagnostic status records.
- Offline Agent lifecycle and context protocol regression tests in CI.
- Canonical ToolOutcome status, error code, execution state and retry semantics.
- Backward-compatible interpretation of legacy tool and command results.

## Partially Implemented Features

### G01 — Tool-call settlement (completed in Phase 1-A)

Every advertised tool call now receives exactly one observation before a turn returns. Calls that were never executed are marked `SKIPPED`; a boundary failure after execution may have begun is marked `RESULT_UNKNOWN` and is never replayed automatically. Duplicate call IDs and incomplete or orphaned exchanges are rejected before restore, export or another model request.

### G02 — Core error and cancellation boundary (completed in Phase 1-A)

Context compaction, model requests, tool dispatch, observation recording and finalization now share one turn boundary. Expected cancellation and runtime/model/protocol failures return a normalized `AgentResult`, allowing the TUI to save the settled conversation. Event callback failures are isolated and exposed through Agent status without changing execution outcomes.

### G03 — Loop Guard is narrow

Current detection compares consecutive complete call-result signatures. It misses alternating cycles such as A/B/A/B, semantically identical command failures whose duration changes, and broader no-progress behavior. `max_steps` does not bound total tool calls or wall-clock time.

Recommended change: track call, normalized observation and progress separately; add a sliding window, total tool budget and turn deadline.

### G04 — Tool outcome semantics (completed in Phase 1-B)

All new tool observations now include canonical `status`, `code`, `execution_state` and `retryable` fields. Schema rejection, permission denial, known execution failure, timeout, cancellation, skipped execution and result-unknown boundaries have distinct status values. Command non-zero exits are explicitly `failed`, while the legacy `ok=true` field is retained for compatibility with consumers that interpret it as successful process dispatch. Agent state and TUI behavior use the canonical parser, which also infers equivalent outcomes from old saved observations.

### G05 — Plan exists, Plan Mode does not

`/plan` displays PlanState and `never` prevents mutation, but there is no explicit read-only analysis -> approved plan revision -> execute transition. Plan steps also lack stable IDs and a dedicated failed state.

Recommended change: extend PlanState incrementally and use the permission layer for mode enforcement.

### G06 — Session Resume is turn-level, not execution-level

Normal completed turns can be restored, but there is no durable checkpoint after each important step, pending-tool transaction state or policy for process crashes during side effects. Unknown write results must never be replayed automatically.

Recommended change: retain JSON initially, establish checkpoint ownership and recovery semantics, then evaluate SQLite only if measured needs justify it.

### G07 — Context compression is not a hard model budget

The limit is based on serialized message characters and excludes tool schemas and response reserve. One oversized recent message can remain over budget. Current Goal, Plan and verification evidence are program state but are not always injected as an authoritative current snapshot.

Recommended change: separate raw history, program state and the model Context View; add deterministic over-budget behavior.

### G08 — Permission rules are distributed

Tool mutation flags, approval mode, command classification and Workspace path rules work together but do not share a single policy decision object.

Recommended change: introduce a PolicyEngine returning ALLOW, ASK or DENY from role, mode, operation and resource while retaining current modes.

### G09 — Skill lifecycle is incomplete

Discovery and activation exist. Persistent enable/disable controls, explicit refresh behavior and management commands do not.

Recommended change: audit and extend the current registry only when needed; do not rewrite the Skill system.

### G10 — Systematic behavior tests are partial

CI now runs offline Agent lifecycle and context protocol tests for normal multi-call execution, early settlement, cancellation, model errors, unknown tool outcomes, duplicate IDs, callback failures and restore/export validation. Permission matrices, process timeout/evidence and crash-recovery scenarios still need broader coverage.

Recommended change: add standard-library fake-client tests with normal, failure and boundary cases from Phase 1 onward.

## Missing Features

The following mechanisms are absent from the current single-Agent TUI Runtime:

- Sliding-window/no-progress detection, total tool budget and turn deadline.
- Explicit Plan Mode and approved-plan revision transition.
- Stable Todo IDs and failed-state transition rules.
- Execution-time Session checkpoints and pending-tool recovery.
- Context budgeting that includes tool schemas and output reserve.
- Unified ALLOW/ASK/DENY PolicyEngine.
- Cross-session Memory Store.
- Broader permission, command and recovery behavior coverage plus a coding-task benchmark.

MCP, Plugin Marketplace, browser automation, multimodal input and remote execution remain optional future extensions.

## Capability Matrix

| Capability | Current state | Refactoring priority |
| --- | --- | --- |
| Agent Loop | Implemented; settlement and Core error boundary completed | High / Phase 1-C |
| Tool System | Registry, validation and canonical outcome semantics implemented | High / Phase 1-C |
| Error Recovery | Tool self-correction, HTTP retry and normalized Core/tool failures exist | High / Phase 1-C |
| Loop Guard | Consecutive duplicate detection and max steps only | High / Phase 1 |
| Plan / Todo | PlanState exists; no Plan Mode or stable task IDs | Medium / Phase 2 |
| Permission | safe/ask/never and resource rules exist; no unified engine | High / Phase 2 |
| Session / Resume | Turn-level JSON persistence exists; no execution checkpoints | Medium / Phase 3 |
| Context | Structured compression exists; no hard request budget | Medium / Phase 4 |
| Skill | Three-level registry and activation exist | Low / later audit |
| Memory | Current-session archive search only | Low / later phase |
| MCP / Plugin | Not implemented and currently out of scope | Low / optional |

## Technical Debt

1. `agent.py` and `workspace.py` contain several lifecycle responsibilities and need small extraction boundaries rather than full rewrites.
2. TaskState interprets specific tool names and dictionary fields, coupling evidence to tool implementations.
3. Session state contains overlapping conversation, archive, transcript, evidence and result representations; compatibility tests are required before schema changes.
4. ModelClient formally declares only `complete`; streaming and cancellation are discovered dynamically.
5. Returned output limits do not guarantee bounded memory use while reading files or collecting subprocess output.
6. The behavior suite covers the Phase 1-A lifecycle but permission, command and recovery coverage remains incomplete.

## Recommended Refactoring Order

| Phase | Work | Acceptance focus |
| --- | --- | --- |
| Phase 1-A (completed) | Tool-call settlement and unified turn finalization | No orphan call IDs; failure/cancellation produces a result; TUI saves returned failures |
| Phase 1-B (completed) | ToolOutcome and error classification | Schema, execution, denial, timeout and process failure have clear semantics |
| Phase 1-C | Loop Guard and execution budgets | Duplicate errors and cycles stop without false positives |
| Phase 2 | Todo, Plan Mode and PolicyEngine | Approved plan revisions and a tested permission matrix |
| Phase 3 | Session checkpoints and recovery | Safe restart without replaying unknown side effects |
| Phase 4 | Context View and hard request budget | Current goal/state retained and requests remain within budget |
| Phase 5 | Benchmark and evaluation | Reproducible completion, verification, recovery and safety metrics |
| Later | Skill lifecycle, Memory, MCP and Plugin evaluation | Each extension has a demonstrated need |

## Next Task

Proceed to Phase 1-C. Replace the consecutive-only repeat counter with bounded no-progress detection that can identify short alternating cycles without treating legitimate repeated reads as failure. Add a total tool-call budget and a monotonic turn deadline, then cover the exact stop reasons and settlement behavior with fake-client tests.
