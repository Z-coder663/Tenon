from __future__ import annotations

from .client import OpenAICompatibleClient
from .config import Config
from .errors import ConfigurationError
from .types import ModelClient


def create_model_client(config: Config) -> ModelClient:
    """Create a protocol client without exposing provider details to the Agent."""
    if config.protocol == "openai_chat":
        return OpenAICompatibleClient(config)
    raise ConfigurationError(f"no client is available for protocol: {config.protocol}")
