"""Standalone CLI package for the agent platform."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Single-source the version: installed distribution metadata, else local pyproject.

    Installed (``pip install general-augment-cli``) resolves via metadata. A source
    checkout (e.g. running from ``packages/cli/src`` on ``PYTHONPATH`` without an install,
    as CI does) has no distribution metadata, so fall back to reading the version straight
    from the package's ``pyproject.toml`` rather than reporting ``0.0.0+local``.
    """
    try:
        return _pkg_version("general-augment-cli")
    except PackageNotFoundError:
        pass
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0.0.0+local"


__version__ = _resolve_version()
