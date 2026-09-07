from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__
from .agent import AgentResult
from .config import Config
from .session import SessionSummary
from .types import JsonObject


def configure_terminal_encoding() -> None:
    """Prefer UTF-8 for the Windows CLI while preserving redirected stream behavior."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class Console:
    """Small dependency-free terminal renderer with graceful plain-text fallback."""

    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    CYAN = "\x1b[36m"

    COMMANDS = (
        ("/help", "/help", "show all commands"),
        ("/status", "/status", "show conversation and verification state"),
        ("/plan", "/plan", "show the current task plan"),
        ("/diff", "/diff [path]", "show changes from this conversation"),
        ("/skills", "/skills", "show available and recently used skills"),
        ("/permissions", "/permissions [mode]", "show or switch safe, ask, or never mode"),
        ("/sessions", "/sessions", "list recent saved conversations"),
        ("/resume", "/resume [id]", "resume the latest or selected conversation"),
        ("/new", "/new", "start a fresh conversation"),
        ("/exit", "/exit", "save and quit Rivet"),
    )

    TOOL_LABELS = {
        "update_plan": "Plan",
        "list_files": "List",
        "read_file": "Read",
        "search_text": "Search",
        "search_history": "Search history",
        "write_file": "Write",
        "replace_text": "Edit",
        "show_diff": "Diff",
        "run_command": "Run",
        "delegate_task": "Delegate",
        "delegate_readonly_tasks": "Parallel delegate",
        "list_skills": "List skills",
        "activate_skill": "Activate skill",
        "read_skill_resource": "Read skill resource",
    }
    UNICODE_GLYPHS = {
        "top": "┌─",
        "side": "│",
        "bottom": "└─",
        "prompt": "› ",
        "working": "•",
        "arrow": "→",
        "success": "✓",
        "failure": "×",
        "notice": "•",
        "pipe": "│",
        "pending": "○",
        "current": "●",
        "blocked": "!",
    }
    ASCII_GLYPHS = {
        "top": "+-",
        "side": "|",
        "bottom": "+-",
        "prompt": "> ",
        "working": "*",
        "arrow": "->",
        "success": "ok",
        "failure": "x",
        "notice": "*",
        "pipe": "|",
        "pending": "o",
        "current": "*",
        "blocked": "!",
    }
    UNICODE_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    ASCII_SPINNER = ("-", "\\", "|", "/")

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        color: bool | None = None,
    ) -> None:
        self.stream = stream
        self.error_stream = error_stream
        self._color = color
        self._unicode = self._supports_unicode()
        self._stream_active = False
        self._stream_parts: list[str] = []
        self._last_stream_text = ""
        self._activity_stop = threading.Event()
        self._activity_thread: threading.Thread | None = None
        self._activity_visible = False
        self._activity_lock = threading.Lock()
        self._activity_descriptor: tuple[str, str, str] | None = None
        if stream is None and color is not False and os.name == "nt":
            self._enable_windows_vt()

    @property
    def output(self) -> TextIO:
        return self.stream or sys.stdout

    @property
    def errors(self) -> TextIO:
        return self.error_stream or sys.stderr

    @property
    def color(self) -> bool:
        if self._color is not None:
            return self._color
        return (
            "NO_COLOR" not in os.environ
            and os.getenv("TERM", "").lower() != "dumb"
            and bool(getattr(self.output, "isatty", lambda: False)())
        )

    def style(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + self.RESET

    def glyph(self, name: str) -> str:
        glyphs = self.UNICODE_GLYPHS if self._unicode else self.ASCII_GLYPHS
        return glyphs[name]

    def banner(self, config: Config) -> None:
        print(
            f"{self.style(self.glyph('top'), self.CYAN)} "
            f"{self.style('Rivet', self.BOLD)} {self.style(__version__, self.DIM)}",
            file=self.output,
        )
        self._banner_row("workspace", str(config.workspace))
        self._banner_row("model", config.model)
        self._banner_row("protocol", config.protocol)
        self._banner_row("approval", config.approval_mode)
        print(
            f"{self.style(self.glyph('bottom'), self.CYAN)} "
            f"{self.style('Type / for commands · Ctrl+C cancels · /exit quits', self.DIM)}",
            file=self.output,
        )

    def _banner_row(self, label: str, value: str) -> None:
        key = self.style(f"{label:<10}", self.DIM)
        print(f"{self.style(self.glyph('side'), self.CYAN)}  {key} {value}", file=self.output)

    def prompt(self) -> str:
        return self.style("\n" + self.glyph("prompt"), self.BOLD, self.GREEN)

    def read_input(self, input_fn: Callable[[str], str] = input) -> str:
        """Read one line, adding a live slash-command menu on interactive terminals."""
        if input_fn is not input or not self._live_input_supported():
            return input_fn(self.prompt())
        try:
            return self._read_live_input()
        except (ImportError, OSError, ValueError):
            return input_fn(self.prompt())

    def event(self, event: str, data: dict[str, Any]) -> None:
        if event != "model_start":
            self._stop_activity()
        if event == "model_start":
            self._last_stream_text = ""
            turn = data.get("turn", 1)
            step = data["step"]
            self._start_activity("Working", f"turn {turn} · step {step}", leading_newline=True)
        elif event == "context_compacted":
            detail = f"context compacted to {data['messages']} messages"
            print(self.style(f"  └ {detail}", self.DIM), file=self.output)
        elif event == "assistant_stream_start":
            self._stream_active = True
            self._stream_parts = []
            print(f"\n{self.style('rivet', self.BOLD)}", file=self.output)
        elif event == "assistant_text_delta":
            text = str(data.get("text") or "")
            self._stream_parts.append(text)
            print(text, end="", file=self.output, flush=True)
        elif event == "assistant_stream_end":
            if self._stream_active:
                print(file=self.output)
            self._stream_active = False
            self._last_stream_text = "".join(self._stream_parts)
        elif event == "assistant_text" and data["text"].strip():
            if data.get("has_tool_calls") and not data.get("streamed"):
                self._text_block("rivet", data["text"].strip())
        elif event == "tool_start":
            label = self.TOOL_LABELS.get(data["name"], data["name"])
            detail = self._tool_arguments(data["name"], data["arguments"])
            prefix = self.style(self.glyph("arrow"), self.CYAN)
            print(f"  {prefix} {label}" + (f"  {detail}" if detail else ""), file=self.output)
            self._start_activity("Running", label, indent="    ")
        elif event == "tool_end":
            self._tool_result(data["name"], data["result"])
        elif event == "subagent_started":
            label = str(data.get("label") or data.get("agent_id") or "sub-agent")
            mode = str(data.get("mode") or "explore")
            prefix = self.style(self.glyph("arrow"), self.CYAN)
            print(f"  {prefix} Sub-agent  {label} [{mode}]", file=self.output)
            self._start_activity("Delegated", label, indent="    ")
        elif event == "subagent_progress":
            label = str(data.get("label") or data.get("agent_id") or "sub-agent")
            step = data.get("step", 0)
            tool = data.get("tool")
            detail = f"{label} · step {step}"
            if tool:
                detail += f" · {self.TOOL_LABELS.get(str(tool), str(tool))}"
            self._start_activity("Sub-agent", detail, indent="    ")
        elif event == "subagent_finished":
            ok = data.get("status") == "completed"
            marker = self.style(
                self.glyph("success") if ok else self.glyph("failure"),
                self.GREEN if ok else self.RED,
            )
            label = str(data.get("label") or data.get("agent_id") or "sub-agent")
            summary = self._truncate(
                " ".join(str(data.get("summary") or "").split()), 140
            )
            print(
                f"    {marker} {label}" + (f"  {summary}" if summary else ""),
                file=self.output,
            )
        elif event == "skill_activated":
            name = str(data.get("name") or "skill")
            source = str(data.get("source") or "")
            marker = self.style(self.glyph("success"), self.GREEN)
            print(
                f"    {marker} Skill activated  {name}"
                + (f" [{source}]" if source else ""),
                file=self.output,
            )
        elif event == "skill_resource_read":
            name = str(data.get("name") or "skill")
            path = str(data.get("path") or "resource")
            print(
                self.style(f"    {self.glyph('pipe')} {name} · {path}", self.DIM),
                file=self.output,
            )
        elif event == "plan_updated":
            self.plan(data, title="Plan updated")
        elif event == "plan_completion_required":
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(
                f"  {marker} Active plan still has unfinished steps",
                file=self.output,
            )
        elif event == "verification_required":
            files = ", ".join(data.get("files", [])) or "changed files"
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(f"  {marker} Verification required  {files}", file=self.output)
        elif event == "cancelled":
            phase = str(data.get("phase") or "current operation")
            marker = self.style("!", self.YELLOW, self.BOLD)
            print(f"\n{marker} Cancelled  {phase}", file=self.output)

    def turn_result(self, result: AgentResult, turn: int) -> None:
        self._stop_activity()
        if result.success:
            marker = self.style(self.glyph("success"), self.GREEN, self.BOLD)
            label = self.style("Completed", self.GREEN, self.BOLD)
        elif result.reason == "cancelled":
            marker = self.style("!", self.YELLOW, self.BOLD)
            label = self.style("Cancelled", self.YELLOW, self.BOLD)
        else:
            marker = self.style(self.glyph("failure"), self.RED, self.BOLD)
            label = self.style("Stopped", self.RED, self.BOLD)
        meta = self.style(f"turn {turn} | {result.steps} step(s)", self.DIM)
        print(f"\n{marker} {label}  {meta}", file=self.output)
        if self._last_stream_text.strip() != result.final.strip():
            self._text_block("rivet", result.final)
        self._last_stream_text = ""

    def session_saved(self, path: Path) -> None:
        print(self.style(f"  session  {path}", self.DIM), file=self.output)

    def session_resumed(self, summary: SessionSummary, drifted: list[str]) -> None:
        self.notice(
            f"Resumed {summary.session_id} | {summary.turns} turn(s) | "
            f"{summary.total_steps} step(s)."
        )
        if drifted:
            files = ", ".join(drifted[:5])
            suffix = " ..." if len(drifted) > 5 else ""
            self.warning(
                f"{len(drifted)} tracked file(s) changed while the session was closed: "
                f"{files}{suffix}. Verification is pending again."
            )

    def sessions(self, sessions: list[SessionSummary]) -> None:
        print(self.style("\nSaved sessions", self.BOLD), file=self.output)
        if not sessions:
            print("  none", file=self.output)
            return
        for index, session in enumerate(sessions, start=1):
            preview = session.task_preview or "(empty task)"
            print(
                f"  {index:>2}. {self.style(session.session_id, self.CYAN)}"
                f"  {session.turns} turn(s)  {preview}",
                file=self.output,
            )
        print(self.style("  Use /resume [session-id]", self.DIM), file=self.output)

    def help(self) -> None:
        print(self.style("\nCommands", self.BOLD), file=self.output)
        for _, usage, description in self.COMMANDS:
            padding = " " * max(1, 18 - len(usage))
            print(
                f"  {self.style(usage, self.CYAN)}{padding}{description}",
                file=self.output,
            )
        print(self.style("\n  Tip: type / to open the command menu.", self.DIM), file=self.output)

    def status(self, status: JsonObject) -> None:
        changed = ", ".join(status["changed_files"]) or "none"
        inspected = ", ".join(status["inspected_files"]) or "none"
        if not status["verification_required"]:
            verification = "not required"
        elif status["verification_passed"]:
            verification = "passed"
        else:
            verification = "pending"
        print(self.style("\nStatus", self.BOLD), file=self.output)
        print(
            f"  turns       {status['turns']}  |  steps {status['total_steps']}"
            f"  |  messages {status['messages']}",
            file=self.output,
        )
        print(f"  inspected   {inspected}", file=self.output)
        print(f"  changed     {changed}", file=self.output)
        print(f"  verification {verification}", file=self.output)
        print(f"  permissions {status.get('approval_mode', 'safe')}", file=self.output)
        tracking = "complete" if status.get("workspace_tracking_complete", True) else "limited"
        print(f"  tracking    {tracking}", file=self.output)
        plan = status.get("plan", {})
        if isinstance(plan, dict) and plan.get("active"):
            counts = plan.get("counts", {})
            completed = counts.get("completed", 0) if isinstance(counts, dict) else 0
            total = len(plan.get("steps", [])) if isinstance(plan.get("steps"), list) else 0
            plan_status = "blocked" if plan.get("blocked") else f"{completed}/{total} completed"
        else:
            plan_status = "none"
        print(f"  plan        {plan_status}", file=self.output)
        subagents = status.get("subagents", {})
        if isinstance(subagents, dict):
            active = subagents.get("active", [])
            history = subagents.get("history", [])
            active_count = len(active) if isinstance(active, list) else 0
            history_count = len(history) if isinstance(history, list) else 0
            print(
                f"  sub-agents  {active_count} active | {history_count} report(s)",
                file=self.output,
            )
        skills = status.get("skills", {})
        if isinstance(skills, dict):
            available = skills.get("available", [])
            active = skills.get("active", [])
            history = skills.get("history", [])
            print(
                f"  skills      {len(active) if isinstance(active, list) else 0} active | "
                f"{len(available) if isinstance(available, list) else 0} available | "
                f"{len(history) if isinstance(history, list) else 0} use(s)",
                file=self.output,
            )

    def skills(self, snapshot: JsonObject) -> None:
        print(self.style("\nSkills", self.BOLD), file=self.output)
        available = snapshot.get("available", [])
        if not isinstance(available, list) or not available:
            print("  no skills available", file=self.output)
            return
        for item in available:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "skill")
            source = str(item.get("source") or "")
            if item.get("active"):
                state = self.style("active", self.GREEN)
            elif item.get("used_count"):
                state = self.style(f"used {item['used_count']}x", self.CYAN)
            else:
                state = self.style("available", self.DIM)
            print(
                f"  {self.style(name, self.CYAN, self.BOLD)}  [{source}]  {state}",
                file=self.output,
            )
            print(f"    {item.get('description', '')}", file=self.output)
        errors = snapshot.get("errors", [])
        if isinstance(errors, list) and errors:
            print(
                self.style(f"  {len(errors)} invalid skill(s) were skipped", self.YELLOW),
                file=self.output,
            )

    def plan(self, plan: JsonObject, *, title: str = "Plan") -> None:
        print(self.style(f"\n{title}", self.BOLD), file=self.output)
        steps = plan.get("steps", [])
        if not isinstance(steps, list) or not steps:
            print("  no active plan", file=self.output)
            return
        explanation = str(plan.get("explanation") or "").strip()
        if explanation:
            print(self.style(f"  {explanation}", self.DIM), file=self.output)
        for index, item in enumerate(steps, start=1):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "pending")
            text = str(item.get("step") or "")
            if status == "completed":
                marker = self.style(self.glyph("success"), self.GREEN, self.BOLD)
            elif status == "in_progress":
                marker = self.style(self.glyph("current"), self.CYAN, self.BOLD)
            elif status == "blocked":
                marker = self.style(self.glyph("blocked"), self.RED, self.BOLD)
            else:
                marker = self.style(self.glyph("pending"), self.DIM)
            print(f"  {marker} {index}. {text}", file=self.output)

    def diff(self, result: JsonObject, path: str | None) -> None:
        print(self.style("\nDiff", self.BOLD), file=self.output)
        if not result["files"]:
            detail = "no tracked changes" if path is None else f"no tracked changes for {path}"
            print(f"  {detail}", file=self.output)
            return
        print(str(result["diff"]).rstrip(), file=self.output)
        if result["truncated"]:
            print(self.style("  output truncated", self.YELLOW), file=self.output)

    def notice(self, text: str) -> None:
        self._stop_activity()
        print(
            f"\n{self.style(self.glyph('notice'), self.CYAN)} {text}",
            file=self.output,
        )

    def warning(self, text: str) -> None:
        self._stop_activity()
        print(f"\n{self.style('!', self.YELLOW, self.BOLD)} {text}", file=self.output)

    def error(self, text: str) -> None:
        self._stop_activity()
        marker = self.style(self.glyph("failure"), self.RED, self.BOLD)
        print(f"{marker} {text}", file=self.errors)

    def goodbye(self) -> None:
        self._stop_activity()
        print(self.style("\nSession ended.", self.DIM), file=self.output)

    def input_cancelled(self) -> None:
        self.notice("Input cleared. Type /exit to quit.")

    def approve(self, tool: str, summary: str) -> bool:
        descriptor = self._activity_descriptor
        self._stop_activity()
        short = self._truncate(summary.replace("\n", " "), 180)
        prompt = self.style(f"\n? Approve {tool}  {short}? [y/N] ", self.YELLOW, self.BOLD)
        answer = input(prompt).strip().lower()
        approved = answer in {"y", "yes"}
        if approved and descriptor is not None:
            label, meta, indent = descriptor
            self._start_activity(label, meta, indent=indent)
        return approved

    def _read_live_input(self) -> str:
        if os.name == "nt":
            import msvcrt

            return self._edit_line(lambda: self._windows_key(msvcrt))

        import termios
        import tty

        descriptor = sys.stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            return self._edit_line(self._posix_key)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)

    def _edit_line(self, read_key: Callable[[], str]) -> str:
        text = ""
        cursor = 0
        selected = 0
        menu_dismissed = False
        previous_menu_lines = 0
        previous_menu_lines = self._render_editor(
            text, cursor, [], selected, previous_menu_lines, first=True
        )

        while True:
            key = read_key()
            matches = [] if menu_dismissed else self._command_matches(text)
            if key == "ENTER":
                if matches:
                    text = matches[min(selected, len(matches) - 1)][0]
                self._finish_editor(text, previous_menu_lines)
                return text
            if key == "CTRL_C":
                self._finish_editor(text + "^C", previous_menu_lines)
                raise KeyboardInterrupt
            if key == "CTRL_D":
                if not text:
                    self._finish_editor("", previous_menu_lines)
                    raise EOFError
                if cursor < len(text):
                    text = text[:cursor] + text[cursor + 1 :]
                    menu_dismissed = False
            elif key == "BACKSPACE":
                if cursor:
                    text = text[: cursor - 1] + text[cursor:]
                    cursor -= 1
                    selected = 0
                    menu_dismissed = False
            elif key == "DELETE":
                if cursor < len(text):
                    text = text[:cursor] + text[cursor + 1 :]
                    selected = 0
                    menu_dismissed = False
            elif key == "LEFT":
                cursor = max(0, cursor - 1)
            elif key == "RIGHT":
                cursor = min(len(text), cursor + 1)
            elif key == "HOME" or key == "CTRL_A":
                cursor = 0
            elif key == "END" or key == "CTRL_E":
                cursor = len(text)
            elif key == "CTRL_U":
                text = text[cursor:]
                cursor = 0
                selected = 0
                menu_dismissed = False
            elif key == "ESCAPE":
                menu_dismissed = True
            elif key in {"UP", "DOWN"} and matches:
                offset = -1 if key == "UP" else 1
                selected = (selected + offset) % len(matches)
            elif key == "TAB" and matches:
                command, usage, _ = matches[min(selected, len(matches) - 1)]
                text = command + (" " if usage != command else "")
                cursor = len(text)
                selected = 0
                menu_dismissed = True
            elif len(key) == 1 and key.isprintable():
                text = text[:cursor] + key + text[cursor:]
                cursor += 1
                selected = 0
                menu_dismissed = False

            matches = [] if menu_dismissed else self._command_matches(text)
            if selected >= len(matches):
                selected = 0
            previous_menu_lines = self._render_editor(
                text,
                cursor,
                matches,
                selected,
                previous_menu_lines,
                menu_visible=not menu_dismissed,
            )

    def _render_editor(
        self,
        text: str,
        cursor: int,
        matches: list[tuple[str, str, str]],
        selected: int,
        previous_menu_lines: int,
        *,
        first: bool = False,
        menu_visible: bool = True,
    ) -> int:
        output = self.output
        if first:
            output.write("\r\n")
        else:
            self._erase_editor_block(previous_menu_lines)

        menu = (
            self._command_menu(matches, selected)
            if menu_visible
            and text.startswith("/")
            and not any(character.isspace() for character in text)
            else []
        )
        prompt = self.style(self.glyph("prompt"), self.BOLD, self.GREEN)
        output.write("\r" + prompt + text)
        for line in menu:
            output.write("\r\n" + line)
        if menu:
            output.write(f"\x1b[{len(menu)}A")
        output.write("\r")
        column = self._display_width(self.glyph("prompt")) + self._display_width(text[:cursor])
        if column:
            output.write(f"\x1b[{column}C")
        output.flush()
        return len(menu)

    def _finish_editor(self, text: str, previous_menu_lines: int) -> None:
        self._erase_editor_block(previous_menu_lines)
        prompt = self.style(self.glyph("prompt"), self.BOLD, self.GREEN)
        self.output.write("\r" + prompt + text + "\r\n")
        self.output.flush()

    def _erase_editor_block(self, menu_lines: int) -> None:
        output = self.output
        output.write("\r\x1b[2K")
        for _ in range(menu_lines):
            output.write("\x1b[1B\r\x1b[2K")
        if menu_lines:
            output.write(f"\x1b[{menu_lines}A")

    def _command_menu(
        self, matches: list[tuple[str, str, str]], selected: int
    ) -> list[str]:
        if not matches:
            return [
                self.style("  No matching commands", self.DIM),
                self.style("  Esc close", self.DIM),
            ]

        terminal_lines = shutil.get_terminal_size((100, 24)).lines
        visible_count = min(len(matches), max(2, min(terminal_lines - 7, 8)))
        start = min(max(0, selected - visible_count + 1), len(matches) - visible_count)
        visible = matches[start : start + visible_count]
        lines = [self.style("  Slash commands", self.BOLD)]
        for offset, (_, usage, description) in enumerate(visible):
            index = start + offset
            marker = self.style(self.glyph("prompt").strip(), self.CYAN) if index == selected else " "
            command = self.style(usage, self.CYAN, self.BOLD) if index == selected else usage
            padding = " " * max(2, 18 - len(usage))
            detail = self.style(description, self.DIM)
            lines.append(f"  {marker} {command}{padding}{detail}")
        if len(visible) < len(matches):
            lines.append(self.style(f"    {start + 1}-{start + len(visible)} of {len(matches)}", self.DIM))
        lines.append(self.style("  ↑↓ select · Tab complete · Enter run · Esc close", self.DIM))
        return lines

    def _command_matches(self, text: str) -> list[tuple[str, str, str]]:
        if not text.startswith("/") or any(character.isspace() for character in text):
            return []
        query = text.lower()
        return [command for command in self.COMMANDS if command[0].startswith(query)]

    def _start_activity(
        self,
        label: str,
        meta: str = "",
        *,
        indent: str = "",
        leading_newline: bool = False,
    ) -> None:
        self._stop_activity()
        self._activity_descriptor = (label, meta, indent)
        if not self._live_output_supported():
            if label == "Working":
                marker = self.style(self.glyph("working"), self.YELLOW)
                detail = self.style(meta, self.DIM)
                print(f"\n{marker} {label}" + (f"  {detail}" if detail else ""), file=self.output)
            return

        if leading_newline:
            self.output.write("\n")
        self._activity_stop.clear()
        self._activity_visible = True
        frames = self.UNICODE_SPINNER if self._unicode else self.ASCII_SPINNER

        def animate() -> None:
            index = 0
            while not self._activity_stop.is_set():
                marker = self.style(frames[index % len(frames)], self.CYAN, self.BOLD)
                title = self.style(label, self.BOLD)
                detail = self.style(meta, self.DIM)
                line = f"{indent}{marker} {title}" + (f"  {detail}" if detail else "")
                with self._activity_lock:
                    self.output.write("\r\x1b[2K" + line)
                    self.output.flush()
                index += 1
                self._activity_stop.wait(0.08)

        self._activity_thread = threading.Thread(target=animate, daemon=True)
        self._activity_thread.start()

    def _stop_activity(self) -> None:
        thread = self._activity_thread
        if thread is not None:
            self._activity_stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=0.25)
        self._activity_thread = None
        if self._activity_visible and self._live_output_supported():
            with self._activity_lock:
                self.output.write("\r\x1b[2K")
                self.output.flush()
        self._activity_visible = False

    def _live_input_supported(self) -> bool:
        return (
            self.stream is None
            and bool(getattr(sys.stdin, "isatty", lambda: False)())
            and self._live_output_supported()
        )

    def _live_output_supported(self) -> bool:
        return bool(getattr(self.output, "isatty", lambda: False)())

    @staticmethod
    def _windows_key(msvcrt: Any) -> str:
        character = msvcrt.getwch()
        if character in {"\x00", "\xe0"}:
            return {
                "H": "UP",
                "P": "DOWN",
                "K": "LEFT",
                "M": "RIGHT",
                "G": "HOME",
                "O": "END",
                "S": "DELETE",
            }.get(msvcrt.getwch(), "UNKNOWN")
        return Console._control_key(character)

    @staticmethod
    def _posix_key() -> str:
        import select

        character = sys.stdin.read(1)
        if character != "\x1b":
            return Console._control_key(character)
        sequence = character
        while select.select([sys.stdin], [], [], 0.015)[0] and len(sequence) < 4:
            sequence += sys.stdin.read(1)
        return {
            "\x1b[A": "UP",
            "\x1b[B": "DOWN",
            "\x1b[C": "RIGHT",
            "\x1b[D": "LEFT",
            "\x1b[H": "HOME",
            "\x1b[F": "END",
            "\x1b[3~": "DELETE",
        }.get(sequence, "ESCAPE")

    @staticmethod
    def _control_key(character: str) -> str:
        return {
            "\r": "ENTER",
            "\n": "ENTER",
            "\x03": "CTRL_C",
            "\x04": "CTRL_D",
            "\x01": "CTRL_A",
            "\x05": "CTRL_E",
            "\x15": "CTRL_U",
            "\x08": "BACKSPACE",
            "\x7f": "BACKSPACE",
            "\t": "TAB",
            "\x1b": "ESCAPE",
        }.get(character, character)

    @staticmethod
    def _display_width(text: str) -> int:
        width = 0
        for character in text:
            if unicodedata.combining(character):
                continue
            width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        return width

    def _tool_result(self, name: str, raw_result: str) -> None:
        try:
            result = json.loads(raw_result)
        except json.JSONDecodeError:
            marker = self.style(self.glyph("failure"), self.RED)
            print(f"    {marker} invalid tool result", file=self.output)
            return

        if not result.get("ok"):
            cancelled = result.get("cancelled") is True
            marker = self.style(
                "!" if cancelled else self.glyph("failure"),
                self.YELLOW if cancelled else self.RED,
            )
            detail = self._truncate(str(result.get("error") or "tool failed"), 180)
            print(f"    {marker} {detail}", file=self.output)
            return

        command_cancelled = name == "run_command" and result.get("cancelled") is True
        command_failed = name == "run_command" and (
            result.get("timed_out") or result.get("exit_code") != 0
        )
        if command_cancelled:
            marker = self.style("!", self.YELLOW)
        elif command_failed:
            marker = self.style(self.glyph("failure"), self.RED)
        else:
            marker = self.style(self.glyph("success"), self.GREEN)
        detail = self._tool_result_detail(name, result)
        print(f"    {marker}" + (f" {detail}" if detail else " done"), file=self.output)
        if name == "run_command":
            for line in self._command_excerpt(result):
                pipe = self.glyph("pipe")
                print(self.style(f"      {pipe} {line}", self.DIM), file=self.output)

    @classmethod
    def _tool_arguments(cls, name: str, raw_arguments: str) -> str:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return cls._truncate(raw_arguments.replace("\n", " "), 120)
        if not isinstance(arguments, dict):
            return cls._truncate(str(arguments), 120)
        if name == "run_command":
            return cls._truncate(str(arguments.get("command") or ""), 120)
        if name == "update_plan":
            steps = arguments.get("steps", [])
            count = len(steps) if isinstance(steps, list) else 0
            explanation = cls._truncate(str(arguments.get("explanation") or ""), 80)
            return f"{count} step(s)" + (f" | {explanation}" if explanation else "")
        if name == "delegate_task":
            label = str(arguments.get("label") or arguments.get("mode") or "sub-agent")
            task = cls._truncate(str(arguments.get("task") or ""), 90)
            return f"{label} | {task}"
        if name == "delegate_readonly_tasks":
            tasks = arguments.get("tasks", [])
            count = len(tasks) if isinstance(tasks, list) else 0
            return f"{count} read-only sub-agent(s)"
        if name == "activate_skill":
            return str(arguments.get("name") or "")
        if name == "read_skill_resource":
            return f"{arguments.get('skill', '')} · {arguments.get('path', '')}"
        if name == "search_text":
            query = cls._truncate(str(arguments.get("query") or ""), 70)
            path = str(arguments.get("path") or ".")
            return f"{query!r} in {path}"
        path = arguments.get("path")
        return cls._truncate(str(path), 120) if path else ""

    @classmethod
    def _tool_result_detail(cls, name: str, result: JsonObject) -> str:
        if name == "read_file":
            return f"{result.get('path', '')} | {result.get('total_lines', 0)} lines"
        if name == "list_files":
            return f"{len(result.get('entries', []))} entries"
        if name == "search_text":
            return f"{len(result.get('matches', []))} matches"
        if name == "write_file":
            return f"{result.get('action', 'written')} {result.get('path', '')}"
        if name == "replace_text":
            return f"{result.get('replacements', 0)} replacement(s) in {result.get('path', '')}"
        if name == "show_diff":
            return f"{len(result.get('files', []))} changed file(s)"
        if name == "run_command":
            if result.get("cancelled"):
                detail = "cancelled"
            elif result.get("timed_out"):
                detail = "timed out"
            else:
                detail = f"exit {result.get('exit_code')}"
            if result.get("verification"):
                detail += " | verification"
            change_count = result.get("file_change_count")
            if not isinstance(change_count, int):
                changes = result.get("file_changes", [])
                change_count = len(changes) if isinstance(changes, list) else 0
            if change_count:
                detail += f" | {change_count} file change(s)"
            if result.get("tracking_complete") is False:
                detail += " | tracking limited"
            return detail
        if name == "update_plan":
            steps = result.get("steps", [])
            count = len(steps) if isinstance(steps, list) else 0
            return f"{count} step(s)" + (" updated" if result.get("changed") else " unchanged")
        if name == "delegate_task":
            return (
                f"{result.get('label', 'sub-agent')} | "
                f"{result.get('status', 'returned')} | {result.get('steps', 0)} step(s)"
            )
        if name == "delegate_readonly_tasks":
            return f"{result.get('report_count', 0)} report(s) returned"
        if name == "list_skills":
            return f"{result.get('count', 0)} skill(s) available"
        if name == "activate_skill":
            state = "already active" if result.get("already_active") else "activated"
            return f"{result.get('name', 'skill')} | {state}"
        if name == "read_skill_resource":
            return f"{result.get('name', 'skill')} · {result.get('path', '')}"
        return str(result.get("path") or "done")

    def _text_block(self, speaker: str, text: str) -> None:
        print(f"\n{self.style(speaker, self.BOLD)}", file=self.output)
        print(text.strip(), file=self.output)

    @classmethod
    def _command_excerpt(cls, result: JsonObject) -> list[str]:
        lines: list[str] = []
        for key in ("stdout", "stderr"):
            value = str(result.get(key) or "")
            lines.extend(line.strip() for line in value.splitlines() if line.strip())
        return [cls._truncate(line, 140) for line in lines[-3:]]

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _supports_unicode(self) -> bool:
        encoding = getattr(self.output, "encoding", None)
        if not encoding:
            return True
        try:
            "┌─│└›•→✓×".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

    @staticmethod
    def _enable_windows_vt() -> None:
        """Enable ANSI processing on older Windows console hosts when available."""
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except (AttributeError, OSError):
            pass
