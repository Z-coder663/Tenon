from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import Agent, AgentResult
from .errors import SessionError
from .types import JsonObject


SESSION_SCHEMA_VERSION = 1
MAX_SESSION_BYTES = 64_000_000
SESSION_REFERENCE = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    path: Path
    created_at: str
    updated_at: str
    turns: int
    total_steps: int
    task_preview: str
    model: str
    title: str
    pinned: bool
    result_status: str
    changed_count: int


@dataclass(frozen=True)
class LoadedSession:
    summary: SessionSummary
    agent_state: JsonObject


class SessionStore:
    """Versioned, atomic storage scoped to one workspace's ignored .rivet folder."""

    def __init__(self, workspace: Path, *, model: str) -> None:
        self.workspace = workspace.resolve()
        self.model = model
        self.workspace_fingerprint = self._workspace_fingerprint(self.workspace)
        self.root = self._session_root()

    def save(
        self,
        agent: Agent,
        result: AgentResult,
        *,
        target: Path | None = None,
    ) -> Path:
        path = self._new_path() if target is None else self._validate_target(target)
        now = self._now()
        created_at = now
        metadata: JsonObject = {"title": "", "pinned": False}
        if path.exists():
            existing = self._read_payload(path)
            created = existing.get("created_at")
            if isinstance(created, str):
                created_at = created
            saved_metadata = existing.get("metadata")
            if isinstance(saved_metadata, dict):
                metadata = dict(saved_metadata)

        payload: JsonObject = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": path.stem,
            "workspace_fingerprint": self.workspace_fingerprint,
            "created_at": created_at,
            "updated_at": now,
            "model": self.model,
            "mode": "interactive",
            "metadata": metadata,
            "agent": agent.export_session_state(),
            "last_result": {
                "success": result.success,
                "reason": result.reason,
                "steps": result.steps,
                "final": result.final,
            },
        }
        self._write_payload(path, payload)
        return path

    def load(self, reference: str | None = None) -> LoadedSession:
        if reference:
            path = self._path_for_reference(reference)
            payload = self._read_payload(path)
            return LoadedSession(
                self._summary(path, payload), self._agent_state_with_result(payload)
            )

        summaries = self.list_sessions(limit=100)
        if not summaries:
            raise SessionError("no saved sessions were found in this workspace")
        summary = max(summaries, key=lambda item: (item.updated_at, item.session_id))
        payload = self._read_payload(summary.path)
        return LoadedSession(summary, self._agent_state_with_result(payload))

    @staticmethod
    def _agent_state_with_result(payload: JsonObject) -> JsonObject:
        """Supply outer result metadata to sessions saved before Agent embedded it."""
        agent_state = dict(payload["agent"])
        if "last_result" not in agent_state and isinstance(payload.get("last_result"), dict):
            agent_state["last_result"] = dict(payload["last_result"])
        return agent_state

    def rename(self, reference: str, title: str) -> SessionSummary:
        normalized = " ".join(title.split())
        if not normalized or len(normalized) > 120:
            raise SessionError("session title must contain 1 to 120 characters")
        path = self._path_for_reference(reference)
        payload = self._read_payload(path)
        metadata = dict(payload.get("metadata") or {})
        metadata["title"] = normalized
        payload["metadata"] = metadata
        self._write_payload(path, payload)
        return self._summary(path, payload)

    def set_pinned(self, reference: str, pinned: bool) -> SessionSummary:
        if not isinstance(pinned, bool):
            raise SessionError("pinned state must be boolean")
        path = self._path_for_reference(reference)
        payload = self._read_payload(path)
        metadata = dict(payload.get("metadata") or {})
        metadata["pinned"] = pinned
        payload["metadata"] = metadata
        self._write_payload(path, payload)
        return self._summary(path, payload)

    def delete(self, reference: str) -> str:
        path = self._path_for_reference(reference)
        try:
            path.unlink()
        except OSError as exc:
            raise SessionError(f"could not delete saved session: {exc}") from exc
        return path.stem

    def update_agent_state(self, target: Path, agent: Agent) -> None:
        """Persist an out-of-band UI state change without replacing result metadata."""
        path = self._validate_target(target)
        payload = self._read_payload(path)
        payload["agent"] = agent.export_session_state()
        payload["updated_at"] = self._now()
        self._write_payload(path, payload)

    def export_markdown(self, reference: str) -> tuple[str, str]:
        path = self._path_for_reference(reference)
        payload = self._read_payload(path)
        summary = self._summary(path, payload)
        agent = payload["agent"]
        tasks = agent.get("tasks", []) if isinstance(agent, dict) else []
        conversation = agent.get("conversation", []) if isinstance(agent, dict) else []
        transcript = agent.get("transcript") if isinstance(agent, dict) else None
        lines = [
            f"# {summary.title}",
            "",
            f"- 会话：`{summary.session_id}`",
            f"- 模型：`{summary.model}`",
            f"- 更新时间：{summary.updated_at}",
            f"- 轮次：{summary.turns}",
            "",
            "## 对话",
            "",
        ]
        if isinstance(transcript, list):
            for message in transcript:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if role == "user":
                    lines.extend(["### 用户", "", content, ""])
                elif role == "assistant":
                    lines.extend(["### Rivet", "", content, ""])
        elif isinstance(conversation, list):
            task_index = 0
            for message in conversation:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if role == "user":
                    if task_index >= len(tasks) or content != tasks[task_index]:
                        continue
                    task_index += 1
                    lines.extend(["### 用户", "", content, ""])
                elif role == "assistant":
                    lines.extend(["### Rivet", "", content, ""])
        filename = re.sub(r"[^\w.-]+", "-", summary.title, flags=re.UNICODE).strip("-.")
        return (filename[:80] or summary.session_id) + ".md", "\n".join(lines).rstrip() + "\n"

    def list_sessions(self, *, limit: int = 10) -> list[SessionSummary]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SessionError("session list limit must be between 1 and 100")
        summaries: list[SessionSummary] = []
        for path in self.root.glob("*.json"):
            try:
                safe_path = self._validate_target(path)
                payload = self._read_payload(safe_path)
                summaries.append(self._summary(safe_path, payload))
            except SessionError:
                continue
        summaries.sort(
            key=lambda item: (item.pinned, item.updated_at, item.session_id), reverse=True
        )
        return summaries[:limit]

    def _session_root(self) -> Path:
        rivet_dir = self.workspace / ".rivet"
        if rivet_dir.exists():
            self._require_within_workspace(rivet_dir.resolve())
        rivet_dir.mkdir(parents=True, exist_ok=True)
        root = rivet_dir / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        self._require_within_workspace(resolved)
        return resolved

    def _require_within_workspace(self, path: Path) -> None:
        try:
            path.relative_to(self.workspace)
        except ValueError as exc:
            raise SessionError("session directory escapes the selected workspace") from exc

    def _new_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.root / f"{timestamp}.json"
        suffix = 1
        while target.exists():
            target = self.root / f"{timestamp}-{suffix}.json"
            suffix += 1
        return target

    def _path_for_reference(self, reference: str) -> Path:
        name = reference.strip()
        if name.lower().endswith(".json"):
            name = name[:-5]
        if not name or not SESSION_REFERENCE.fullmatch(name):
            raise SessionError("invalid session id; use /sessions to view valid ids")
        path = self._validate_target(self.root / f"{name}.json")
        if not path.exists():
            raise SessionError(f"saved session not found: {name}")
        return path

    def _validate_target(self, target: Path) -> Path:
        candidate = target if target.is_absolute() else self.root / target
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SessionError("session file escapes the session directory") from exc
        if resolved.parent != self.root or resolved.suffix.lower() != ".json":
            raise SessionError("session file must be a JSON file in the session directory")
        return resolved

    def _read_payload(self, path: Path) -> JsonObject:
        safe_path = self._validate_target(path)
        try:
            if safe_path.stat().st_size > MAX_SESSION_BYTES:
                raise SessionError("saved session exceeds the size limit")
            raw = safe_path.read_text(encoding="utf-8")
            payload: Any = json.loads(raw)
        except FileNotFoundError as exc:
            raise SessionError(f"saved session not found: {safe_path.stem}") from exc
        except UnicodeDecodeError as exc:
            raise SessionError(f"saved session is not UTF-8: {safe_path.stem}") from exc
        except json.JSONDecodeError as exc:
            raise SessionError(f"saved session is invalid JSON: {safe_path.stem}") from exc
        except OSError as exc:
            raise SessionError(f"could not read saved session: {exc}") from exc
        if not isinstance(payload, dict):
            raise SessionError("saved session root must be an object")
        return self._normalize_payload(safe_path, payload)

    def _normalize_payload(self, path: Path, payload: JsonObject) -> JsonObject:
        version = payload.get("schema_version")
        if version is None:
            payload = self._upgrade_legacy(path, payload)
            version = SESSION_SCHEMA_VERSION
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != SESSION_SCHEMA_VERSION
        ):
            raise SessionError(f"unsupported saved session version: {version}")
        if payload.get("workspace_fingerprint") != self.workspace_fingerprint:
            raise SessionError("saved session belongs to a different workspace")
        if not isinstance(payload.get("agent"), dict):
            raise SessionError("saved session has no valid agent state")
        for name in ("created_at", "updated_at", "model"):
            if not isinstance(payload.get(name), str):
                raise SessionError(f"saved session has no valid {name}")
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {"title": "", "pinned": False}
            payload["metadata"] = metadata
        if not isinstance(metadata, dict):
            raise SessionError("saved session metadata must be an object")
        title = metadata.get("title", "")
        pinned = metadata.get("pinned", False)
        if not isinstance(title, str) or len(title) > 120 or not isinstance(pinned, bool):
            raise SessionError("saved session metadata is invalid")
        metadata["title"] = " ".join(title.split())
        metadata["pinned"] = pinned
        return payload

    def _upgrade_legacy(self, path: Path, payload: JsonObject) -> JsonObject:
        tasks = payload.get("tasks")
        messages = payload.get("messages")
        state = payload.get("state")
        if (
            not isinstance(tasks, list)
            or not isinstance(messages, list)
            or not isinstance(state, dict)
        ):
            raise SessionError("saved session uses an unknown format")
        conversation = list(messages)
        if (
            conversation
            and isinstance(conversation[0], dict)
            and conversation[0].get("role") == "system"
        ):
            conversation = conversation[1:]

        changed = state.get("changed_files", [])
        commands = state.get("commands", [])
        verified = bool(state.get("verification_passed"))
        if not isinstance(changed, list):
            changed = []
        if not isinstance(commands, list):
            commands = []
        if changed:
            operation_index = max(len(commands), 2 if verified else 1)
            last_mutation = operation_index - 1 if verified else operation_index
            last_successful = operation_index if verified else 0
        else:
            operation_index = len(commands)
            last_mutation = 0
            last_successful = 0

        timestamp = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": path.stem,
            "workspace_fingerprint": self.workspace_fingerprint,
            "created_at": timestamp,
            "updated_at": timestamp,
            "model": "legacy",
            "mode": "interactive",
            "metadata": {"title": "", "pinned": False},
            "agent": {
                "tasks": tasks,
                "turns": payload.get("turns", len(tasks)),
                "total_steps": payload.get("total_steps", 0),
                "conversation": conversation,
                "task_state": {
                    "inspected_files": state.get("inspected_files", []),
                    "changed_files": changed,
                    "commands": commands,
                    "operation_index": operation_index,
                    "last_mutation_operation": last_mutation,
                    "last_successful_command_operation": last_successful,
                    "workspace_tracking_complete": not bool(changed),
                },
                "workspace_state": {
                    "original_text": [],
                    "unavailable_diffs": [],
                },
            },
            "last_result": {
                "success": bool(payload.get("success")),
                "reason": str(payload.get("reason") or "legacy"),
                "steps": payload.get("last_steps", 0),
                "final": str(payload.get("final") or ""),
            },
        }

    def _summary(self, path: Path, payload: JsonObject) -> SessionSummary:
        agent = payload["agent"]
        tasks = agent.get("tasks") if isinstance(agent, dict) else None
        first_task = tasks[0] if isinstance(tasks, list) and tasks else ""
        preview = " ".join(str(first_task).split())[:100]
        turns = agent.get("turns", 0) if isinstance(agent, dict) else 0
        total_steps = agent.get("total_steps", 0) if isinstance(agent, dict) else 0
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
            raise SessionError("saved session has an invalid turn count")
        if (
            not isinstance(total_steps, int)
            or isinstance(total_steps, bool)
            or total_steps < 0
        ):
            raise SessionError("saved session has an invalid step count")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        title = str(metadata.get("title") or preview or "未命名会话")
        pinned = bool(metadata.get("pinned"))
        last_result = payload.get("last_result")
        result_status = "unknown"
        if isinstance(last_result, dict):
            result_status = "completed" if last_result.get("success") is True else str(
                last_result.get("reason") or "stopped"
            )
        task_state = agent.get("task_state") if isinstance(agent, dict) else None
        changed = task_state.get("changed_files", []) if isinstance(task_state, dict) else []
        changed_count = len(changed) if isinstance(changed, list) else 0
        return SessionSummary(
            session_id=path.stem,
            path=path,
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            turns=turns,
            total_steps=total_steps,
            task_preview=preview,
            model=payload["model"],
            title=title,
            pinned=pinned,
            result_status=result_status,
            changed_count=changed_count,
        )

    def _write_payload(self, target: Path, payload: JsonObject) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > MAX_SESSION_BYTES:
            raise SessionError("conversation is too large to save safely")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=self.root, prefix="session-", suffix=".tmp", delete=False
            ) as stream:
                stream.write(encoded)
                temporary_name = stream.name
            os.replace(temporary_name, target)
        except OSError as exc:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise SessionError(f"could not save conversation: {exc}") from exc

    @staticmethod
    def _workspace_fingerprint(workspace: Path) -> str:
        normalized = os.path.normcase(str(workspace.resolve())).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
