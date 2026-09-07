from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .agent import Agent, AgentResult
from .config import Config
from .errors import ConfigurationError, RivetError
from .provider import create_model_client
from .session import SessionStore
from .types import JsonObject, ModelClient


MAX_REQUEST_BYTES = 1_000_000
APPROVAL_TIMEOUT_SECONDS = 300
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class WebRuntime:
    """Own one browser session while keeping the Agent UI-independent."""

    def __init__(self, config: Config, client: ModelClient) -> None:
        self.config = config
        self.sessions = SessionStore(config.workspace, model=config.model)
        self.session_path: Path | None = None
        self.token = secrets.token_urlsafe(32)
        self.turn_lock = threading.Lock()
        self.writer_lock = threading.Lock()
        self.approval_condition = threading.Condition()
        self.pending_approval: JsonObject | None = None
        self.writer: BinaryIO | None = None
        self.agent = Agent(
            config,
            client,
            event_handler=self._agent_event,
            approver=self._approve,
            client_factory=lambda: create_model_client(config),
        )

    def snapshot(self) -> JsonObject:
        status = self._web_status()
        diff = self.agent.tools.workspace.preview_diff()
        return {
            "config": {
                "model": self.config.model,
                "protocol": self.config.protocol,
                "workspace": str(self.config.workspace),
                "workspace_name": self.config.workspace.name,
                "approval": self.config.approval_mode,
                "max_steps": self.config.max_steps,
                "max_context_chars": self.config.max_context_chars,
                "command_timeout": self.config.command_timeout,
                "subagent_max_steps": self.config.subagent_max_steps,
                "max_subagents_per_turn": min(
                    2, self.config.max_subagents_per_turn
                ),
                "subagent_parallelism": min(
                    2,
                    self.config.subagent_parallelism,
                    self.config.max_subagents_per_turn,
                ),
            },
            "status": status,
            "plan": self.agent.plan_snapshot(),
            "diff": self._redact_payload(diff),
            "sessions": [
                self._session_payload(item)
                for item in self.sessions.list_sessions(limit=100)
            ],
            "conversation": self._visible_conversation(),
            "busy": self.turn_lock.locked(),
            "session_id": self.session_path.stem if self.session_path else None,
        }

    def run_turn(self, task: str, writer: BinaryIO) -> None:
        self._run_stream(task, writer)

    def recover_turn(self, mode: str, writer: BinaryIO) -> None:
        if mode not in {"continue", "retry"}:
            raise ValueError("recovery mode must be continue or retry")
        self._run_stream("", writer, recovery_mode=mode)

    def _run_stream(
        self,
        task: str,
        writer: BinaryIO,
        *,
        recovery_mode: str | None = None,
    ) -> None:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("Rivet is already processing another turn")
        try:
            self.writer = writer
            if recovery_mode is not None:
                recovery = self.agent.recovery_snapshot()
                if recovery.get("available") is not True:
                    self._write_record(
                        {
                            "type": "recovery_error",
                            "message": "当前没有可以恢复的失败任务",
                            "snapshot": self.snapshot(),
                        }
                    )
                    return
                original_task = str(recovery.get("task") or "").strip()
                if recovery_mode == "retry":
                    try:
                        restored = self.agent.prepare_retry(
                            recovery.get("retry_operation_id")
                            if isinstance(recovery.get("retry_operation_id"), int)
                            else None
                        )
                    except RivetError as exc:
                        self._write_record(
                            {
                                "type": "recovery_error",
                                "message": str(exc),
                                "snapshot": self.snapshot(),
                            }
                        )
                        return
                    task = (
                        f'重新尝试上一轮任务：“{original_task}”。'
                        "失败轮次产生的文件修改已安全恢复；请重新分析，完成任务并验证结果。"
                    )
                    self._emit(
                        "recovery_started",
                        {
                            "mode": "retry",
                            "restored_files": restored.get("files", []),
                        },
                    )
                else:
                    task = (
                        f'继续完成上一轮未完成的任务：“{original_task}”。'
                        "保留当前已有的有效修改，先检查现状，再完成剩余工作并验证结果。"
                    )
                    self._emit(
                        "recovery_started",
                        {"mode": "continue", "restored_files": []},
                    )
            self._emit("turn_started", {"task": task})
            try:
                result = self.agent.run(task)
            except RivetError as exc:
                try:
                    result = self.agent.record_failure(
                        f"任务因运行错误而停止：{exc}", "runtime_error"
                    )
                except RivetError:
                    self._write_record({"type": "fatal_error", "message": str(exc)})
                    return
            except Exception:
                print("Unexpected error while processing a web turn", file=sys.stderr)
                try:
                    result = self.agent.record_failure(
                        "任务因本地运行错误而停止，请检查配置后重试。",
                        "runtime_error",
                    )
                except RivetError:
                    self._write_record(
                        {"type": "fatal_error", "message": "Unexpected local server error"}
                    )
                    return
            self._finish_web_turn(result)
        finally:
            with self.approval_condition:
                if self.pending_approval is not None:
                    self.pending_approval["approved"] = False
                    self.approval_condition.notify_all()
                self.pending_approval = None
            self.writer = None
            self.turn_lock.release()

    def _finish_web_turn(self, result: AgentResult) -> None:
        first_save = self.session_path is None
        try:
            self.session_path = self.sessions.save(
                self.agent, result, target=self.session_path
            )
        except RivetError as exc:
            self._emit("session_error", {"message": str(exc)})
        if first_save and self.session_path is not None:
            self._emit("session_saved", {"id": self.session_path.stem})
        self._write_record(
            {
                "type": "turn_complete",
                "result": self._result_payload(result),
                "snapshot": self.snapshot(),
            }
        )

    def new_session(self) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            self.agent.reset()
            self.session_path = None
            return self.snapshot()
        finally:
            self.turn_lock.release()

    def resume_session(self, reference: str | None) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            loaded = self.sessions.load(reference or None)
            drifted = self.agent.restore_session_state(loaded.agent_state)
            self.session_path = loaded.summary.path
            snapshot = self.snapshot()
            snapshot["resume"] = {
                "id": loaded.summary.session_id,
                "drifted": drifted,
                "saved_model": loaded.summary.model,
            }
            return snapshot
        finally:
            self.turn_lock.release()

    def list_workspace_files(self) -> JsonObject:
        workspace = self.agent.tools.workspace
        result = workspace.list_files(".", depth=32, max_entries=5000)
        entries = result.get("entries", [])
        if isinstance(entries, list):
            result["entries"] = [
                item
                for item in entries
                if isinstance(item, str) and workspace.preview_allowed(item)
            ]
        safe_diff = workspace.preview_diff()
        result["changed_files"] = safe_diff.get("files", [])
        result["hidden_files"] = safe_diff.get("hidden_files", 0)
        return result

    def preview_workspace_file(self, path: str) -> JsonObject:
        return self.agent.tools.workspace.preview_file(path)

    def preview_diff(self, path: str | None = None) -> JsonObject:
        return self._redact_payload(self.agent.tools.workspace.preview_diff(path))

    def rename_session(self, reference: str, title: str) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            self.sessions.rename(reference, title)
            return self.snapshot()
        finally:
            self.turn_lock.release()

    def pin_session(self, reference: str, pinned: bool) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            self.sessions.set_pinned(reference, pinned)
            return self.snapshot()
        finally:
            self.turn_lock.release()

    def delete_session(self, reference: str) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            deleted = self.sessions.delete(reference)
            if self.session_path is not None and self.session_path.stem == deleted:
                self.agent.reset()
                self.session_path = None
            snapshot = self.snapshot()
            snapshot["deleted_session"] = deleted
            return snapshot
        finally:
            self.turn_lock.release()

    def revert_changes(self, path: str | None) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            workspace = self.agent.tools.workspace
            if path is not None and not workspace.preview_allowed(path):
                raise ValueError("sensitive files cannot be restored from the Web UI")
            if path is None:
                changed = self.agent.show_diff().get("files", [])
                hidden = [
                    item
                    for item in changed
                    if isinstance(item, str) and not workspace.preview_allowed(item)
                ]
                if hidden:
                    raise ValueError(
                        "review sensitive file changes in the terminal before restoring"
                    )
            result = self.agent.revert_changes(path)
            if self.session_path is not None:
                self.sessions.update_agent_state(self.session_path, self.agent)
            snapshot = self.snapshot()
            snapshot["revert"] = result
            return snapshot
        finally:
            self.turn_lock.release()

    def undo_operation(self, operation_id: int) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            result = self.agent.undo_operation(operation_id)
            if self.session_path is not None:
                self.sessions.update_agent_state(self.session_path, self.agent)
            snapshot = self.snapshot()
            snapshot["undo"] = result
            return snapshot
        finally:
            self.turn_lock.release()

    def compact_context(self) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            report = self.agent.compact_context()
            if report.get("compacted") is True and self.session_path is not None:
                self.sessions.update_agent_state(self.session_path, self.agent)
            snapshot = self.snapshot()
            snapshot["compaction"] = report
            return snapshot
        finally:
            self.turn_lock.release()

    def set_approval_mode(self, mode: str) -> JsonObject:
        if not self.turn_lock.acquire(blocking=False):
            raise RuntimeError("wait for the current turn to finish")
        try:
            result = self.agent.set_approval_mode(mode)
            self.config = self.agent.config
            snapshot = self.snapshot()
            snapshot["approval_update"] = result
            return snapshot
        finally:
            self.turn_lock.release()

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        with self.approval_condition:
            pending = self.pending_approval
            if pending is None or pending.get("id") != approval_id:
                return False
            pending["approved"] = approved
            self.approval_condition.notify_all()
            return True

    def cancel_turn(self) -> bool:
        if not self.turn_lock.locked():
            return False
        with self.approval_condition:
            if self.pending_approval is not None:
                self.pending_approval["approved"] = False
                self.approval_condition.notify_all()
        return self.agent.request_cancel()

    def _approve(self, tool: str, summary: str) -> bool:
        approval_id = secrets.token_urlsafe(12)
        with self.approval_condition:
            self.pending_approval = {
                "id": approval_id,
                "tool": tool,
                "summary": summary,
                "approved": None,
            }
            self._emit(
                "approval_required",
                {"id": approval_id, "tool": tool, "summary": summary},
            )
            resolved = self.approval_condition.wait_for(
                lambda: self.pending_approval is None
                or self.pending_approval.get("approved") is not None,
                timeout=APPROVAL_TIMEOUT_SECONDS,
            )
            approved = bool(
                resolved
                and self.pending_approval is not None
                and self.pending_approval.get("approved") is True
            )
            self.pending_approval = None
            return approved

    def _agent_event(self, event: str, data: JsonObject) -> None:
        self._emit(event, data)

    def _emit(self, event: str, data: JsonObject) -> None:
        self._write_record({"type": "event", "event": event, "data": data})

    def _write_record(self, payload: JsonObject) -> None:
        writer = self.writer
        if writer is None:
            return
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self.writer_lock:
            try:
                writer.write(encoded)
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.writer = None

    def _visible_conversation(self) -> list[JsonObject]:
        return [
            {
                "role": str(message.get("role") or "assistant"),
                "content": self._redact_text(str(message.get("content") or "")),
                "turn": int(message.get("turn") or 0),
            }
            for message in self.agent.transcript
        ]

    def _web_status(self) -> JsonObject:
        status = self._redact_payload(self.agent.status())
        for name in ("inspected_files", "changed_files"):
            values = status.get(name, [])
            if isinstance(values, list):
                status[name] = [
                    item
                    for item in values
                    if isinstance(item, str)
                    and self.agent.tools.workspace.preview_allowed(item)
                ]
        operations = status.get("operations", [])
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                paths = operation.get("files", [])
                if not isinstance(paths, list):
                    continue
                visible = [
                    path
                    for path in paths
                    if isinstance(path, str)
                    and self.agent.tools.workspace.preview_allowed(path)
                ]
                operation["files"] = visible
                operation["hidden_file_count"] = max(0, len(paths) - len(visible))
        return status

    def _redact_payload(self, payload: JsonObject) -> JsonObject:
        secret = self.config.api_key
        if not secret:
            return payload
        encoded = json.dumps(payload, ensure_ascii=False)
        return json.loads(encoded.replace(secret, "[REDACTED]"))

    def _redact_text(self, text: str) -> str:
        secret = self.config.api_key
        return text.replace(secret, "[REDACTED]") if secret else text

    @staticmethod
    def _session_payload(summary: Any) -> JsonObject:
        payload = asdict(summary)
        payload.pop("path", None)
        return payload

    @staticmethod
    def _result_payload(result: AgentResult) -> JsonObject:
        return {
            "success": result.success,
            "final": result.final,
            "steps": result.steps,
            "reason": result.reason,
            "state": result.state,
        }


class RivetWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: WebRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, RivetRequestHandler)


class RivetRequestHandler(BaseHTTPRequestHandler):
    server: RivetWebServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            if not self._authorized():
                return
            if parsed.path == "/api/bootstrap":
                self._json_response(self.server.runtime.snapshot())
                return
            if parsed.path == "/api/diff":
                query = parse_qs(parsed.query)
                path = query.get("path", [None])[0]
                try:
                    result = self.server.runtime.preview_diff(path)
                except RivetError as exc:
                    self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
                    return
                self._json_response(result)
                return
            if parsed.path == "/api/files":
                try:
                    self._json_response(self.server.runtime.list_workspace_files())
                except RivetError as exc:
                    self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/file":
                query = parse_qs(parsed.query)
                path = query.get("path", [""])[0]
                if not path:
                    self._json_error("file path is required", HTTPStatus.BAD_REQUEST)
                    return
                try:
                    self._json_response(self.server.runtime.preview_workspace_file(path))
                except RivetError as exc:
                    self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/api/session/export":
                query = parse_qs(parsed.query)
                reference = query.get("id", [""])[0]
                if not reference:
                    self._json_error("session id is required", HTTPStatus.BAD_REQUEST)
                    return
                try:
                    filename, content = self.server.runtime.sessions.export_markdown(reference)
                except RivetError as exc:
                    self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
                    return
                self._text_response(content, filename=filename)
                return
            self._json_error("API route not found", HTTPStatus.NOT_FOUND)
            return
        self._serve_asset(parsed.path)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not self._authorized():
            return
        try:
            body = self._json_body()
        except ValueError as exc:
            self._json_error(str(exc), HTTPStatus.BAD_REQUEST)
            return

        runtime = self.server.runtime
        if parsed.path == "/api/turn":
            task = body.get("message")
            if not isinstance(task, str) or not task.strip():
                self._json_error("message must be non-empty text", HTTPStatus.BAD_REQUEST)
                return
            if len(task) > 100_000:
                self._json_error("message is too long", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            if runtime.turn_lock.locked():
                self._json_error("Rivet is already working", HTTPStatus.CONFLICT)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                runtime.run_turn(task.strip(), self.wfile)
            except RuntimeError as exc:
                encoded = (
                    json.dumps(
                        {"type": "fatal_error", "message": str(exc)},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                self.wfile.write(encoded)
                self.wfile.flush()
            return

        if parsed.path == "/api/recover":
            mode = body.get("mode")
            if mode not in {"continue", "retry"}:
                self._json_error(
                    "recovery mode must be continue or retry", HTTPStatus.BAD_REQUEST
                )
                return
            if runtime.turn_lock.locked():
                self._json_error("Rivet is already working", HTTPStatus.CONFLICT)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                runtime.recover_turn(mode, self.wfile)
            except (RuntimeError, ValueError) as exc:
                encoded = (
                    json.dumps(
                        {"type": "recovery_error", "message": str(exc)},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                self.wfile.write(encoded)
                self.wfile.flush()
            return

        if parsed.path == "/api/approval":
            approval_id = body.get("id")
            approved = body.get("approved")
            if not isinstance(approval_id, str) or not isinstance(approved, bool):
                self._json_error("invalid approval response", HTTPStatus.BAD_REQUEST)
                return
            if not runtime.resolve_approval(approval_id, approved):
                self._json_error("approval is no longer pending", HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True})
            return

        if parsed.path == "/api/cancel":
            if not runtime.cancel_turn():
                self._json_error("there is no active turn to cancel", HTTPStatus.CONFLICT)
                return
            self._json_response({"ok": True, "cancel_requested": True})
            return

        try:
            if parsed.path == "/api/session/new":
                self._json_response(runtime.new_session())
                return
            if parsed.path == "/api/session/resume":
                reference = body.get("id")
                if reference is not None and not isinstance(reference, str):
                    raise ValueError("session id must be text")
                self._json_response(runtime.resume_session(reference))
                return
            if parsed.path == "/api/session/rename":
                reference = body.get("id")
                title = body.get("title")
                if not isinstance(reference, str) or not isinstance(title, str):
                    raise ValueError("session id and title must be text")
                self._json_response(runtime.rename_session(reference, title))
                return
            if parsed.path == "/api/session/pin":
                reference = body.get("id")
                pinned = body.get("pinned")
                if not isinstance(reference, str) or not isinstance(pinned, bool):
                    raise ValueError("invalid session pin request")
                self._json_response(runtime.pin_session(reference, pinned))
                return
            if parsed.path == "/api/session/delete":
                reference = body.get("id")
                if not isinstance(reference, str):
                    raise ValueError("session id must be text")
                self._json_response(runtime.delete_session(reference))
                return
            if parsed.path == "/api/revert":
                path = body.get("path")
                if path is not None and not isinstance(path, str):
                    raise ValueError("file path must be text")
                self._json_response(runtime.revert_changes(path))
                return
            if parsed.path == "/api/undo":
                operation_id = body.get("operation_id")
                if not isinstance(operation_id, int) or isinstance(operation_id, bool):
                    raise ValueError("operation id must be an integer")
                self._json_response(runtime.undo_operation(operation_id))
                return
            if parsed.path == "/api/context/compact":
                self._json_response(runtime.compact_context())
                return
            if parsed.path == "/api/settings/approval":
                mode = body.get("mode")
                if not isinstance(mode, str):
                    raise ValueError("approval mode must be text")
                self._json_response(runtime.set_approval_mode(mode))
                return
        except (RivetError, RuntimeError, ValueError) as exc:
            self._json_error(str(exc), HTTPStatus.CONFLICT)
            return
        self._json_error("API route not found", HTTPStatus.NOT_FOUND)

    def _serve_asset(self, request_path: str) -> None:
        names = {
            "/": "index.html",
            "/index.html": "index.html",
            "/styles.css": "styles.css",
            "/app.js": "app.js",
        }
        name = names.get(unquote(request_path))
        if name is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content = files("rivet").joinpath("webui").joinpath(name).read_bytes()
        except (FileNotFoundError, OSError):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if name == "index.html":
            content = content.replace(b"__RIVET_TOKEN__", self.server.runtime.token.encode("ascii"))
        media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{media_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def _authorized(self) -> bool:
        host = self.headers.get("Host", "")
        hostname = urlsplit("//" + host).hostname
        if hostname not in LOOPBACK_HOSTS:
            self._json_error("loopback host required", HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.hostname not in LOOPBACK_HOSTS or parsed.port != self.server.server_port:
                self._json_error("cross-origin request rejected", HTTPStatus.FORBIDDEN)
                return False
        if not secrets.compare_digest(
            self.headers.get("X-Rivet-Token", ""), self.server.runtime.token
        ):
            self._json_error("invalid local session token", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _json_body(self) -> JsonObject:
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        try:
            payload: Any = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _json_response(self, payload: JsonObject, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _text_response(self, content: str, *, filename: str) -> None:
        encoded = content.encode("utf-8")
        safe_name = quote(filename, safe="")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{safe_name}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _json_error(self, message: str, status: HTTPStatus) -> None:
        self._json_response({"ok": False, "error": message}, status)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def run_web(
    config: Config,
    client: ModelClient,
    *,
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    if not 0 <= port <= 65535:
        raise ConfigurationError("web port must be between 0 and 65535")
    runtime = WebRuntime(config, client)
    try:
        server = RivetWebServer(("127.0.0.1", port), runtime)
    except OSError as exc:
        raise ConfigurationError(f"could not start local web UI: {exc}") from exc
    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"Rivet Web is running at {url}")
    print("Press Ctrl+C to stop the local server.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nRivet Web stopped.")
    finally:
        server.server_close()
    return 0
