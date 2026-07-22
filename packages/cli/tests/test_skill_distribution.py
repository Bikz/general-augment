"""Tests for checkout-free launch-skill distribution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from platform_cli.errors import CLIError
from platform_cli.launch_contract import LAUNCH_SKILL_VERSION as CONTRACT_SKILL_VERSION
from platform_cli.launch_verification import (
    SUPPORTED_API_MAXIMUM_EXCLUSIVE,
    SUPPORTED_API_MINIMUM,
)
from platform_cli.main import app
from platform_cli.skill_distribution import (
    LAUNCH_SKILL_VERSION,
    install_launch_skill,
    launch_skill_status,
    remove_launch_skill,
    verify_skill_bundle,
)


def _source() -> Path:
    return Path(__file__).resolve().parents[3] / "skills" / "generalaugment-launch"


def test_canonical_skill_bundle_integrity_is_current() -> None:
    integrity = verify_skill_bundle(_source())
    compatibility = json.loads(
        (_source() / "references" / "compatibility.json").read_text(encoding="utf-8")
    )

    assert integrity["skill_version"] == LAUNCH_SKILL_VERSION
    assert compatibility["skill_version"] == LAUNCH_SKILL_VERSION
    assert CONTRACT_SKILL_VERSION == LAUNCH_SKILL_VERSION
    assert compatibility["hosted_api"] == {
        "minimum": ".".join(str(part) for part in SUPPORTED_API_MINIMUM),
        "maximum_exclusive": ".".join(
            str(part) for part in SUPPORTED_API_MAXIMUM_EXCLUSIVE
        ),
    }
    assert "SKILL.md" in integrity["files"]
    assert "references/distribution.md" in integrity["files"]


def test_nextjs_skill_reference_pins_the_canonical_project_header() -> None:
    guidance = (_source() / "references" / "nextjs-clerk.md").read_text(encoding="utf-8")

    assert "X-Project-ID: ${GENAUG_PROJECT_ID}" in guidance
    assert "X-General-Augment-Project-ID" not in guidance


def test_skill_compatibility_script_checks_hosted_api_range() -> None:
    script = _source() / "scripts" / "check_versions.py"
    compatible = subprocess.run(
        [
            sys.executable,
            str(script),
            "--cli",
            "0.3.0",
            "--api",
            "0.1.0",
            "--skill",
            "1.2.0",
            "--manifest",
            "genaug/v1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    incompatible = subprocess.run(
        [
            sys.executable,
            str(script),
            "--cli",
            "0.3.0",
            "--api",
            "0.2.0",
            "--skill",
            "1.2.0",
            "--manifest",
            "genaug/v1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert compatible.returncode == 0
    assert json.loads(compatible.stdout) == {"compatible": True, "reason_codes": []}
    assert incompatible.returncode == 1
    assert json.loads(incompatible.stdout)["reason_codes"] == [
        "hosted_api_version_incompatible"
    ]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_install_upgrade_status_and_remove_managed_skill(tmp_path: Path, agent: str) -> None:
    first = install_launch_skill(
        source=_source(),
        agent=agent,  # type: ignore[arg-type]
        scope="project",
        workspace=tmp_path,
        requested_version=LAUNCH_SKILL_VERSION,
    )
    installed = Path(first["path"])

    assert first["action"] == "installed"
    assert (installed / "SKILL.md").is_file()
    assert first["bundle_sha256"]
    second = install_launch_skill(
        source=_source(),
        agent=agent,  # type: ignore[arg-type]
        scope="project",
        workspace=tmp_path,
        requested_version=LAUNCH_SKILL_VERSION,
    )
    status = launch_skill_status(
        source=_source(),
        agent=agent,  # type: ignore[arg-type]
        scope="project",
        workspace=tmp_path,
    )

    assert second["action"] == "upgraded"
    assert status["managed"] is True
    assert status["installed_integrity"] == "verified"
    removed = remove_launch_skill(
        agent=agent,  # type: ignore[arg-type]
        scope="project",
        workspace=tmp_path,
    )
    assert removed["action"] == "removed"
    assert not installed.exists()


def test_integrity_failure_blocks_install(tmp_path: Path) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(_source(), source)
    (source / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(CLIError, match="integrity check failed"):
        install_launch_skill(
            source=source,
            agent="codex",
            scope="project",
            workspace=tmp_path / "customer",
            requested_version=LAUNCH_SKILL_VERSION,
        )


def test_integrity_allows_generated_python_cache_but_rejects_other_extra_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bundle"
    shutil.copytree(_source(), source)
    cache = source / "scripts" / "__pycache__"
    cache.mkdir()
    (cache / "check_versions.cpython-312.pyc").write_bytes(b"synthetic interpreter cache")

    verify_skill_bundle(source)

    (source / "undeclared.txt").write_text("not part of the signed bundle\n", encoding="utf-8")
    with pytest.raises(CLIError, match="file set does not match"):
        verify_skill_bundle(source)


def test_unmanaged_destination_is_not_overwritten_or_removed(tmp_path: Path) -> None:
    target = tmp_path / ".codex" / "skills" / "generalaugment-launch"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("customer-owned\n", encoding="utf-8")

    with pytest.raises(CLIError, match="Refusing to replace unmanaged"):
        install_launch_skill(
            source=_source(),
            agent="codex",
            scope="project",
            workspace=tmp_path,
            requested_version=LAUNCH_SKILL_VERSION,
        )
    with pytest.raises(CLIError, match="Refusing to remove unmanaged"):
        remove_launch_skill(agent="codex", scope="project", workspace=tmp_path)
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "customer-owned\n"


def test_cli_installs_both_project_skills_without_exposing_bundle_contents(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "launch-skill",
            "install",
            "--agent",
            "all",
            "--scope",
            "project",
            "--workspace",
            str(tmp_path),
            "--version",
            LAUNCH_SKILL_VERSION,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["agent"] for row in payload["results"]] == ["codex", "claude"]
    assert all(row["integrity"] == "verified" for row in payload["results"])
    assert (tmp_path / ".codex" / "skills" / "generalaugment-launch" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "generalaugment-launch" / "SKILL.md").is_file()


def test_cli_status_is_read_only(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "launch-skill",
            "status",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert all(row["installed"] is False for row in payload["results"])
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("agent,vendor", [("codex", ".codex"), ("claude", ".claude")])
@pytest.mark.parametrize("operation", ["install", "status", "remove"])
@pytest.mark.parametrize("symlink_level", ["vendor", "skills", "target"])
def test_project_skill_operations_refuse_symlinked_parent(
    tmp_path: Path,
    agent: str,
    vendor: str,
    operation: str,
    symlink_level: str,
) -> None:
    """Project operations must reject every symlink level through the target leaf."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    vendor_path = workspace / vendor
    skills_path = vendor_path / "skills"
    target_path = skills_path / "generalaugment-launch"
    if symlink_level == "vendor":
        vendor_path.symlink_to(outside, target_is_directory=True)
    elif symlink_level == "skills":
        vendor_path.mkdir()
        skills_path.symlink_to(outside, target_is_directory=True)
    else:
        skills_path.mkdir(parents=True)
        target_path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CLIError, match="symlinked"):
        if operation == "install":
            install_launch_skill(
                source=_source(),
                agent=agent,  # type: ignore[arg-type]
                scope="project",
                workspace=workspace,
                requested_version=LAUNCH_SKILL_VERSION,
            )
        elif operation == "status":
            launch_skill_status(
                source=_source(),
                agent=agent,  # type: ignore[arg-type]
                scope="project",
                workspace=workspace,
            )
        else:
            remove_launch_skill(
                agent=agent,  # type: ignore[arg-type]
                scope="project",
                workspace=workspace,
            )

    assert list(outside.iterdir()) == []


def test_cli_project_skill_install_refuses_symlinked_parent(tmp_path: Path) -> None:
    """The public CLI must surface containment failure without writing outside the app."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".codex").symlink_to(outside, target_is_directory=True)

    result = CliRunner().invoke(
        app,
        [
            "launch-skill",
            "install",
            "--agent",
            "codex",
            "--scope",
            "project",
            "--workspace",
            str(tmp_path),
            "--version",
            LAUNCH_SKILL_VERSION,
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "symlinked" in result.output.lower()
    assert list(outside.iterdir()) == []


def test_project_skill_upgrade_parent_swap_cannot_mutate_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent swap after validation must keep every rename on the opened directory."""

    workspace = tmp_path / "app"
    workspace.mkdir()
    install_launch_skill(
        source=_source(),
        agent="codex",
        scope="project",
        workspace=workspace,
        requested_version=LAUNCH_SKILL_VERSION,
    )
    parent = workspace / ".codex" / "skills"
    original_parent = workspace / ".codex" / "skills-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "generalaugment-launch"
    victim.mkdir()
    (victim / "victim.txt").write_text("must remain\n", encoding="utf-8")
    original_replace = os.replace
    swapped = False

    def swap_before_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(original_parent)
            parent.symlink_to(outside, target_is_directory=True)
        original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", swap_before_replace)

    with pytest.raises(CLIError, match=r"symlinked|changed during the operation"):
        install_launch_skill(
            source=_source(),
            agent="codex",
            scope="project",
            workspace=workspace,
            requested_version=LAUNCH_SKILL_VERSION,
        )

    assert (victim / "victim.txt").read_text(encoding="utf-8") == "must remain\n"
    assert sorted(path.name for path in outside.iterdir()) == ["generalaugment-launch"]
    restored = original_parent / "generalaugment-launch"
    assert restored.is_dir()


def test_project_skill_status_rejects_internal_symlink_without_reading_target(
    tmp_path: Path,
) -> None:
    """Integrity status treats a declared-file symlink as invalid and never follows it."""

    workspace = tmp_path / "app"
    workspace.mkdir()
    install_launch_skill(
        source=_source(),
        agent="claude",
        scope="project",
        workspace=workspace,
        requested_version=LAUNCH_SKILL_VERSION,
    )
    target = workspace / ".claude" / "skills" / "generalaugment-launch"
    victim = tmp_path / "victim.txt"
    victim.write_text("outside sentinel\n", encoding="utf-8")
    installed_file = target / "SKILL.md"
    installed_file.unlink()
    installed_file.symlink_to(victim)

    status = launch_skill_status(
        source=_source(),
        agent="claude",
        scope="project",
        workspace=workspace,
    )

    assert status["installed_integrity"] == "invalid"
    assert victim.read_text(encoding="utf-8") == "outside sentinel\n"
    remove_launch_skill(agent="claude", scope="project", workspace=workspace)
    assert victim.read_text(encoding="utf-8") == "outside sentinel\n"
