"""Tests for the secret-safe launch environment handoff."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from platform_cli import application_environment
from platform_cli.application_environment import (
    LocalEnvFileAdapter,
    reviewed_activation_environment_values,
)
from platform_cli.errors import CLIError


def _repository(tmp_path: Path, *, ignored: bool = True) -> Path:
    root = tmp_path / "app"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if ignored:
        (root / ".gitignore").write_text(".env.local\n", encoding="utf-8")
    else:
        (root / ".gitignore").write_text("!.env.local\n", encoding="utf-8")
    return root


def _values(key: str = "ga_runtime_test_secret") -> dict[str, str]:
    return {
        "GENAUG_API_KEY": key,
        "GENAUG_PROJECT_ID": "project-1",
        "GENAUG_API_BASE_URL": "https://api.example.test",
    }


def test_local_env_handoff_is_secret_free_idempotent_and_rotatable(tmp_path: Path) -> None:
    """Apply should preserve unrelated values and rotate without duplicate lines."""
    root = _repository(tmp_path)
    target = root / ".env.local"
    target.write_text("CLERK_SECRET_KEY=clerk-local\nUNRELATED=yes\n", encoding="utf-8")
    adapter = LocalEnvFileAdapter(root)

    first = adapter.apply(_values())
    second = adapter.apply(_values("ga_runtime_rotated_secret"))

    content = target.read_text(encoding="utf-8")
    serialized = json.dumps(second.as_dict())
    assert first.status == "configured"
    assert second.status == "configured"
    assert "CLERK_SECRET_KEY=clerk-local" in content
    assert "UNRELATED=yes" in content
    assert content.count("GENAUG_API_KEY=") == 1
    assert content.count("GENAUG_PROJECT_ID=") == 1
    assert content.count("GENAUG_API_BASE_URL=") == 1
    assert "ga_runtime_rotated_secret" in content
    assert "ga_runtime_test_secret" not in content
    assert "ga_runtime_rotated_secret" not in serialized
    assert "clerk-local" not in serialized
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_local_env_handoff_removes_only_managed_entries(tmp_path: Path) -> None:
    """Rollback should remove all owned entries and retain unrelated assignments."""
    root = _repository(tmp_path)
    target = root / ".env.local"
    target.write_text("OTHER=value\n", encoding="utf-8")
    adapter = LocalEnvFileAdapter(root)
    adapter.apply(_values())

    result = adapter.remove()

    content = target.read_text(encoding="utf-8")
    assert result.status == "removed"
    assert "GENAUG_API_KEY" not in content
    assert "GENAUG_API_BASE_URL" not in content
    assert "GENAUG_PROJECT_ID" not in content
    assert "OTHER=value" in content
    assert result.preserved_variables == ()


def test_local_env_handoff_manages_reviewed_capability_flag(tmp_path: Path) -> None:
    """Approved non-secret capability flags should be installed and rolled back."""
    root = _repository(tmp_path)
    values = {
        **_values(),
        "GENAUG_HABIT_CONTEXT_ENABLED": "true",
    }

    configured = LocalEnvFileAdapter(root).apply(values)
    content = (root / ".env.local").read_text(encoding="utf-8")
    assert "GENAUG_HABIT_CONTEXT_ENABLED=true" in content
    assert configured.managed_variables == (
        "GENAUG_API_KEY",
        "GENAUG_PROJECT_ID",
        "GENAUG_API_BASE_URL",
        "GENAUG_HABIT_CONTEXT_ENABLED",
    )

    removed = LocalEnvFileAdapter(root).remove()
    assert "GENAUG_HABIT_CONTEXT_ENABLED" not in (root / ".env.local").read_text(
        encoding="utf-8"
    )
    assert "GENAUG_HABIT_CONTEXT_ENABLED" in removed.managed_variables


def test_reviewed_activation_values_are_narrow_and_read_only() -> None:
    """Manifest data may select a safe flag name, never arbitrary values or authority."""
    artifact = {
        "plan": {
            "capabilities": [
                {
                    "classification": "read_only",
                    "execution_owner": "application",
                    "enable_after_review": True,
                    "activation_variable": "GENAUG_HABIT_CONTEXT_ENABLED",
                }
            ]
        }
    }
    assert reviewed_activation_environment_values(artifact) == {
        "GENAUG_HABIT_CONTEXT_ENABLED": "true"
    }

    artifact["plan"]["capabilities"][0]["classification"] = "write"
    with pytest.raises(CLIError, match="read-only application-owned"):
        reviewed_activation_environment_values(artifact)


@pytest.mark.parametrize(
    "name",
    ["NEXT_PUBLIC_GENAUG_ENABLED", "GENAUG_API_KEY_COPY", "GENAUG_BAD-NAME_ENABLED"],
)
def test_local_env_handoff_rejects_unreviewable_variable_names(
    tmp_path: Path,
    name: str,
) -> None:
    root = _repository(tmp_path)
    with pytest.raises(CLIError, match="unsupported managed variables"):
        LocalEnvFileAdapter(root).apply({**_values(), name: "true"})


def test_local_env_handoff_refuses_to_overwrite_unmanaged_genaug_values(
    tmp_path: Path,
) -> None:
    """Pre-existing values remain byte-for-byte intact when ownership is ambiguous."""
    root = _repository(tmp_path)
    target = root / ".env.local"
    original = "GENAUG_API_KEY=pre-existing-secret\nOTHER=value\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(CLIError, match="pre-existing application environment variables") as exc:
        LocalEnvFileAdapter(root).apply(_values())

    assert target.read_text(encoding="utf-8") == original
    assert "pre-existing-secret" not in str(exc.value)


def test_local_env_handoff_refuses_tracked_or_unignored_targets(tmp_path: Path) -> None:
    """Credentials must not enter a tracked or potentially committable file."""
    tracked = _repository(tmp_path / "tracked")
    target = tracked / ".env.local"
    target.write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(tracked), "add", "-f", ".env.local"], check=True)

    with pytest.raises(CLIError, match="tracked file"):
        LocalEnvFileAdapter(tracked).apply(_values())

    unignored = _repository(tmp_path / "unignored", ignored=False)
    with pytest.raises(CLIError, match="not Git-ignored"):
        LocalEnvFileAdapter(unignored).apply(_values())


def test_local_env_handoff_refuses_symlinked_target_without_exposing_secret(
    tmp_path: Path,
) -> None:
    """A repository symlink must not redirect the runtime key into public content."""
    root = _repository(tmp_path)
    public = root / "public"
    public.mkdir()
    exposed = public / "runtime.txt"
    exposed.write_text("public sentinel\n", encoding="utf-8")
    (root / ".env.local").symlink_to(exposed.relative_to(root))

    with pytest.raises(CLIError, match="symlinked") as exc:
        LocalEnvFileAdapter(root).apply(_values())

    assert exposed.read_text(encoding="utf-8") == "public sentinel\n"
    assert "ga_runtime_test_secret" not in str(exc.value)


def test_local_env_handoff_refuses_symlinked_parent(tmp_path: Path) -> None:
    """Custom ignored targets must remain inside non-symlinked repository directories."""
    root = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "secrets").symlink_to(outside, target_is_directory=True)
    (root / ".gitignore").write_text("secrets/.env.local\n", encoding="utf-8")

    with pytest.raises(CLIError, match="symlinked"):
        LocalEnvFileAdapter(root, root / "secrets" / ".env.local").apply(_values())

    assert not (outside / ".env.local").exists()


def test_local_env_handoff_creates_safe_ignored_nested_target(tmp_path: Path) -> None:
    """A legitimate custom target retains the supported atomic handoff behavior."""
    root = _repository(tmp_path)
    (root / ".gitignore").write_text("secrets/.env.local\n", encoding="utf-8")
    target = root / "secrets" / ".env.local"

    result = LocalEnvFileAdapter(root, target).apply(_values())

    assert result.status == "configured"
    assert "GENAUG_API_KEY=ga_runtime_test_secret" in target.read_text(encoding="utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_local_env_handoff_blocks_target_swap_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target swapped to a symlink after validation must still fail closed."""
    root = _repository(tmp_path)
    target = root / ".env.local"
    exposed = root / "public.txt"
    exposed.write_text("public sentinel\n", encoding="utf-8")
    original_without_managed_block = application_environment._without_managed_block

    def swap_target(lines: list[str]) -> list[str]:
        target.symlink_to(exposed.name)
        return original_without_managed_block(lines)

    monkeypatch.setattr(application_environment, "_without_managed_block", swap_target)

    with pytest.raises(CLIError, match="symlinked"):
        LocalEnvFileAdapter(root).apply(_values())

    assert exposed.read_text(encoding="utf-8") == "public sentinel\n"


def test_local_env_handoff_does_not_report_success_after_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed parent stays confined through its fd and cannot produce false success."""

    root = _repository(tmp_path)
    (root / ".gitignore").write_text("secrets/.env.local\n", encoding="utf-8")
    parent = root / "secrets"
    parent.mkdir()
    original_parent = root / "secrets-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_replace = os.replace
    swapped = False

    def swap_parent(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(original_parent)
            parent.symlink_to(outside, target_is_directory=True)
        original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", swap_parent)

    with pytest.raises(CLIError, match=r"symlinked|changed during the operation"):
        LocalEnvFileAdapter(root, parent / ".env.local").apply(_values())

    assert list(outside.iterdir()) == []
    assert not (outside / ".env.local").exists()
