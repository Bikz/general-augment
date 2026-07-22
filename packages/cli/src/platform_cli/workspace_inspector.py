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
    auth = _detect_auth(root, package_json, code_files)
    stable_user_candidates = _stable_user_candidates(root, code_files)
    backend_boundaries = _backend_boundaries(root, code_files)
    assistant_surfaces = _assistant_surfaces(root, code_files)
    api_descriptions = _api_descriptions(root)
    test_commands = _test_commands(package_json)
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
            "language": _detect_language(package_json, pyproject_text),
            "auth": auth,
            "stable_user_candidates": stable_user_candidates,
            "backend_boundaries": backend_boundaries,
            "assistant_surfaces": assistant_surfaces,
            "api_descriptions": api_descriptions,
            "deployment_provider": _deployment_provider(root),
            "environment_variables": _all_env_vars(root),
            "test_commands": test_commands,
            "risks": _inspection_risks(auth, stable_user_candidates, backend_boundaries),
        },
    }


def _detect_language(package_json: dict[str, Any], pyproject_text: str) -> list[str]:
    languages: list[str] = []
    dependencies = _package_dependencies(package_json)
    if "typescript" in dependencies or package_json:
        languages.append("typescript" if "typescript" in dependencies else "javascript")
    if pyproject_text:
        languages.append("python")
    return languages or ["unknown"]


def _detect_auth(
    root: Path,
    package_json: dict[str, Any],
    code_files: list[Path],
) -> dict[str, Any]:
    dependencies = _package_dependencies(package_json)
    provider = "unknown"
    evidence: list[dict[str, str]] = []
    dependency_candidates = (
        ("clerk", ("@clerk/nextjs", "@clerk/clerk-react")),
        ("authjs", ("next-auth", "@auth/core")),
        ("supabase", ("@supabase/ssr", "@supabase/supabase-js")),
    )
    for candidate, packages in dependency_candidates:
        installed = next((name for name in packages if name in dependencies), None)
        if installed:
            provider = candidate
            evidence.append({"kind": "dependency", "value": installed})
            break
    patterns = {
        "clerk": ("auth()", "currentUser(", "clerkMiddleware("),
        "authjs": ("getServerSession(", "NextAuth(", "auth("),
        "supabase": ("createServerClient(", "supabase.auth.getUser("),
    }
    for path in code_files:
        text = _safe_read(path)
        for candidate, needles in patterns.items():
            if any(needle in text for needle in needles):
                if provider == "unknown":
                    provider = candidate
                if candidate == provider and len(evidence) < 12:
                    evidence.append(
                        {"kind": "server_usage", "file": str(path.relative_to(root))}
                    )
                break
    return {"provider": provider, "server_side_evidence": evidence}


def _stable_user_candidates(root: Path, code_files: list[Path]) -> list[dict[str, Any]]:
    patterns = (
        ("clerk_user_id", ("userId", "auth().userId")),
        ("session_user_id", ("session.user.id", "session?.user?.id")),
        ("supabase_user_id", ("user.id", "user?.id")),
    )
    candidates: list[dict[str, Any]] = []
    for path in code_files:
        lines = _safe_read(path).splitlines()
        for line_number, line in enumerate(lines, start=1):
            for source, needles in patterns:
                if any(needle in line for needle in needles):
                    candidates.append(
                        {
                            "source": source,
                            "file": str(path.relative_to(root)),
                            "line": line_number,
                            "server_side": _server_side_path(path.relative_to(root)),
                        }
                    )
                    break
            if len(candidates) >= 30:
                return candidates
    return candidates


def _server_side_path(relative: Path) -> bool:
    value = "/".join(relative.parts).lower()
    return any(
        marker in value
        for marker in ("/api/", "route.", "server", "action", "middleware", "lib/auth")
    )


def _backend_boundaries(root: Path, code_files: list[Path]) -> list[dict[str, str]]:
    boundaries: list[dict[str, str]] = []
    for path in code_files:
        relative = path.relative_to(root)
        value = "/".join(relative.parts).lower()
        kind = None
        if "/api/" in value and path.stem == "route":
            kind = "next_route_handler"
        elif "actions" in relative.parts or "action" in path.stem.lower():
            kind = "server_action"
        elif any(part in {"server", "services", "service"} for part in relative.parts):
            kind = "server_service"
        if kind:
            boundaries.append({"file": str(relative), "kind": kind})
        if len(boundaries) >= 50:
            break
    return boundaries


def _assistant_surfaces(root: Path, code_files: list[Path]) -> list[dict[str, str]]:
    markers = ("chat", "assistant", "copilot", "agent")
    ranked: list[tuple[int, str, dict[str, str]]] = []
    for path in code_files:
        relative = path.relative_to(root)
        if _is_test_source(relative):
            continue
        value = "/".join(relative.parts).lower()
        if any(marker in value for marker in markers):
            browser_surface = path.suffix.lower() in {".jsx", ".tsx"} and "/api/" not in value
            ranked.append(
                (
                    0 if browser_surface else 1,
                    str(relative),
                    {
                        "file": str(relative),
                        "kind": "in_app_assistant" if browser_surface else "backend_candidate",
                    },
                )
            )
    return [item for _, _, item in sorted(ranked)[:30]]


def _is_test_source(relative: Path) -> bool:
    """Exclude tests and stories from product integration-point detection."""
    filename = relative.name.lower()
    return (
        any(part.lower() in {"__tests__", "test", "tests"} for part in relative.parts[:-1])
        or any(marker in filename for marker in (".test.", ".spec.", ".stories."))
    )


def _api_descriptions(root: Path) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for pattern, kind in (
        ("**/openapi*.json", "openapi"),
        ("**/openapi*.yaml", "openapi"),
        ("**/openapi*.yml", "openapi"),
    ):
        for path in sorted(root.glob(pattern)):
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            candidates.append({"file": str(path.relative_to(root)), "kind": kind})
    return candidates[:20]


def _deployment_provider(root: Path) -> str:
    if (root / "vercel.json").exists() or any(root.glob("next.config.*")):
        return "vercel_compatible"
    if (root / "netlify.toml").exists():
        return "netlify"
    if (root / "Dockerfile").exists():
        return "container"
    return "unknown"


def _all_env_vars(root: Path) -> list[str]:
    values: set[str] = set()
    for item in _env_files(root):
        values.update(str(key) for key in item["keys"])
    return sorted(values)


def _test_commands(package_json: dict[str, Any]) -> list[str]:
    scripts = package_json.get("scripts")
    if not isinstance(scripts, dict):
        return []
    preferred = ("test", "typecheck", "lint", "build", "test:e2e", "e2e")
    return [f"npm run {name}" for name in preferred if isinstance(scripts.get(name), str)]


def _inspection_risks(
    auth: dict[str, Any],
    stable_user_candidates: list[dict[str, Any]],
    backend_boundaries: list[dict[str, str]],
) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    if auth.get("provider") == "unknown":
        risks.append({"code": "auth_provider_unknown", "severity": "blocking"})
    if not any(item.get("server_side") for item in stable_user_candidates):
        risks.append({"code": "stable_server_user_id_unresolved", "severity": "blocking"})
    if not backend_boundaries:
        risks.append({"code": "backend_integration_point_unresolved", "severity": "warning"})
    return risks


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
