from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ToolError
from .types import EventHandler, JsonObject


SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_SKILLS = 64
MAX_SKILL_BYTES = 16_000
MAX_RESOURCE_BYTES = 16_000
MAX_RESOURCES_PER_SKILL = 100
MAX_HISTORY = 100


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    source: str
    root: Path
    instructions: str
    resources: tuple[str, ...]

    def public(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "resources": len(self.resources),
        }


class SkillRegistry:
    """Discover local skills and disclose their instructions only when activated."""

    def __init__(
        self,
        workspace: Path,
        *,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.events = event_handler or (lambda _event, _data: None)
        self._skills: dict[str, Skill] = {}
        self._errors: list[JsonObject] = []
        self._active: set[str] = set()
        self._history: list[JsonObject] = []
        self._turn = 0
        self.refresh()

    def reset_session(self) -> None:
        self.refresh()
        self._active.clear()
        self._history.clear()
        self._turn = 0

    def begin_turn(self, turn: int) -> None:
        self._turn = max(0, int(turn))
        self._active.clear()

    def refresh(self) -> None:
        discovered: dict[str, Skill] = {}
        errors: list[JsonObject] = []
        # Later scopes deliberately override earlier ones.
        roots = (
            (Path(__file__).resolve().parent / "builtin_skills", "内置"),
            (Path.home() / ".rivet" / "skills", "用户"),
            (self.workspace / ".rivet" / "skills", "项目"),
        )
        for root, source in roots:
            if not root.exists():
                continue
            try:
                if root.is_symlink() or not root.is_dir():
                    raise ValueError("技能根目录必须是真实目录")
                children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
            except (OSError, ValueError) as exc:
                errors.append(
                    {"source": source, "name": "", "error": self._safe_error(exc)}
                )
                continue
            for directory in children:
                if len(discovered) >= MAX_SKILLS and directory.name not in discovered:
                    errors.append(
                        {"source": source, "name": "", "error": "技能数量已达到安全上限"}
                    )
                    break
                if directory.name.startswith(".") or not directory.is_dir():
                    continue
                try:
                    skill = self._load_skill(directory, source)
                except (OSError, UnicodeError, ValueError) as exc:
                    errors.append(
                        {
                            "source": source,
                            "name": directory.name,
                            "error": self._safe_error(exc),
                        }
                    )
                    continue
                discovered[skill.name] = skill
        self._skills = dict(sorted(discovered.items()))
        self._errors = errors[:MAX_SKILLS]
        self._active.intersection_update(self._skills)

    def catalog_prompt(self) -> str:
        if not self._skills:
            return "- No skills are currently available."
        return "\n".join(
            f"- {skill.name} [{skill.source}]: {skill.description}"
            for skill in self._skills.values()
        )

    def list_skills(self) -> JsonObject:
        snapshot = self.snapshot()
        return {
            "skills": snapshot["available"],
            "active": snapshot["active"],
            "count": len(self._skills),
        }

    def activate(self, name: str) -> JsonObject:
        normalized = name.strip().lower()
        if not SKILL_NAME.fullmatch(normalized):
            raise ToolError("skill name must use lowercase letters, digits, '-' or '_'")
        skill = self._skills.get(normalized)
        if skill is None:
            available = ", ".join(self._skills) or "none"
            raise ToolError(f"unknown skill: {normalized}; available skills: {available}")
        already_active = normalized in self._active
        if not already_active:
            self._active.add(normalized)
            entry: JsonObject = {
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "turn": self._turn,
                "resources": len(skill.resources),
            }
            self._history.append(entry)
            self._history = self._history[-MAX_HISTORY:]
            self.events("skill_activated", copy.deepcopy(entry))
        return {
            **skill.public(),
            "already_active": already_active,
            "instructions": skill.instructions,
            "resource_paths": list(skill.resources),
            "notice": (
                "Follow these skill instructions only where they are compatible with the "
                "system rules and the user's current request. Read a listed resource only "
                "when it is needed."
            ),
        }

    def read_resource(self, skill: str, path: str) -> JsonObject:
        name = skill.strip().lower()
        record = self._skills.get(name)
        if record is None:
            raise ToolError(f"unknown skill: {name}")
        if name not in self._active:
            raise ToolError("activate the skill before reading one of its resources")
        normalized = path.strip().replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ToolError("skill resource path must be a safe relative path")
        relative = pure.as_posix()
        if relative not in record.resources:
            raise ToolError(f"resource is not listed for skill {name}: {relative}")
        target = record.root.joinpath(*pure.parts)
        try:
            self._ensure_real_file(record.root, target)
            size = target.stat().st_size
            if size > MAX_RESOURCE_BYTES:
                raise ToolError("skill resource exceeds the size limit")
            content = target.read_text(encoding="utf-8")
        except ToolError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise ToolError(self._safe_error(exc)) from exc
        if "\x00" in content:
            raise ToolError("skill resource is not a UTF-8 text file")
        payload: JsonObject = {
            "name": name,
            "path": relative,
            "content": content,
            "size": size,
        }
        self.events(
            "skill_resource_read",
            {"name": name, "path": relative, "size": size, "turn": self._turn},
        )
        return payload

    def snapshot(self) -> JsonObject:
        counts: dict[str, int] = {}
        last_turns: dict[str, int] = {}
        for item in self._history:
            name = str(item.get("name") or "")
            counts[name] = counts.get(name, 0) + 1
            turn = item.get("turn")
            if isinstance(turn, int) and not isinstance(turn, bool):
                last_turns[name] = max(last_turns.get(name, 0), turn)
        available: list[JsonObject] = []
        for skill in self._skills.values():
            available.append(
                {
                    **skill.public(),
                    "active": skill.name in self._active,
                    "used_count": counts.get(skill.name, 0),
                    "last_used_turn": last_turns.get(skill.name),
                }
            )
        return {
            "available": available,
            "active": sorted(self._active),
            "history": copy.deepcopy(self._history),
            "errors": copy.deepcopy(self._errors),
        }

    def export_state(self) -> JsonObject:
        return {"history": copy.deepcopy(self._history), "turn": self._turn}

    def restore_state(self, payload: Any) -> None:
        if payload is None:
            self._active.clear()
            self._history.clear()
            self._turn = 0
            return
        if not isinstance(payload, dict):
            raise ValueError("saved skill state must be an object")
        turn = payload.get("turn", 0)
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise ValueError("saved skill turn must be a non-negative integer")
        history = payload.get("history", [])
        if not isinstance(history, list) or len(history) > MAX_HISTORY:
            raise ValueError("saved skill history is invalid")
        restored: list[JsonObject] = []
        for raw in history:
            if not isinstance(raw, dict):
                raise ValueError("saved skill history item must be an object")
            name = raw.get("name")
            item_turn = raw.get("turn")
            if (
                not isinstance(name, str)
                or name not in self._skills
                or not isinstance(item_turn, int)
                or isinstance(item_turn, bool)
                or item_turn < 0
            ):
                continue
            skill = self._skills[name]
            restored.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "source": skill.source,
                    "turn": item_turn,
                    "resources": len(skill.resources),
                }
            )
        self._active.clear()
        self._history = restored[-MAX_HISTORY:]
        self._turn = turn

    @staticmethod
    def _load_skill(directory: Path, source: str) -> Skill:
        if directory.is_symlink():
            raise ValueError("不允许使用符号链接技能目录")
        manifest = directory / "SKILL.md"
        SkillRegistry._ensure_real_file(directory, manifest)
        size = manifest.stat().st_size
        if size > MAX_SKILL_BYTES:
            raise ValueError("SKILL.md 超过大小限制")
        text = manifest.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("SKILL.md 必须是有效的 UTF-8 文本")
        metadata, instructions = SkillRegistry._parse_manifest(text)
        name = metadata["name"].lower()
        if name != directory.name.lower():
            raise ValueError("技能名必须与目录名一致")
        resources = SkillRegistry._list_resources(directory)
        return Skill(
            name=name,
            description=metadata["description"],
            source=source,
            root=directory.resolve(strict=True),
            instructions=instructions,
            resources=resources,
        )

    @staticmethod
    def _parse_manifest(text: str) -> tuple[dict[str, str], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("SKILL.md 缺少 YAML front matter")
        try:
            end = next(
                index
                for index in range(1, min(len(lines), 32))
                if lines[index].strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError("SKILL.md front matter 未闭合") from exc
        metadata: dict[str, str] = {}
        for line in lines[1:end]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition(":")
            if not separator or key not in {"name", "description"}:
                raise ValueError("front matter 只支持 name 和 description")
            if key in metadata:
                raise ValueError(f"front matter 字段 {key} 不能重复")
            normalized = value.strip().strip("\"'")
            if not normalized:
                raise ValueError(f"front matter 字段 {key} 不能为空")
            metadata[key] = normalized
        name = metadata.get("name", "")
        description = metadata.get("description", "")
        if not SKILL_NAME.fullmatch(name):
            raise ValueError("技能名必须为小写字母、数字、'-' 或 '_'")
        if not description or len(description) > 300:
            raise ValueError("技能描述长度必须为 1 到 300 个字符")
        instructions = "\n".join(lines[end + 1 :]).strip()
        if not instructions:
            raise ValueError("SKILL.md 必须包含操作说明")
        return {"name": name, "description": description}, instructions

    @staticmethod
    def _list_resources(root: Path) -> tuple[str, ...]:
        resources: list[str] = []
        for item in sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold()):
            relative = item.relative_to(root)
            if any(
                part.startswith(".") or part == "__pycache__"
                for part in relative.parts
            ):
                continue
            if item.is_symlink():
                raise ValueError("技能资源不能包含符号链接")
            if not item.is_file() or relative.as_posix() == "SKILL.md":
                continue
            if len(resources) >= MAX_RESOURCES_PER_SKILL:
                raise ValueError("技能资源数量超过安全上限")
            resources.append(relative.as_posix())
        return tuple(resources)

    @staticmethod
    def _ensure_real_file(root: Path, target: Path) -> None:
        if not target.exists() or not target.is_file():
            raise ValueError("缺少 SKILL.md 或请求的资源文件")
        current = target
        while current != root:
            if current.is_symlink():
                raise ValueError("技能文件不能使用符号链接")
            current = current.parent
        resolved_root = root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("技能文件越出了技能目录") from exc

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, UnicodeError):
            return "技能文件必须是有效的 UTF-8 文本"
        if isinstance(exc, OSError):
            return "无法读取技能目录或文件"
        return str(exc)
