"""OpenAI Responses to General Augment migration planning."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from platform_cli.workspace_inspector import SCAN_EXTENSIONS, SKIP_DIRS

GENAUG_API_BASE_URL = "https://api.generalaugment.com"
GENAUG_OPENAI_BASE_URL = f"{GENAUG_API_BASE_URL}/v1"


def plan_openai_responses_migration(
    workspace: Path,
    *,
    apply: bool,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Plan or apply safe OpenAI client config changes for Responses-compatible GA."""
    root = workspace.expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    patches: list[dict[str, Any]] = []
    diff_chunks: list[str] = []
    for file_path in _candidate_files(root):
        original = file_path.read_text(encoding="utf-8")
        migrated = migrate_source(original)
        if migrated == original:
            continue
        relative = str(file_path.relative_to(root))
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                migrated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        if apply:
            file_path.write_text(migrated, encoding="utf-8")
        diff_chunks.append(diff)
        patches.append(
            {
                "file": relative,
                "kind": "openai_responses_client_config",
                "applies_cleanly": True,
                "applied": apply,
            }
        )
    diff_path = artifact_dir / "openai-responses.patch"
    diff_path.write_text("\n".join(diff_chunks), encoding="utf-8")
    if apply:
        _ensure_env_example(root)
    return {
        "source": "openai-responses",
        "apply": apply,
        "diff_path": str(diff_path),
        "diff_files": [patch["file"] for patch in patches],
        "patches": patches,
        "env": {
            "required": [
                "GENAUG_API_KEY",
                "GENAUG_PROJECT_ID",
                "GENAUG_API_BASE_URL",
                "GENAUG_OPENAI_BASE_URL",
            ],
            "written_files": [".env.example"] if apply else [],
        },
    }


def migrate_source(source: str) -> str:
    """Return a source string using GA env and base URL for OpenAI-compatible clients."""
    if "OPENAI_API_KEY" not in source and "new OpenAI" not in source:
        return source
    updated = source.replace("process.env.OPENAI_API_KEY", "process.env.GENAUG_API_KEY")
    if "new OpenAI({" not in updated:
        return updated
    if "baseURL:" in updated or "base_url=" in updated:
        return updated
    return updated.replace(
        "new OpenAI({",
        (
            "new OpenAI({\n"
            "  baseURL: process.env.GENAUG_OPENAI_BASE_URL ?? "
            f'"{GENAUG_OPENAI_BASE_URL}",'
        ),
        1,
    )


def _candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "responses.create" in text or "new OpenAI" in text or "OPENAI_API_KEY" in text:
            files.append(path)
    return files


def _ensure_env_example(root: Path) -> None:
    path = root / ".env.example"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    required = {
        "GENAUG_API_KEY": "",
        "GENAUG_PROJECT_ID": "",
        "GENAUG_API_BASE_URL": GENAUG_API_BASE_URL,
        "GENAUG_OPENAI_BASE_URL": GENAUG_OPENAI_BASE_URL,
    }
    present = {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}
    for key, value in required.items():
        if key not in present:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
