"""Secret-safe application environment adapters for launch provisioning."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from platform_cli.errors import CLIError
from platform_cli.secure_filesystem import (
    assert_no_symlink_components,
    atomic_write_text_no_follow,
    lexical_path,
    read_text_no_follow,
)

MANAGED_START = "# General Augment launch managed variables"
MANAGED_END = "# End General Augment launch managed variables"
RUNTIME_ENV_NAMES = (
    "GENAUG_API_KEY",
    "GENAUG_PROJECT_ID",
    "GENAUG_API_BASE_URL",
)
ACTIVATION_VARIABLE_PATTERN = re.compile(r"^GENAUG_[A-Z0-9_]+_ENABLED$")


@dataclass(frozen=True)
class EnvironmentHandoffResult:
    """Non-sensitive metadata for one environment handoff operation."""

    status: str
    target: Path
    variables: tuple[str, ...]
    managed_variables: tuple[str, ...]
    preserved_variables: tuple[str, ...]
    permission_mode: str | None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without environment values."""
        return {
            "status": self.status,
            "target": str(self.target),
            "variables": list(self.variables),
            "managed_variables": list(self.managed_variables),
            "preserved_variables": list(self.preserved_variables),
            "permission_mode": self.permission_mode,
        }


class ApplicationEnvironmentAdapter(Protocol):
    """Boundary for local or hosted environment configuration providers."""

    def apply(self, values: dict[str, str]) -> EnvironmentHandoffResult:
        """Apply runtime variables without returning their values."""

    def remove(self) -> EnvironmentHandoffResult:
        """Remove only entries owned by this adapter."""


class LocalEnvFileAdapter:
    """Manage General Augment variables in an ignored local env file."""

    def __init__(self, workspace: Path, target: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        selected = (target or self.workspace / ".env.local").expanduser()
        self.target = lexical_path(
            selected if selected.is_absolute() else self.workspace / selected
        )

    def apply(self, values: dict[str, str]) -> EnvironmentHandoffResult:
        """Write a managed env block after repository safety checks."""
        normalized = _validated_values(values)
        managed_names = tuple(normalized)
        self._check_target()
        repository = _git_root(self.workspace)
        lines = _without_managed_block(_read_lines(repository, self.target))
        conflicts = tuple(
            name
            for name in managed_names
            if any(_assignment_name(line) == name for line in lines)
        )
        if conflicts:
            raise CLIError(
                "Refusing to replace pre-existing application environment variables: "
                f"{', '.join(conflicts)}. Remove or relocate them before configuring launch."
            )
        output = list(lines)
        if output and output[-1] != "":
            output.append("")
        output.extend(
            [
                MANAGED_START,
                *(f"{name}={normalized[name]}" for name in managed_names),
                MANAGED_END,
            ]
        )
        _atomic_write(repository, self.target, output)
        return EnvironmentHandoffResult(
            status="configured",
            target=self.target,
            variables=managed_names,
            managed_variables=managed_names,
            preserved_variables=(),
            permission_mode="0600",
        )

    def remove(self) -> EnvironmentHandoffResult:
        """Remove the managed block while retaining pre-existing assignments."""
        self._check_target(require_existing=False)
        if not self.target.exists():
            return EnvironmentHandoffResult(
                status="absent",
                target=self.target,
                variables=RUNTIME_ENV_NAMES,
                managed_variables=(),
                preserved_variables=(),
                permission_mode=None,
            )
        repository = _git_root(self.workspace)
        before = _read_lines(repository, self.target)
        managed_before = _managed_assignment_names(before)
        after = _without_managed_block(before)
        removed = tuple(
            name
            for name in managed_before
            if any(_assignment_name(line) == name for line in before)
            and not any(_assignment_name(line) == name for line in after)
        )
        retained = tuple(
            name
            for name in managed_before
            if any(_assignment_name(line) == name for line in after)
        )
        _atomic_write(repository, self.target, after)
        return EnvironmentHandoffResult(
            status="removed" if before != after else "unchanged",
            target=self.target,
            variables=RUNTIME_ENV_NAMES,
            managed_variables=removed,
            preserved_variables=retained,
            permission_mode="0600",
        )

    def _check_target(self, *, require_existing: bool = False) -> None:
        """Refuse tracked, unignored, external, or symlinked env targets."""
        repository = _git_root(self.workspace)
        try:
            relative = self.target.relative_to(repository)
        except ValueError as exc:
            raise CLIError("Application env target must stay inside the Git repository.") from exc
        assert_no_symlink_components(
            repository,
            self.target,
            description="application env target",
        )
        if require_existing and not self.target.exists():
            raise CLIError(f"Application env target does not exist: {self.target}")
        relative_text = relative.as_posix()
        if _git_status(repository, "ls-files", "--error-unmatch", "--", relative_text) == 0:
            raise CLIError(
                f"Refusing to write runtime credentials to tracked file: {relative_text}"
            )
        if _git_status(repository, "check-ignore", "-q", "--", relative_text) != 0:
            raise CLIError(
                f"Refusing to write runtime credentials because {relative_text} is not Git-ignored."
            )


def _validated_values(values: dict[str, str]) -> dict[str, str]:
    """Require the runtime contract plus reviewed boolean activation variables."""
    missing = [name for name in RUNTIME_ENV_NAMES if not str(values.get(name) or "").strip()]
    if missing:
        raise CLIError(f"Runtime environment values are missing for: {', '.join(missing)}")
    unexpected = [
        name
        for name in values
        if name not in RUNTIME_ENV_NAMES and not ACTIVATION_VARIABLE_PATTERN.fullmatch(name)
    ]
    if unexpected:
        raise CLIError(
            "Runtime environment contains unsupported managed variables: "
            + ", ".join(sorted(unexpected))
        )
    normalized = {name: str(values[name]).strip() for name in RUNTIME_ENV_NAMES}
    for name in sorted(set(values) - set(RUNTIME_ENV_NAMES)):
        value = str(values[name]).strip().lower()
        if value not in {"true", "false"}:
            raise CLIError(f"Activation variable {name} must be true or false.")
        normalized[name] = value
    if any("\n" in value or "\r" in value for value in normalized.values()):
        raise CLIError("Runtime environment values may not contain line breaks.")
    return normalized


def reviewed_activation_environment_values(artifact: dict[str, Any]) -> dict[str, str]:
    """Derive boolean app-owned capability flags from the exact reviewed launch plan.

    The manifest may choose a variable name but never a value. Only the narrow
    ``GENAUG_*_ENABLED`` namespace is accepted, and approval always enables the
    reviewed read-only capability with the literal non-secret value ``true``.
    """
    plan = artifact.get("plan")
    capabilities = plan.get("capabilities") if isinstance(plan, dict) else None
    if not isinstance(capabilities, list):
        return {}
    values: dict[str, str] = {}
    for item in capabilities:
        if not isinstance(item, dict) or item.get("enable_after_review") is not True:
            continue
        if (
            item.get("classification") != "read_only"
            or item.get("execution_owner") != "application"
        ):
            raise CLIError(
                "Only reviewed read-only application-owned capabilities may be activated."
            )
        name = item.get("activation_variable")
        if name is None:
            continue
        normalized = str(name).strip()
        if not ACTIVATION_VARIABLE_PATTERN.fullmatch(normalized):
            raise CLIError(
                "Capability activation variables must match GENAUG_*_ENABLED."
            )
        values[normalized] = "true"
    return values


def _git_root(workspace: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CLIError("Application environment handoff requires a Git repository.")
    return Path(result.stdout.strip()).resolve()


def _git_status(repository: Path, *arguments: str) -> int:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode


def _read_lines(repository: Path, path: Path) -> list[str]:
    content = read_text_no_follow(
        repository,
        path,
        description="application env target",
    )
    if content is None:
        return []
    return content.splitlines()


def _assignment_name(line: str) -> str | None:
    match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
    return match.group(1) if match else None


def _managed_assignment_names(lines: list[str]) -> tuple[str, ...]:
    """Return assignments owned by the delimited General Augment block."""
    names: list[str] = []
    inside = False
    for line in lines:
        if line == MANAGED_START:
            inside = True
            continue
        if inside and line == MANAGED_END:
            break
        if inside:
            name = _assignment_name(line)
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _without_managed_block(lines: list[str]) -> list[str]:
    output: list[str] = []
    inside = False
    for line in lines:
        if line == MANAGED_START:
            inside = True
            continue
        if inside and line == MANAGED_END:
            inside = False
            continue
        if not inside:
            output.append(line)
    if inside:
        raise CLIError("Application env file contains an unterminated General Augment block.")
    while output and output[-1] == "":
        output.pop()
    return output


def _atomic_write(repository: Path, path: Path, lines: list[str]) -> None:
    atomic_write_text_no_follow(
        repository,
        path,
        "\n".join(lines) + ("\n" if lines else ""),
        description="application env target",
        mode=0o600,
    )
