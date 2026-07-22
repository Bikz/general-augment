"""Tests for structured launch questions and coding-agent answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from platform_cli.errors import CLIError
from platform_cli.launch_contract import build_launch_manifest
from platform_cli.launch_guidance import (
    apply_launch_answers,
    launch_questions,
    load_launch_answers,
)
from platform_cli.openapi import validate_local_agent_config


def _inspection(*, provider: str = "clerk") -> dict[str, object]:
    return {
        "detected": {
            "frameworks": ["nextjs"],
            "language": ["typescript"],
            "auth": {"provider": provider},
            "stable_user_candidates": (
                [{"source": "auth().userId", "server_side": True}]
                if provider != "unknown"
                else []
            ),
        }
    }


def test_questions_are_structured_and_account_scoped() -> None:
    payload = launch_questions(
        _inspection(provider="unknown"),
        authenticated=True,
        active_workspace=None,
        active_project=None,
        workspaces=[
            {"id": "workspace-1", "name": "Health", "slug": "health"},
            {"id": "workspace-2", "name": "Developer tools", "slug": "devtools"},
        ],
        projects=[],
    )

    assert payload["status"] == "USER_INPUT_REQUIRED"
    by_id = {row["id"]: row for row in payload["questions"]}
    assert by_id["workspace"]["required"] is True
    assert by_id["workspace"]["options"][0] == {
        "id": "workspace-1",
        "name": "Health",
        "slug": "health",
    }
    assert by_id["application_auth"]["required"] is True
    assert by_id["agent_topology"]["required"] is False
    assert payload["source_upload"] is False


def test_answers_create_valid_multi_agent_manifest(tmp_path: Path) -> None:
    skill_directory = tmp_path / "skills" / "habit-coaching"
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text(
        "---\nname: habit-coaching\ndescription: Coach from reviewed habit data.\n---\n\n"
        "Use only the signed-in user's habit context.\n",
        encoding="utf-8",
    )
    answers_path = tmp_path / "launch-answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "schema_version": "general-augment-launch-answers/v1",
                "workspace": {"ref": "health"},
                "project": {"create": True, "name": "Habit App", "slug": "habit-app"},
                "skills_directory": "skills",
                "release_intent": "test",
                "agents": [
                    {
                        "name": "concierge",
                        "purpose": "Help users understand their habits.",
                        "entry": True,
                        "tools": ["web_search"],
                        "skills": ["habit-coaching"],
                        "memory": {"profile": "read_write"},
                        "delegations": [{"to": "coach", "mode": "as_tool"}],
                    },
                    {
                        "name": "coach",
                        "purpose": "Provide focused habit coaching.",
                        "tools": ["web_search"],
                        "memory": {"profile": "read"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    answers = load_launch_answers(tmp_path, answers_path)
    manifest = apply_launch_answers(
        build_launch_manifest(tmp_path, _inspection()),
        answers,
    )
    manifest_path = tmp_path / "genaug-agent.yaml"
    manifest_path.write_text(
        __import__("yaml").safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    validation = validate_local_agent_config(manifest_path)

    assert validation.errors == []
    assert [agent["name"] for agent in manifest["agents"]] == ["concierge", "coach"]
    assert manifest["tools"]["builtin"] == ["web_search"]
    assert manifest["skills"] == {
        "directory": "skills",
        "learning_enabled": False,
    }
    assert manifest["agents"][0]["skills"] == ["habit-coaching"]
    assert manifest["x-general-augment-launch"]["project"]["workspace"] == {
        "ref": "health",
        "create": False,
    }
    assert manifest["x-general-augment-launch"]["release"]["intent"] == "test"


def test_answers_reject_skill_assignment_without_project_catalog(tmp_path: Path) -> None:
    answers_path = tmp_path / "launch-answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "assistant",
                        "purpose": "Help the signed-in user.",
                        "entry": True,
                        "skills": ["habit-coaching"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CLIError, match="skills_directory is required"):
        load_launch_answers(tmp_path, answers_path)


def test_answers_reject_skill_directory_escape(tmp_path: Path) -> None:
    answers_path = tmp_path / "launch-answers.json"
    answers_path.write_text(
        json.dumps({"skills_directory": "../shared-skills"}),
        encoding="utf-8",
    )

    with pytest.raises(CLIError, match="inside the application repository"):
        load_launch_answers(tmp_path, answers_path)
