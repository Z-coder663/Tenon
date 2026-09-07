"""Domain errors with messages safe to show in the CLI."""


class RivetError(Exception):
    """Base class for expected failures."""


class ConfigurationError(RivetError):
    """Configuration is missing or invalid."""


class ModelError(RivetError):
    """The model endpoint failed or returned an invalid response."""


class OperationCancelled(RivetError):
    """The active model or tool operation was cancelled by the user."""


class SessionError(RivetError):
    """A saved conversation could not be stored or restored safely."""


class ToolError(RivetError):
    """A local tool could not complete its operation."""


class WorkspaceViolation(ToolError):
    """A path escaped the configured workspace."""

