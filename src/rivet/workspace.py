from __future__ import annotations

import difflib
import hashlib
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError, WorkspaceViolation


DEFAULT_IGNORES = {
    ".git",
    ".rivet",
    ".venv",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

SNAPSHOT_MAX_FILES = 10_000
SNAPSHOT_TEXT_FILE_BYTES = 2_000_000
SNAPSHOT_TEXT_BUDGET_BYTES = 20_000_000
SNAPSHOT_CHANGE_REPORT_LIMIT = 200
SNAPSHOT_IGNORED_FILES = {".coverage", ".DS_Store"}
PREVIEW_DENIED_DIRECTORIES = {".aws", ".gnupg", ".ssh"}
PREVIEW_DENIED_FILES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
}
PREVIEW_DENIED_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
OPERATION_HISTORY_LIMIT = 200
OPERATION_FILES_LIMIT = 2_000


@dataclass(frozen=True)
class FileSnapshot:
    digest: str
    size: int
    text: str | None
    text_available: bool


@dataclass(frozen=True)
class WorkspaceSnapshot:
    files: dict[Path, FileSnapshot]
    complete: bool

RISKY_COMMANDS = (
    re.compile(r"\b(?:rm|rmdir)\b.*(?:-[a-zA-Z]*r|/s)"),
    re.compile(r"\b(?:del|erase)\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\b.*-Recurse", re.IGNORECASE),
    re.compile(r"\bgit(?:\s+-\S+)*\s+(?:push|clean|reset\s+--hard)\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b.*(?:--data|--upload-file|-d\s)", re.IGNORECASE),
    re.compile(r"\bscp\b", re.IGNORECASE),
    re.compile(r"^\s*(?:shutdown|reboot|format)(?:\s|$)", re.IGNORECASE),
)

REVIEW_COMMANDS = (
    (
        "downloads or network access",
        re.compile(r"\b(?:curl|wget|Invoke-WebRequest|iwr)\b", re.IGNORECASE),
    ),
    (
        "package installation",
        re.compile(
            r"\b(?:pip|pip3|uv)\s+(?:install|uninstall)|"
            r"\b(?:npm|pnpm|yarn)\s+(?:install|add|remove|ci)|"
            r"\b(?:cargo|gem)\s+install\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Git history or branch mutation",
        re.compile(
            r"\bgit(?:\s+-\S+)*\s+(?:add|commit|checkout|switch|merge|rebase|tag)\b",
            re.IGNORECASE,
        ),
    ),
)

VERIFICATION_COMMANDS = (
    re.compile(
        r"\b(?:python(?:3)?|py)(?:\.exe)?\b.*\s-m\s+"
        r"(?:unittest|pytest|compileall|mypy|ruff|pyright)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:pytest|tox|nox|ruff|mypy|pyright)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:npm|pnpm|yarn)\s+(?:test|run\s+(?:test|build|lint|check|typecheck))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcargo\s+(?:test|check|clippy|build)\b", re.IGNORECASE),
    re.compile(r"\bgo\s+test\b|\bdotnet\s+(?:test|build)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:mvn|gradle|gradlew)\s+(?:test|verify|check|build)\b|"
        r"\bmake\s+(?:test|check|build)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgit\s+diff\s+--check\b", re.IGNORECASE),
)


class Workspace:
    """All filesystem operations are resolved against one trusted root."""

    def __init__(
        self,
        root: Path,
        *,
        max_output_chars: int = 20_000,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.root = root.resolve()
        self.max_output_chars = max_output_chars
        self.cancel_event = cancel_event
        self._original_text: dict[Path, str | None] = {}
        self._unavailable_diffs: dict[Path, str] = {}
        self._tracked_current: dict[Path, dict[str, Any]] = {}
        self._operations: list[dict[str, Any]] = []
        self._next_operation_id = 1
        self._pending_operation: dict[str, Any] | None = None

    def export_diff_state(self) -> dict[str, Any]:
        """Return the bounded baseline needed for /diff after session resume."""
        original = []
        for target in sorted(self._original_text, key=lambda item: self.display(item)):
            original.append(
                {
                    "path": self.display(target),
                    "content": self._original_text[target],
                    "current": self._tracked_current.get(
                        target, self._file_identity(target)
                    ),
                }
            )
        unavailable = []
        for target in sorted(self._unavailable_diffs, key=lambda item: self.display(item)):
            unavailable.append(
                {
                    "path": self.display(target),
                    "reason": self._unavailable_diffs[target],
                    "current": self._tracked_current.get(
                        target, self._file_identity(target)
                    ),
                }
            )
        return {
            "original_text": original,
            "unavailable_diffs": unavailable,
            "operations": self._export_operations(),
            "next_operation_id": self._next_operation_id,
        }

    def restore_diff_state(self, payload: Any) -> list[str]:
        """Restore diff baselines and report files changed since the session was saved."""
        if not isinstance(payload, dict):
            raise ToolError("saved workspace state must be an object")
        original = payload.get("original_text", [])
        unavailable = payload.get("unavailable_diffs", [])
        if not isinstance(original, list) or not isinstance(unavailable, list):
            raise ToolError("saved workspace diff entries must be lists")
        if len(original) + len(unavailable) > SNAPSHOT_MAX_FILES:
            raise ToolError("saved workspace diff contains too many files")

        restored_original: dict[Path, str | None] = {}
        restored_unavailable: dict[Path, str] = {}
        saved_identities: dict[Path, dict[str, Any]] = {}
        text_bytes = 0

        for entry in original:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ToolError("saved text diff entry is invalid")
            target = self.resolve(entry["path"])
            if target in restored_original or target in restored_unavailable:
                raise ToolError(f"duplicate saved diff path: {self.display(target)}")
            content = entry.get("content")
            if content is not None and not isinstance(content, str):
                raise ToolError(f"invalid saved diff content: {self.display(target)}")
            if isinstance(content, str):
                text_bytes += len(content.encode("utf-8"))
                if text_bytes > SNAPSHOT_TEXT_BUDGET_BYTES:
                    raise ToolError("saved workspace diff text exceeds the restore budget")
            restored_original[target] = content
            saved_identities[target] = self._validate_file_identity(entry.get("current"))

        for entry in unavailable:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("reason"), str)
            ):
                raise ToolError("saved unavailable diff entry is invalid")
            target = self.resolve(entry["path"])
            if target in restored_original or target in restored_unavailable:
                raise ToolError(f"duplicate saved diff path: {self.display(target)}")
            restored_unavailable[target] = entry["reason"]
            saved_identities[target] = self._validate_file_identity(entry.get("current"))

        drifted = [
            self.display(target)
            for target, identity in saved_identities.items()
            if self._file_identity(target) != identity
        ]
        self._original_text = restored_original
        self._unavailable_diffs = restored_unavailable
        self._tracked_current = saved_identities
        self._operations, self._next_operation_id = self._restore_operations(
            payload.get("operations", []), payload.get("next_operation_id")
        )
        return sorted(drifted)

    def begin_turn_operation(self, turn: int, task: str) -> None:
        """Capture the workspace state before one user/agent turn."""
        if self._pending_operation is not None:
            raise ToolError("a turn checkpoint is already active")
        self._pending_operation = {
            "turn": turn,
            "task": " ".join(task.split())[:240],
            "before": self._capture_snapshot(capture_text=True),
        }

    def finish_turn_operation(self) -> dict[str, Any] | None:
        """Commit one undoable record for all changes made during the turn."""
        pending = self._pending_operation
        self._pending_operation = None
        if pending is None:
            return None
        before = pending["before"]
        if not isinstance(before, WorkspaceSnapshot):
            return None
        after = self._capture_snapshot(capture_text=True)
        changes = self._snapshot_changes(before, after)
        if not changes:
            return None

        files: list[dict[str, Any]] = []
        tracked_changes = changes if len(changes) <= OPERATION_FILES_LIMIT else []
        for target, _change in tracked_changes:
            old = before.files.get(target)
            new = after.files.get(target)
            before_exists = old is not None
            before_text = old.text if old is not None and old.text_available else None
            after_text_available = new is None or new.text_available
            reversible = (not before_exists or before_text is not None) and after_text_available
            reason = ""
            if not reversible:
                reason = "文件为二进制、过大或超出文本快照预算"
            files.append(
                {
                    "path": target,
                    "before_exists": before_exists,
                    "before_text": before_text,
                    "after": self._snapshot_identity(new),
                    "reversible": reversible,
                    "reason": reason,
                }
            )

        operation = {
            "id": self._next_operation_id,
            "turn": pending["turn"],
            "task": pending["task"],
            "status": "active",
            "tracking_complete": (
                before.complete
                and after.complete
                and len(changes) <= OPERATION_FILES_LIMIT
            ),
            "total_file_count": len(changes),
            "files": files,
        }
        self._next_operation_id += 1
        self._operations.append(operation)
        self._trim_operation_history()
        return self._public_operation(operation)

    def operation_history(self) -> list[dict[str, Any]]:
        """Return turn-level change records with their current undo eligibility."""
        return [self._public_operation(item) for item in self._operations]

    def undo_operation(self, operation_id: int) -> dict[str, Any]:
        """Undo one completed turn when every affected file still matches it."""
        if not isinstance(operation_id, int) or isinstance(operation_id, bool):
            raise ToolError("operation id must be an integer")
        operation = next(
            (item for item in self._operations if item.get("id") == operation_id),
            None,
        )
        if operation is None:
            raise ToolError(f"turn operation not found: {operation_id}")
        if operation.get("status") != "active":
            raise ToolError("this turn has already been undone")
        files = operation.get("files", [])
        if not operation.get("tracking_complete") or not all(
            isinstance(item, dict) and item.get("reversible") is True for item in files
        ):
            raise ToolError("this turn cannot be safely undone from the saved snapshots")

        drifted = [
            self.display(item["path"])
            for item in files
            if self._file_identity(item["path"]) != item["after"]
        ]
        if drifted:
            raise ToolError(
                "files changed after this turn; undo the newer change first: "
                + ", ".join(drifted[:5])
            )

        current_state: dict[Path, tuple[bool, str | None]] = {}
        for item in files:
            target = item["path"]
            exists = target.exists()
            current_state[target] = (
                exists,
                target.read_text(encoding="utf-8-sig") if exists else None,
            )

        applied: list[Path] = []
        try:
            for item in files:
                target = item["path"]
                self._restore_text_state(
                    target,
                    exists=bool(item["before_exists"]),
                    content=item.get("before_text"),
                )
                applied.append(target)
        except (OSError, ToolError) as exc:
            rollback_error: Exception | None = None
            for target in reversed(applied):
                existed, content = current_state[target]
                try:
                    self._restore_text_state(target, exists=existed, content=content)
                except (OSError, ToolError) as rollback_exc:
                    rollback_error = rollback_exc
                    break
            if rollback_error is not None:
                raise ToolError(
                    f"turn undo failed and rollback was incomplete: {rollback_error}"
                ) from exc
            raise ToolError(f"turn undo failed; no changes were kept: {exc}") from exc

        for item in files:
            target = item["path"]
            self._tracked_current[target] = self._file_identity(target)
        operation["status"] = "undone"
        result = self._public_operation(operation)
        result["remaining"] = self.show_diff().get("files", [])
        return result

    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ToolError("path must be a non-empty string")
        supplied = Path(relative_path)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        try:
            resolved = candidate.resolve(strict=must_exist)
        except OSError as exc:
            raise ToolError(f"cannot resolve path: {relative_path}: {exc}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {relative_path}") from exc
        return resolved

    def display(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        data = target.read_bytes()
        if b"\x00" in data[:4096]:
            raise ToolError(f"binary file is not readable as text: {path}")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {path}") from exc
        lines = text.splitlines()
        if start_line < 1:
            raise ToolError("start_line must be at least 1")
        final = len(lines) if end_line is None else end_line
        if final < start_line:
            raise ToolError("end_line must be greater than or equal to start_line")
        selected = lines[start_line - 1 : final]
        numbered = "\n".join(
            f"{number:>6} | {line}"
            for number, line in enumerate(selected, start=start_line)
        )
        return {
            "path": self.display(target),
            "start_line": start_line,
            "end_line": min(final, len(lines)),
            "total_lines": len(lines),
            "content": self._truncate(numbered),
        }

    def list_files(self, path: str = ".", depth: int = 3, max_entries: int = 300) -> dict[str, Any]:
        target = self.resolve(path, must_exist=True)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        if not 0 <= depth <= 32:
            raise ToolError("depth must be between 0 and 32")
        if not 1 <= max_entries <= 5000:
            raise ToolError("max_entries must be between 1 and 5000")

        base_depth = len(target.parts)
        entries: list[str] = []
        limit = max_entries + 1
        for current, dirnames, filenames in os.walk(target):
            current_path = Path(current)
            level = len(current_path.parts) - base_depth
            dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_IGNORES)
            if level >= depth:
                dirnames[:] = []
            for dirname in dirnames:
                entries.append(self.display(current_path / dirname) + "/")
                if len(entries) >= limit:
                    break
            if len(entries) >= limit:
                break
            for filename in sorted(filenames):
                entries.append(self.display(current_path / filename))
                if len(entries) >= limit:
                    break
            if len(entries) >= limit:
                break
        limited = entries[:max_entries]
        return {
            "path": self.display(target),
            "entries": limited,
            "truncated": len(entries) > max_entries,
        }

    def preview_file(self, path: str, *, max_bytes: int = 500_000) -> dict[str, Any]:
        """Return a bounded UTF-8 file preview for the local Web UI."""
        if not 1 <= max_bytes <= 2_000_000:
            raise ToolError("preview byte limit must be between 1 and 2000000")
        target = self.resolve(path, must_exist=True)
        if not self.preview_allowed(self.display(target)):
            raise ToolError("sensitive files are hidden from Web preview")
        if target.is_symlink() or not target.is_file():
            raise ToolError(f"not a regular file: {path}")
        try:
            size = target.stat().st_size
            with target.open("rb") as stream:
                data = stream.read(max_bytes + 1)
        except OSError as exc:
            raise ToolError(f"could not read {path}: {exc}") from exc
        if b"\x00" in data[:4096]:
            raise ToolError(f"binary file cannot be previewed: {path}")
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {path}") from exc
        changed_files = self.show_diff().get("files", [])
        return {
            "path": self.display(target),
            "content": content,
            "size": size,
            "lines": content.count("\n") + (1 if content else 0),
            "truncated": truncated,
            "changed": self.display(target) in changed_files,
        }

    @staticmethod
    def preview_allowed(path: str) -> bool:
        """Return whether a workspace entry may be exposed in the Web file browser."""
        normalized = path.rstrip("/")
        parts = [part.casefold() for part in Path(normalized).parts]
        if any(part in PREVIEW_DENIED_DIRECTORIES for part in parts):
            return False
        if not parts:
            return True
        name = parts[-1]
        if name == ".env.example":
            return True
        if name in PREVIEW_DENIED_FILES or name.startswith(".env."):
            return False
        return Path(name).suffix.casefold() not in PREVIEW_DENIED_SUFFIXES

    def search_text(
        self,
        query: str,
        path: str = ".",
        file_glob: str = "*",
        max_results: int = 100,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        if not query:
            raise ToolError("query must not be empty")
        if not 1 <= max_results <= 1000:
            raise ToolError("max_results must be between 1 and 1000")
        target = self.resolve(path, must_exist=True)
        files = [target] if target.is_file() else target.rglob(file_glob)
        needle = query if case_sensitive else query.casefold()
        matches: list[dict[str, Any]] = []

        for file_path in files:
            if len(matches) >= max_results:
                break
            if not file_path.is_file() or any(part in DEFAULT_IGNORES for part in file_path.parts):
                continue
            try:
                data = file_path.read_bytes()
                if b"\x00" in data[:4096] or len(data) > 2_000_000:
                    continue
                text = data.decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append(
                        {
                            "path": self.display(file_path),
                            "line": number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= max_results:
                        break
        return {"query": query, "matches": matches, "truncated": len(matches) >= max_results}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        self._remember_original(target)
        self._atomic_write(target, content)
        after_identity = self._file_identity(target)
        self._tracked_current[target] = after_identity
        return {
            "path": self.display(target),
            "action": "updated" if existed else "created",
            "bytes": len(content.encode("utf-8")),
        }

    def replace_text(self, path: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise ToolError("old text must not be empty")
        if count < 1:
            raise ToolError("count must be at least 1")
        target = self.resolve(path, must_exist=True)
        try:
            text = target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {path}") from exc
        occurrences = text.count(old)
        if occurrences == 0:
            raise ToolError("old text was not found; re-read the file before editing")
        if occurrences < count:
            raise ToolError(f"requested {count} replacements but found only {occurrences}")
        updated = text.replace(old, new, count)
        self._remember_original(target, text=text)
        self._atomic_write(target, updated)
        after_identity = self._file_identity(target)
        self._tracked_current[target] = after_identity
        return {
            "path": self.display(target),
            "replacements": count,
            "remaining_matches": occurrences - count,
        }

    def show_diff(self, path: str | None = None, context_lines: int = 3) -> dict[str, Any]:
        if not 0 <= context_lines <= 20:
            raise ToolError("context_lines must be between 0 and 20")
        if path is None:
            tracked = set(self._original_text) | set(self._unavailable_diffs)
            targets = sorted(tracked, key=lambda item: self.display(item))
        else:
            target = self.resolve(path)
            targets = [target] if target in self._original_text or target in self._unavailable_diffs else []

        changed_files: list[str] = []
        diff_parts: list[str] = []
        for target in targets:
            if target not in self._original_text:
                display_path = self.display(target)
                changed_files.append(display_path)
                reason = self._unavailable_diffs[target]
                diff_parts.append(
                    f"--- a/{display_path}\n+++ b/{display_path}\n"
                    f"[diff unavailable: {reason}]\n"
                )
                continue
            original = self._original_text[target]
            try:
                current = target.read_text(encoding="utf-8-sig") if target.exists() else None
            except UnicodeDecodeError as exc:
                raise ToolError(f"file is not UTF-8 text: {self.display(target)}") from exc
            if original == current:
                continue
            display_path = self.display(target)
            changed_files.append(display_path)
            before = [] if original is None else original.splitlines(keepends=True)
            after = [] if current is None else current.splitlines(keepends=True)
            diff_parts.extend(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile="/dev/null" if original is None else f"a/{display_path}",
                    tofile="/dev/null" if current is None else f"b/{display_path}",
                    n=context_lines,
                )
            )

        full_diff = "".join(diff_parts)
        return {
            "files": changed_files,
            "diff": self._truncate(full_diff),
            "truncated": len(full_diff) > self.max_output_chars,
        }

    def preview_diff(self, path: str | None = None) -> dict[str, Any]:
        """Return a Web-safe diff that omits credential-bearing paths."""
        if path is not None:
            if not self.preview_allowed(path):
                raise ToolError("sensitive files are hidden from Web diff")
            return self.show_diff(path)

        complete = self.show_diff()
        files = complete.get("files", [])
        visible = [
            item
            for item in files
            if isinstance(item, str) and self.preview_allowed(item)
        ]
        hidden_count = len(files) - len(visible) if isinstance(files, list) else 0
        parts: list[str] = []
        truncated = False
        for item in visible:
            item_diff = self.show_diff(item)
            parts.append(str(item_diff.get("diff") or ""))
            truncated = truncated or bool(item_diff.get("truncated"))
        combined = "".join(parts)
        return {
            "files": visible,
            "diff": self._truncate(combined),
            "truncated": truncated or len(combined) > self.max_output_chars,
            "hidden_files": hidden_count,
        }

    def revert_changes(self, path: str | None = None) -> dict[str, Any]:
        """Restore one or all tracked text files to their session baseline."""
        changed = self.show_diff().get("files", [])
        changed_set = {item for item in changed if isinstance(item, str)}
        if path is not None:
            target = self.resolve(path)
            display_path = self.display(target)
            if display_path not in changed_set:
                raise ToolError(f"file has no tracked changes: {display_path}")
            targets = [target]
        else:
            targets = [self.resolve(item) for item in sorted(changed_set)]
        if not targets:
            return {"reverted": [], "remaining": sorted(changed_set)}

        unavailable = [
            self.display(target) for target in targets if target in self._unavailable_diffs
        ]
        if unavailable:
            joined = ", ".join(unavailable[:5])
            raise ToolError(f"cannot safely restore files without a text baseline: {joined}")

        drifted = [
            self.display(target)
            for target in targets
            if target in self._tracked_current
            and self._file_identity(target) != self._tracked_current[target]
        ]
        if drifted:
            joined = ", ".join(drifted[:5])
            raise ToolError(
                f"refusing to overwrite files changed outside Rivet: {joined}"
            )

        current_state: dict[Path, tuple[bool, str | None]] = {}
        for target in targets:
            exists = target.exists()
            current_state[target] = (
                exists,
                target.read_text(encoding="utf-8-sig") if exists else None,
            )

        reverted: list[str] = []
        applied: list[Path] = []
        try:
            for target in targets:
                if target not in self._original_text:
                    raise ToolError(
                        f"file has no restorable baseline: {self.display(target)}"
                    )
                original = self._original_text[target]
                if original is None:
                    if target.exists():
                        if target.is_symlink() or not target.is_file():
                            raise ToolError(
                                f"refusing to remove non-file path: {self.display(target)}"
                            )
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._atomic_write(target, original)
                applied.append(target)
                reverted.append(self.display(target))
        except (OSError, ToolError) as exc:
            rollback_error: Exception | None = None
            for restored in reversed(applied):
                existed, content = current_state[restored]
                try:
                    if existed:
                        self._atomic_write(restored, content or "")
                    elif restored.exists():
                        restored.unlink()
                except (OSError, ToolError) as rollback_exc:
                    rollback_error = rollback_exc
                    break
            if rollback_error is not None:
                raise ToolError(
                    f"restore failed and rollback was incomplete: {rollback_error}"
                ) from exc
            raise ToolError(f"restore failed; no changes were kept: {exc}") from exc

        for target in targets:
            self._original_text.pop(target, None)
            self._unavailable_diffs.pop(target, None)
            self._tracked_current.pop(target, None)

        remaining = self.show_diff().get("files", [])
        return {
            "reverted": reverted,
            "remaining": remaining if isinstance(remaining, list) else [],
        }

    @staticmethod
    def command_review_reason(command: str) -> str | None:
        for reason, pattern in REVIEW_COMMANDS:
            if pattern.search(command):
                return reason
        return None

    @staticmethod
    def is_verification_command(command: str) -> bool:
        return any(pattern.search(command) for pattern in VERIFICATION_COMMANDS)

    def run_command(
        self, command: str, timeout: int, purpose: str = "auto"
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        if len(command) > 10_000:
            raise ToolError("command must not exceed 10000 characters")
        if "\n" in command or "\r" in command:
            raise ToolError("multi-line commands are not allowed")
        if any(pattern.search(command) for pattern in RISKY_COMMANDS):
            raise ToolError("command blocked by safety policy; perform a narrower operation")
        if not 1 <= timeout <= 600:
            raise ToolError("timeout must be between 1 and 600 seconds")
        if purpose not in {"auto", "inspect", "verify"}:
            raise ToolError("purpose must be auto, inspect, or verify")

        environment = {
            key: value
            for key, value in os.environ.items()
            if not re.search(
                r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)|^(?:RIVET|OPENAI)_",
                key,
                re.IGNORECASE,
            )
        }
        verification = purpose == "verify" or (
            purpose == "auto" and self.is_verification_command(command)
        )
        before = self._capture_snapshot(capture_text=True)
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        started_at = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=self.root,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        windows_job = self._create_windows_job(process)
        deadline = time.monotonic() + timeout
        cancelled = False
        timed_out = False
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                cancelled = True
                self._terminate_process_tree(process, windows_job)
                stdout, stderr = process.communicate()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self._terminate_process_tree(process, windows_job)
                stdout, stderr = process.communicate()
                break
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                cancelled = True
                self._terminate_process_tree(process, windows_job)
                stdout, stderr = process.communicate()
                break

        if timed_out or cancelled:
            command_result: dict[str, Any] = {
                "command": command,
                "exit_code": None,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "verification": verification,
                "purpose": purpose,
                "stdout": self._truncate(self._output_text(stdout)),
                "stderr": self._truncate(self._output_text(stderr)),
            }
        else:
            command_result = {
                "command": command,
                "exit_code": process.returncode,
                "timed_out": False,
                "cancelled": False,
                "verification": verification,
                "purpose": purpose,
                "stdout": self._truncate(stdout),
                "stderr": self._truncate(stderr),
            }
        self._close_windows_job(windows_job)

        try:
            after = self._capture_snapshot(capture_text=False)
            changes = self._record_command_changes(before, after)
        except KeyboardInterrupt:
            command_result["cancelled"] = True
            after = self._capture_snapshot(capture_text=False)
            changes = self._record_command_changes(before, after)
        reported_changes = changes[:SNAPSHOT_CHANGE_REPORT_LIMIT]
        changes_truncated = len(changes) > len(reported_changes)
        command_result["file_changes"] = reported_changes
        command_result["file_change_count"] = len(changes)
        command_result["file_changes_truncated"] = changes_truncated
        command_result["tracking_complete"] = (
            before.complete and after.complete and not changes_truncated
        )
        command_result["duration_ms"] = max(
            0, round((time.monotonic() - started_at) * 1000)
        )
        return command_result

    @staticmethod
    def _create_windows_job(process: subprocess.Popen[str]) -> int | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = 0x00002000
            configured = kernel32.SetInformationJobObject(
                job,
                9,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            assigned = configured and kernel32.AssignProcessToJobObject(
                job, wintypes.HANDLE(int(process._handle))
            )
            if not assigned:
                kernel32.CloseHandle(job)
                return None
            return int(job)
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    @staticmethod
    def _close_windows_job(job: int | None) -> None:
        if os.name != "nt" or job is None:
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(job))
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen[str], windows_job: int | None = None
    ) -> None:
        """Best-effort termination of the shell and descendants on Windows and POSIX."""
        if os.name == "nt":
            if windows_job is not None:
                try:
                    import ctypes
                    from ctypes import wintypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
                    kernel32.TerminateJobObject.restype = wintypes.BOOL
                    kernel32.TerminateJobObject(wintypes.HANDLE(windows_job), 1)
                except (AttributeError, OSError, TypeError, ValueError):
                    pass
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=0.75)
            except (OSError, subprocess.SubprocessError, AttributeError):
                pass
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _capture_snapshot(self, *, capture_text: bool) -> WorkspaceSnapshot:
        files: dict[Path, FileSnapshot] = {}
        text_budget = SNAPSHOT_TEXT_BUDGET_BYTES
        complete = True

        for current, dirnames, filenames in os.walk(self.root):
            current_path = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in DEFAULT_IGNORES and not (current_path / name).is_symlink()
            )
            for filename in sorted(filenames):
                if filename in SNAPSHOT_IGNORED_FILES:
                    continue
                if len(files) >= SNAPSHOT_MAX_FILES:
                    return WorkspaceSnapshot(files, False)
                target = current_path / filename
                if target.is_symlink() or not target.is_file():
                    continue
                try:
                    size = target.stat().st_size
                    data: bytes | None = None
                    if size <= SNAPSHOT_TEXT_FILE_BYTES:
                        data = target.read_bytes()
                        digest = hashlib.sha256(data).hexdigest()
                    else:
                        digest_hash = hashlib.sha256()
                        with target.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                digest_hash.update(chunk)
                        digest = digest_hash.hexdigest()

                    text: str | None = None
                    text_available = False
                    if (
                        capture_text
                        and data is not None
                        and len(data) <= text_budget
                        and b"\x00" not in data[:4096]
                    ):
                        try:
                            text = data.decode("utf-8-sig")
                            text_available = True
                            text_budget -= len(data)
                        except UnicodeDecodeError:
                            pass
                    files[target] = FileSnapshot(digest, size, text, text_available)
                except OSError:
                    complete = False
        return WorkspaceSnapshot(files, complete)

    def _record_command_changes(
        self, before: WorkspaceSnapshot, after: WorkspaceSnapshot
    ) -> list[dict[str, Any]]:
        changes = self._snapshot_changes(before, after)

        payload: list[dict[str, Any]] = []
        for target, change in changes:
            self._remember_command_baseline(target, change, before.files.get(target))
            self._tracked_current[target] = self._file_identity(target)
            payload.append(
                {
                    "path": self.display(target),
                    "change": change,
                    "diff_available": target in self._original_text,
                }
            )
        return payload

    def _snapshot_changes(
        self, before: WorkspaceSnapshot, after: WorkspaceSnapshot
    ) -> list[tuple[Path, str]]:
        changes: list[tuple[Path, str]] = []
        all_paths = sorted(
            set(before.files) | set(after.files), key=lambda item: self.display(item)
        )
        for target in all_paths:
            old = before.files.get(target)
            new = after.files.get(target)
            if old is None:
                changes.append((target, "created"))
            elif new is None:
                changes.append((target, "deleted"))
            elif old.digest != new.digest:
                changes.append((target, "modified"))
        return changes

    def _remember_command_baseline(
        self, target: Path, change: str, old: FileSnapshot | None
    ) -> None:
        if target in self._original_text or target in self._unavailable_diffs:
            return
        if change == "created":
            try:
                data = target.read_bytes()
                if (
                    len(data) <= SNAPSHOT_TEXT_FILE_BYTES
                    and b"\x00" not in data[:4096]
                ):
                    data.decode("utf-8-sig")
                    self._original_text[target] = None
                    return
            except (OSError, UnicodeDecodeError):
                pass
            self._unavailable_diffs[target] = "created file is binary, oversized, or unreadable"
            return

        if old is not None and old.text_available:
            self._original_text[target] = old.text
        else:
            self._unavailable_diffs[target] = (
                f"original content for {change} file was binary, oversized, or outside snapshot budget"
            )

    def _remember_original(self, target: Path, *, text: str | None = None) -> None:
        if target in self._original_text:
            return
        if not target.exists():
            self._original_text[target] = None
            return
        if not target.is_file():
            raise ToolError(f"not a file: {self.display(target)}")
        try:
            original = text if text is not None else target.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not UTF-8 text: {self.display(target)}") from exc
        self._original_text[target] = original

    def _public_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        files = operation.get("files", [])
        paths = [self.display(item["path"]) for item in files]
        status = str(operation.get("status") or "active")
        can_undo = status == "active" and bool(operation.get("tracking_complete"))
        blocked_reason = ""
        if status != "active":
            can_undo = False
            blocked_reason = "本轮修改已经撤销"
        elif not operation.get("tracking_complete"):
            can_undo = False
            blocked_reason = "工作区过大，无法确认完整修改范围"
        elif not all(item.get("reversible") is True for item in files):
            can_undo = False
            blocked_reason = "包含无法安全恢复的二进制或超大文件"
        elif any(self._file_identity(item["path"]) != item["after"] for item in files):
            can_undo = False
            blocked_reason = "相关文件后来又发生了变化"
        return {
            "id": operation.get("id"),
            "turn": operation.get("turn"),
            "task": str(operation.get("task") or ""),
            "status": status,
            "files": paths,
            "file_count": int(operation.get("total_file_count", len(paths))),
            "can_undo": can_undo,
            "blocked_reason": blocked_reason,
        }

    def _export_operations(self) -> list[dict[str, Any]]:
        exported: list[dict[str, Any]] = []
        for operation in self._operations:
            exported.append(
                {
                    "id": operation["id"],
                    "turn": operation["turn"],
                    "task": operation["task"],
                    "status": operation["status"],
                    "tracking_complete": operation["tracking_complete"],
                    "total_file_count": operation.get(
                        "total_file_count", len(operation["files"])
                    ),
                    "files": [
                        {
                            "path": self.display(item["path"]),
                            "before_exists": item["before_exists"],
                            "before_text": item["before_text"],
                            "after": item["after"],
                            "reversible": item["reversible"],
                            "reason": item["reason"],
                        }
                        for item in operation["files"]
                    ],
                }
            )
        return exported

    def _restore_operations(
        self, payload: Any, next_operation_id: Any
    ) -> tuple[list[dict[str, Any]], int]:
        if not isinstance(payload, list) or len(payload) > OPERATION_HISTORY_LIMIT:
            raise ToolError("saved turn operations must be a bounded list")
        restored: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        file_count = 0
        text_bytes = 0
        for raw in payload:
            if not isinstance(raw, dict):
                raise ToolError("saved turn operation is invalid")
            operation_id = raw.get("id")
            turn = raw.get("turn")
            task = raw.get("task")
            status = raw.get("status")
            tracking = raw.get("tracking_complete")
            raw_files = raw.get("files")
            total_file_count = raw.get("total_file_count")
            if (
                not isinstance(operation_id, int)
                or isinstance(operation_id, bool)
                or operation_id < 1
                or operation_id in seen_ids
                or not isinstance(turn, int)
                or isinstance(turn, bool)
                or turn < 1
                or not isinstance(task, str)
                or len(task) > 240
                or status not in {"active", "undone", "reverted"}
                or not isinstance(tracking, bool)
                or not isinstance(raw_files, list)
            ):
                raise ToolError("saved turn operation metadata is invalid")
            if total_file_count is None:
                total_file_count = len(raw_files)
            if (
                not isinstance(total_file_count, int)
                or isinstance(total_file_count, bool)
                or total_file_count < len(raw_files)
            ):
                raise ToolError("saved turn operation file count is invalid")
            seen_ids.add(operation_id)
            files: list[dict[str, Any]] = []
            for raw_file in raw_files:
                file_count += 1
                if file_count > OPERATION_FILES_LIMIT or not isinstance(raw_file, dict):
                    raise ToolError("saved turn operations contain too many files")
                path = raw_file.get("path")
                before_exists = raw_file.get("before_exists")
                before_text = raw_file.get("before_text")
                reversible = raw_file.get("reversible")
                reason = raw_file.get("reason", "")
                if (
                    not isinstance(path, str)
                    or not isinstance(before_exists, bool)
                    or (before_text is not None and not isinstance(before_text, str))
                    or not isinstance(reversible, bool)
                    or not isinstance(reason, str)
                ):
                    raise ToolError("saved turn operation file is invalid")
                if not before_exists and before_text is not None:
                    raise ToolError("saved new-file checkpoint has invalid content")
                if isinstance(before_text, str):
                    text_bytes += len(before_text.encode("utf-8"))
                    if text_bytes > SNAPSHOT_TEXT_BUDGET_BYTES:
                        raise ToolError("saved turn operation text exceeds the restore budget")
                files.append(
                    {
                        "path": self.resolve(path),
                        "before_exists": before_exists,
                        "before_text": before_text,
                        "after": self._validate_file_identity(raw_file.get("after")),
                        "reversible": reversible,
                        "reason": reason,
                    }
                )
            restored.append(
                {
                    "id": operation_id,
                    "turn": turn,
                    "task": task,
                    "status": status,
                    "tracking_complete": tracking,
                    "total_file_count": total_file_count,
                    "files": files,
                }
            )
        maximum = max(seen_ids, default=0)
        if next_operation_id is None:
            next_id = maximum + 1
        elif (
            not isinstance(next_operation_id, int)
            or isinstance(next_operation_id, bool)
            or next_operation_id <= maximum
        ):
            raise ToolError("saved next turn operation id is invalid")
        else:
            next_id = next_operation_id
        return restored, next_id

    def _trim_operation_history(self) -> None:
        """Keep persisted turn checkpoints inside the same restore budgets."""
        while self._operations:
            file_count = sum(len(item.get("files", [])) for item in self._operations)
            text_bytes = sum(
                len(file.get("before_text", "").encode("utf-8"))
                for item in self._operations
                for file in item.get("files", [])
                if isinstance(file.get("before_text"), str)
            )
            if (
                len(self._operations) <= OPERATION_HISTORY_LIMIT
                and file_count <= OPERATION_FILES_LIMIT
                and text_bytes <= SNAPSHOT_TEXT_BUDGET_BYTES
            ):
                return
            self._operations.pop(0)

    @staticmethod
    def _snapshot_identity(snapshot: FileSnapshot | None) -> dict[str, Any]:
        if snapshot is None:
            return {"kind": "missing", "sha256": None}
        return {"kind": "file", "sha256": snapshot.digest}

    def _restore_text_state(
        self, target: Path, *, exists: bool, content: str | None
    ) -> None:
        if exists:
            if content is None:
                raise ToolError(f"missing text snapshot for {self.display(target)}")
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(target, content)
        elif target.exists():
            if target.is_symlink() or not target.is_file():
                raise ToolError(
                    f"refusing to remove non-file path: {self.display(target)}"
                )
            target.unlink()

    @staticmethod
    def _validate_file_identity(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ToolError("saved file identity must be an object")
        kind = value.get("kind")
        if kind not in {"missing", "file", "other", "unreadable"}:
            raise ToolError("saved file identity has an invalid kind")
        digest = value.get("sha256")
        if kind == "file":
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ToolError("saved file identity has an invalid digest")
            return {"kind": kind, "sha256": digest}
        if digest is not None:
            raise ToolError("saved non-file identity must not contain a digest")
        return {"kind": kind, "sha256": None}

    @staticmethod
    def _file_identity(target: Path) -> dict[str, Any]:
        try:
            if not target.exists():
                return {"kind": "missing", "sha256": None}
            if target.is_symlink() or not target.is_file():
                return {"kind": "other", "sha256": None}
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return {"kind": "file", "sha256": digest.hexdigest()}
        except OSError:
            return {"kind": "unreadable", "sha256": None}

    @staticmethod
    def _output_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def _atomic_write(self, target: Path, content: str) -> None:
        temporary_name: str | None = None
        existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=target.parent, delete=False
            ) as stream:
                stream.write(content)
                temporary_name = stream.name
            if existing_mode is not None:
                os.chmod(temporary_name, existing_mode)
            os.replace(temporary_name, target)
        except OSError as exc:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise ToolError(f"failed to write {self.display(target)}: {exc}") from exc

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        head = self.max_output_chars * 3 // 4
        tail = self.max_output_chars - head
        omitted = len(text) - self.max_output_chars
        return f"{text[:head]}\n...[truncated {omitted} characters]...\n{text[-tail:]}"
