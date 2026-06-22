"""General Augment Python SDK public import path."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from genaug.agent import AgentClient
from genaug.client import (
    UNSET,
    GeneralAugmentAPIError,
    GeneralAugmentClient,
    response_output_text,
    response_structured_output,
)

try:
    # Single source of truth: the installed distribution version (pyproject.toml).
    __version__ = _pkg_version("general-augment-sdk")
except PackageNotFoundError:  # pragma: no cover - editable/source checkout fallback
    __version__ = "0.0.0+local"

__all__ = [
    "UNSET",
    "AgentClient",
    "GeneralAugmentAPIError",
    "GeneralAugmentClient",
    "__version__",
    "response_output_text",
    "response_structured_output",
]
