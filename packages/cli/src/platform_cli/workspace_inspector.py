"""Workspace inspection helpers for self-serve onboarding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCAN_EXTENSIONS = {
    ".cjs",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".mts",
    ".py",
    ".ts",
    ".tsx",
}
PROMPT_EXTENSIONS = {".md", ".mdx", ".prompt", ".txt", ".yaml", ".yml"}
ENV_FILE_NAMES = {
    ".env",
    ".env.development",
    ".env.local",
    ".env.production",
    ".env.test",
}
SKIP_DIRS = {
    ".git",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}


def inspect_workspace(workspace: Path) -> dict[str, Any]:
    """Inspect a workspace and return a redacted, deterministic setup summary."""
    root = workspace.expanduser().resolve()
    package_json = _load_package_json(root / "package.json")
    pyproject_text = _safe_read(root / "pyproject.toml")
    code_files = _iter_files(root, SCAN_EXTENSIONS)
    responses_call_sites = _responses_call_sites(root, code_files)
    prompt_files = _prompt_files(root)
    return {
        "workspace": {
            "root": str(root),
            "package_manager": _detect_package_manager(root),
            "frameworks": _detect_frameworks(root, package_json, pyproject_text),
        },
        "detected": {
            "frameworks": _detect_frameworks(root, package_json, pyproject_text),
            "package_manager": _detect_package_manager(root),
            "env_files": _env_files(root),
            "openai": {
                "packages": _openai_packages(package_json, pyproject_text),
                "env_vars": _env_vars(root, prefix="OPENAI_"),
                "responses_api_call_count": len(responses_call_sites),
                "responses_call_sites": responses_call_sites,
                "client_wrappers": _client_wrappers(root, code_files),
            },
            "prompts": prompt_files,
            "webhooks": _matching_paths(root, code_files, "webhook"),
            "tools": _matching_paths(root, code_files, "tool"),
        },
    }


def _load_package_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _detect_package_manager(root: Path) -> str:
    candidates = [
        ("bun", "bun.lockb"),
        ("bun", "bun.lock"),
        ("pnpm", "pnpm-lock.yaml"),
        ("yarn", "yarn.lock"),
        ("npm", "package-lock.json"),
        ("uv", "uv.lock"),
    ]
    for manager, filename in candidates:
        if (root / filename).exists():
            return manager
    if (root / "package.json").exists():
        return "npm"
    if (root / "pyproject.toml").exists():
        return "uv"
    return "unknown"


def _detect_frameworks(root: Path, package_json: dict[str, Any], pyproject_text: str) -> list[str]:
    dependencies = _package_dependencies(package_json)
    frameworks: list[str] = []
    if "next" in dependencies or any(root.glob("next.config.*")):
        frameworks.append("nextjs")
    if "react" in dependencies and "nextjs" not in frameworks:
        frameworks.append("react")
    if "vite" in dependencies:
        frameworks.append("vite")
    if "fastapi" in pyproject_text.lower():
        frameworks.append("fastapi")
    return frameworks or ["unknown"]


def _package_dependencies(package_json: dict[str, Any]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        values = package_json.get(key)
        if isinstance(values, dict):
            dependencies.update({str(name): str(version) for name, version in values.items()})
    return dependencies


def _openai_packages(package_json: dict[str, Any], pyproject_text: str) -> list[str]:
    packages: list[str] = []
    if "openai" in _package_dependencies(package_json):
        packages.append("openai")
    if "openai" in pyproject_text.lower() and "openai" not in packages:
        packages.append("openai")
    return packages


def _iter_files(root: Path, extensions: set[str]) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in extensions:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def _responses_call_sites(root: Path, code_files: list[Path]) -> list[dict[str, Any]]:
    call_sites: list[dict[str, Any]] = []
    needles = ("responses.create", "/v1/responses", "responses.create(")
    for path in code_files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if any(needle in line for needle in needles):
                call_sites.append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": line_number,
                        "kind": "openai_responses",
                    }
                )
    return call_sites


def _client_wrappers(root: Path, code_files: list[Path]) -> list[dict[str, Any]]:
    wrappers: list[dict[str, Any]] = []
    for path in code_files:
        text = _safe_read(path)
        if "new OpenAI" in text or "OpenAI(" in text:
            wrappers.append({"file": str(path.relative_to(root)), "kind": "openai_client"})
    return wrappers


def _env_files(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()) if root.exists() else []:
        if path.name not in ENV_FILE_NAMES or not path.is_file():
            continue
        keys = _env_keys(path)
        files.append(
            {
                "path": path.name,
                "keys": keys,
                "secret_values_redacted": True,
            }
        )
    return files


def _env_vars(root: Path, *, prefix: str) -> list[str]:
    values: set[str] = set()
    for item in _env_files(root):
        for key in item["keys"]:
            if str(key).startswith(prefix):
                values.add(str(key))
    return sorted(values)


def _env_keys(path: Path) -> list[str]:
    keys: list[str] = []
    for line in _safe_read(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return sorted(set(keys))


def _prompt_files(root: Path) -> list[dict[str, str]]:
    prompt_paths: list[dict[str, str]] = []
    for path in _iter_files(root, PROMPT_EXTENSIONS):
        relative = path.relative_to(root)
        lowered = "/".join(relative.parts).lower()
        if "prompt" in lowered or "system" in lowered or "skill" in lowered:
            prompt_paths.append({"file": str(relative), "kind": "prompt"})
    return prompt_paths


def _matching_paths(root: Path, code_files: list[Path], needle: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for path in code_files:
        relative = path.relative_to(root)
        lowered = "/".join(relative.parts).lower()
        if needle in lowered:
            matches.append({"file": str(relative), "kind": needle})
    return matches
