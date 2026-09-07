from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ConfigurationError
from .types import JsonObject


MAX_ENV_BYTES = 128_000
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
RESERVED_BODY_FIELDS = {"model", "messages", "tools", "tool_choice", "stream"}
RESERVED_HEADER_FIELDS = {"content-type", "accept", "user-agent"}
RESERVED_MESSAGE_FIELDS = {"role", "content", "tool_calls"}


def _default_env_candidates() -> list[Path]:
    candidates = [Path.home() / ".rivet" / ".env"]
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file() and (
        source_root / ".env.example"
    ).is_file():
        candidates.append(source_root / ".env")
    candidates.append(Path.cwd() / ".env")
    return candidates


def _load_env_file(reference: str | Path | None) -> tuple[dict[str, str], Path | None]:
    selected = reference
    if selected is None:
        configured = os.environ.get("RIVET_ENV_FILE")
        selected = configured.strip() if configured and configured.strip() else None

    explicit = selected is not None
    if explicit:
        paths = [Path(selected).expanduser().resolve(strict=False)]
    else:
        paths = [candidate.resolve(strict=False) for candidate in _default_env_candidates()]

    path = next((candidate for candidate in paths if candidate.exists()), None)
    if path is None:
        if explicit:
            raise ConfigurationError(f"environment file does not exist: {paths[0]}")
        return {}, None
    if not path.is_file():
        raise ConfigurationError(f"environment file is not a regular file: {path}")
    try:
        if path.stat().st_size > MAX_ENV_BYTES:
            raise ConfigurationError("environment file exceeds the 128 KB limit")
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("environment file must be UTF-8") from exc
    except OSError as exc:
        raise ConfigurationError(f"could not read environment file: {exc}") from exc

    values: dict[str, str] = {}
    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"invalid environment file line {line_number}: expected NAME=VALUE"
            )
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME.fullmatch(name):
            raise ConfigurationError(
                f"invalid environment variable name on line {line_number}"
            )
        values[name] = _dotenv_value(raw_value, line_number)
    return values, path


def _dotenv_value(raw: str, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] == '"':
        try:
            parsed: Any = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"invalid double-quoted value on environment line {line_number}"
            ) from exc
        if not isinstance(parsed, str):
            raise ConfigurationError(
                f"environment line {line_number} must contain a text value"
            )
        return parsed
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise ConfigurationError(
                f"unterminated single-quoted value on environment line {line_number}"
            )
        return value[1:-1]
    comment = re.search(r"\s+#", value)
    return value[: comment.start()].rstrip() if comment else value


class _Settings:
    def __init__(self, file_values: Mapping[str, str]) -> None:
        self.file_values = file_values

    def get(self, *names: str) -> str | None:
        for source in (os.environ, self.file_values):
            for name in names:
                value = source.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None


def _positive_int(settings: _Settings, name: str, default: int) -> int:
    raw = settings.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _json_object(name: str, raw: str | None) -> JsonObject:
    if raw is None:
        return {}
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must contain a valid JSON object") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must contain a JSON object")
    return value


def _replay_fields(settings: _Settings) -> tuple[str, ...]:
    raw = settings.get("RIVET_REPLAY_FIELDS_JSON")
    if raw is None:
        return ("reasoning_content",)
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "RIVET_REPLAY_FIELDS_JSON must contain a JSON list of field names"
        ) from exc
    if (
        not isinstance(value, list)
        or len(value) > 16
        or any(not isinstance(item, str) for item in value)
    ):
        raise ConfigurationError(
            "RIVET_REPLAY_FIELDS_JSON must contain at most 16 field names"
        )
    fields: list[str] = []
    for item in value:
        if not ENV_NAME.fullmatch(item) or item in RESERVED_MESSAGE_FIELDS:
            raise ConfigurationError(
                f"RIVET_REPLAY_FIELDS_JSON contains an invalid field: {item}"
            )
        if item not in fields:
            fields.append(item)
    return tuple(fields)


def _validate_http_url(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ConfigurationError(f"{name} must not contain embedded credentials")
    return value.rstrip("/")


def _extra_body(settings: _Settings) -> JsonObject:
    value = _json_object("RIVET_EXTRA_BODY_JSON", settings.get("RIVET_EXTRA_BODY_JSON"))
    conflicts = sorted(RESERVED_BODY_FIELDS.intersection(value))
    if conflicts:
        raise ConfigurationError(
            "RIVET_EXTRA_BODY_JSON cannot override core field: " + conflicts[0]
        )
    return value


def _extra_headers(settings: _Settings) -> dict[str, str]:
    value = _json_object(
        "RIVET_EXTRA_HEADERS_JSON", settings.get("RIVET_EXTRA_HEADERS_JSON")
    )
    headers: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
            raise ConfigurationError("RIVET_EXTRA_HEADERS_JSON has an invalid header name")
        if name.lower() in RESERVED_HEADER_FIELDS:
            raise ConfigurationError(
                f"RIVET_EXTRA_HEADERS_JSON cannot override header: {name}"
            )
        if not isinstance(item, str) or "\r" in item or "\n" in item:
            raise ConfigurationError(
                f"RIVET_EXTRA_HEADERS_JSON header {name} must be one line of text"
            )
        headers[name] = item
    return headers


@dataclass(frozen=True)
class Config:
    workspace: Path
    api_key: str | None
    base_url: str
    model: str
    max_steps: int = 30
    request_timeout: int = 120
    command_timeout: int = 120
    max_context_chars: int = 120_000
    max_tool_output_chars: int = 20_000
    approval_mode: str = "safe"
    protocol: str = "openai_chat"
    endpoint_path: str = "/chat/completions"
    endpoint_url: str | None = None
    auth_style: str = "bearer"
    api_key_header: str = "api-key"
    extra_body: JsonObject = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    replay_fields: tuple[str, ...] = ("reasoning_content",)
    env_file: Path | None = None

    @property
    def endpoint(self) -> str:
        return self.endpoint_url or self.base_url + self.endpoint_path

    @classmethod
    def from_env(
        cls,
        workspace: str | Path,
        *,
        model: str | None = None,
        base_url: str | None = None,
        max_steps: int | None = None,
        approval_mode: str | None = None,
        protocol: str | None = None,
        endpoint_url: str | None = None,
        auth_style: str | None = None,
        env_file: str | Path | None = None,
    ) -> "Config":
        file_values, loaded_env = _load_env_file(env_file)
        settings = _Settings(file_values)

        root = Path(workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ConfigurationError(f"workspace does not exist: {root}")

        chosen_protocol = (
            protocol or settings.get("RIVET_PROTOCOL") or "openai_chat"
        ).lower().replace("-", "_")
        protocol_aliases = {
            "openai": "openai_chat",
            "openai_compatible": "openai_chat",
            "chat_completions": "openai_chat",
        }
        chosen_protocol = protocol_aliases.get(chosen_protocol, chosen_protocol)
        if chosen_protocol != "openai_chat":
            raise ConfigurationError(
                f"unsupported protocol: {chosen_protocol}; currently available: openai_chat"
            )

        chosen_model = model or settings.get("RIVET_MODEL", "OPENAI_MODEL")
        if not chosen_model:
            raise ConfigurationError(
                "model is required; pass --model or set RIVET_MODEL"
            )

        chosen_base = base_url or settings.get("RIVET_BASE_URL", "OPENAI_BASE_URL")
        if not chosen_base:
            chosen_base = "https://api.openai.com/v1"
        chosen_base = _validate_http_url("base URL", chosen_base)

        chosen_endpoint = endpoint_url or settings.get("RIVET_ENDPOINT")
        if chosen_endpoint:
            chosen_endpoint = _validate_http_url("endpoint", chosen_endpoint)
        endpoint_path = settings.get("RIVET_ENDPOINT_PATH") or "/chat/completions"
        if not endpoint_path.startswith("/") or "://" in endpoint_path:
            raise ConfigurationError("RIVET_ENDPOINT_PATH must be an absolute URL path")

        chosen_auth = (
            auth_style or settings.get("RIVET_AUTH_STYLE") or "bearer"
        ).lower().replace("_", "-")
        if chosen_auth not in {"bearer", "api-key", "none"}:
            raise ConfigurationError("auth style must be bearer, api-key, or none")
        key = settings.get("RIVET_API_KEY", "OPENAI_API_KEY")
        if chosen_auth != "none" and not key:
            raise ConfigurationError(
                "RIVET_API_KEY is required unless RIVET_AUTH_STYLE=none"
            )
        key_header = settings.get("RIVET_API_KEY_HEADER") or "api-key"
        if not HEADER_NAME.fullmatch(key_header):
            raise ConfigurationError("RIVET_API_KEY_HEADER is not a valid header name")

        chosen_approval = (
            approval_mode or settings.get("RIVET_APPROVAL") or "safe"
        ).lower()
        if chosen_approval not in {"safe", "ask", "never"}:
            raise ConfigurationError("approval mode must be safe, ask, or never")

        chosen_max_steps = (
            max_steps
            if max_steps is not None
            else _positive_int(settings, "RIVET_MAX_STEPS", 30)
        )
        if chosen_max_steps <= 0:
            raise ConfigurationError("max steps must be greater than zero")

        return cls(
            workspace=root,
            api_key=key,
            base_url=chosen_base,
            model=chosen_model,
            max_steps=chosen_max_steps,
            request_timeout=_positive_int(settings, "RIVET_REQUEST_TIMEOUT", 120),
            command_timeout=_positive_int(settings, "RIVET_COMMAND_TIMEOUT", 120),
            max_context_chars=_positive_int(
                settings, "RIVET_MAX_CONTEXT_CHARS", 120_000
            ),
            max_tool_output_chars=_positive_int(
                settings, "RIVET_MAX_TOOL_OUTPUT_CHARS", 20_000
            ),
            approval_mode=chosen_approval,
            protocol=chosen_protocol,
            endpoint_path=endpoint_path,
            endpoint_url=chosen_endpoint,
            auth_style=chosen_auth,
            api_key_header=key_header,
            extra_body=_extra_body(settings),
            extra_headers=_extra_headers(settings),
            replay_fields=_replay_fields(settings),
            env_file=loaded_env,
        )
