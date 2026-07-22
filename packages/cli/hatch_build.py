"""Include the canonical launch skill in CLI source and wheel artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

SKILL_NAME = "generalaugment-launch"


class LaunchSkillBuildHook(BuildHookInterface):
    """Select the canonical skill path for source and isolated wheel builds."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Add the skill without creating a second repository copy."""

        del version
        root = Path(self.root).resolve()
        repository_skill = (root / ".." / ".." / "skills" / SKILL_NAME).resolve()
        sdist_skill = root / "skills" / SKILL_NAME
        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("Hatch force_include build data must be a mapping.")

        if self.target_name == "sdist":
            source = repository_skill
            destination = f"skills/{SKILL_NAME}"
        elif self.target_name == "wheel":
            source = repository_skill if repository_skill.is_dir() else sdist_skill
            destination = f"platform_cli/bundled_skills/{SKILL_NAME}"
        else:
            return

        if not source.is_dir() or not (source / "integrity.json").is_file():
            raise FileNotFoundError(
                f"Canonical {SKILL_NAME} skill and integrity metadata are required."
            )
        force_include[str(source)] = destination
