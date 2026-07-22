"""Focused tests for the versioned multi-Agent launch contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from platform_cli.launch_contract import (
    MANIFEST_SCHEMA_VERSION,
    bind_launch_context,
    build_launch_manifest,
    compatibility_status,
    write_launch_manifest,
)
from platform_cli.openapi import load_deploy_payload, validate_local_agent_config


def _inspection() -> dict[str, Any]:
    return {
        "detected": {
            "frameworks": ["nextjs"],
            "language": ["typescript"],
            "package_manager": "npm",
            "auth": {"provider": "clerk"},
            "stable_user_candidates": [
                {"source": "auth().userId", "server_side": True}
            ],
            "backend_boundaries": [{"file": "app/api/assistant/route.ts"}],
            "assistant_surfaces": [{"file": "app/assistant/page.tsx"}],
            "test_commands": ["npm run typecheck"],
        }
    }


def test_default_launch_manifest_is_project_shaped_and_locally_valid(tmp_path: Path) -> None:
    manifest = build_launch_manifest(tmp_path, _inspection())
    path = write_launch_manifest(tmp_path / "genaug-agent.yaml", manifest, workspace=tmp_path)
    validation = validate_local_agent_config(path)

    assert manifest["apiVersion"] == "genaug/v2"
    assert manifest["kind"] == "Project"
    assert len(manifest["agents"]) == 1
    assert manifest["agents"][0]["entry"] is True
    assert manifest["agents"][0]["memory"] == {"user_profile": "read_write"}
    assert validation.errors == []
    assert load_deploy_payload(path)["soul_content"] is None


def test_plan_rerun_preserves_reviewed_topology_and_refreshes_detection(tmp_path: Path) -> None:
    path = tmp_path / "genaug-agent.yaml"
    first = build_launch_manifest(tmp_path, _inspection())
    first["agents"].append(
        {
            "name": "triage",
            "entry": False,
            "personality": {"role": "Triage specialist"},
            "model": dict(first["agents"][0]["model"]),
            "tools": [],
            "skills": [],
            "memory": {"user_profile": "read"},
            "delegations": [],
        }
    )
    first["agents"][0]["delegations"] = [{"to": "triage", "mode": "as_tool"}]
    write_launch_manifest(path, first, workspace=tmp_path)

    changed_inspection = _inspection()
    changed_inspection["detected"]["deployment_provider"] = "vercel"
    write_launch_manifest(
        path,
        build_launch_manifest(tmp_path, changed_inspection),
        workspace=tmp_path,
    )
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert [agent["name"] for agent in persisted["agents"]] == [
        first["agents"][0]["name"],
        "triage",
    ]
    assert persisted["agents"][0]["delegations"] == [{"to": "triage", "mode": "as_tool"}]
    assert (
        persisted["x-general-augment-launch"]["application"]["deployment_provider"]
        == "vercel"
    )


def test_review_binding_preserves_exact_project_contract(tmp_path: Path) -> None:
    path = tmp_path / "genaug-agent.yaml"
    manifest = build_launch_manifest(tmp_path, _inspection())
    manifest["agents"][0]["tools"] = ["application_context"]
    manifest["agents"][0]["skills"] = ["habit-coaching@1.0.0"]
    manifest["x-general-augment-launch"]["release"] = {
        "intent": "live",
        "activation_allowed": False,
        "requires_verified_release": True,
    }

    write_launch_manifest(path, manifest, workspace=tmp_path)
    bound = bind_launch_context(
        manifest,
        workspace_id="workspace-1",
        project_id="project-1",
    )
    write_launch_manifest(
        path,
        bound,
        workspace=tmp_path,
        preserve_reviewed_contract=True,
    )
    persisted = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert persisted["agents"] == manifest["agents"]
    assert persisted["x-general-augment-launch"]["release"]["intent"] == "live"
    assert persisted["x-general-augment-launch"]["project"] == {
        "ref": "project-1",
        "link_state": "linked",
        "workspace": {"ref": "workspace-1"},
    }


def test_compatibility_accepts_legacy_and_current_manifest_versions() -> None:
    for version in ("genaug/v1", MANIFEST_SCHEMA_VERSION):
        compatible, reasons = compatibility_status(
            cli_version="0.3.0",
            skill_version="1.0.0",
            manifest_schema_version=version,
        )
        assert compatible is True
        assert reasons == []

    compatible, reasons = compatibility_status(
        cli_version="0.3.0",
        skill_version="1.0.0",
        manifest_schema_version="genaug/v99",
    )
    assert compatible is False
    assert reasons == ["manifest_schema_incompatible"]
