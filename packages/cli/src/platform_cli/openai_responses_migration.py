"""OpenAI Responses to General Augment migration planning."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from platform_cli.workspace_inspector import SCAN_EXTENSIONS, SKIP_DIRS

GENAUG_API_BASE_URL = "https://api.generalaugment.com"
GENAUG_OPENAI_BASE_URL = f"{GENAUG_API_BASE_URL}/v1"

# The single OpenAI env var we treat as safe to redirect to General Augment.
STANDARD_OPENAI_ENV_VAR = "OPENAI_API_KEY"
GENAUG_ENV_VAR = "GENAUG_API_KEY"

TODO_KEY = "TODO(genaug): set GENAUG_API_KEY for the General Augment endpoint"

PY_SUFFIXES = {".py"}


def plan_openai_responses_migration(
    workspace: Path,
    *,
    apply: bool,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Plan or apply safe OpenAI client config changes for Responses-compatible General Augment."""
    root = workspace.expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    patches: list[dict[str, Any]] = []
    diff_chunks: list[str] = []
    warnings: list[str] = []
    candidates = _candidate_files(root)
    for file_path in candidates:
        original = file_path.read_text(encoding="utf-8")
        migrated, file_warnings = migrate_source(
            original, is_python=file_path.suffix in PY_SUFFIXES
        )
        relative = str(file_path.relative_to(root))
        for message in file_warnings:
            warnings.append(f"{relative}: {message}")
        if migrated == original:
            continue
        diff_chunks.append(_diff(original, migrated, relative))
        if apply:
            file_path.write_text(migrated, encoding="utf-8")
        patches.append(
            {
                "file": relative,
                "kind": "openai_responses_client_config",
                "applies_cleanly": True,
                "applied": apply,
            }
        )

    # Env changes are part of the migration and must show up in the dry-run diff,
    # not just on --apply. Plan them the same way regardless of mode, but only
    # when an OpenAI client actually exists -- otherwise there is nothing to wire
    # to General Augment and we must not invent env churn.
    env_patches: list[dict[str, Any]] = []
    if candidates:
        env_patches, env_diffs = _plan_env_files(root, apply=apply)
        patches.extend(env_patches)
        diff_chunks.extend(env_diffs)

    # The .patch artifact lives under .genaug/ (artifact_dir) and is written in
    # both dry-run and apply so the previewed diff matches what --apply does.
    diff_path = artifact_dir / "openai-responses.patch"
    diff_path.write_text("\n".join(diff_chunks), encoding="utf-8")

    changed_files = [patch["file"] for patch in patches]
    return {
        "source": "openai-responses",
        "apply": apply,
        "changed": bool(changed_files),
        "diff_path": str(diff_path),
        "diff_files": changed_files,
        "patches": patches,
        "warnings": warnings,
        "env": {
            "required": [
                "GENAUG_API_KEY",
                "GENAUG_PROJECT_ID",
                "GENAUG_API_BASE_URL",
                "GENAUG_OPENAI_BASE_URL",
            ],
            "written_files": [p["file"] for p in env_patches] if apply else [],
        },
    }


def migrate_source(source: str, *, is_python: bool = False) -> tuple[str, list[str]]:
    """Return a source rewritten to use the GA endpoint, plus any warnings.

    This is a conservative regex transform, not a parser. When the existing key
    cannot be safely redirected (e.g. a non-standard env var or a literal), it
    leaves a TODO and refrains from pointing a foreign secret at GA.
    """
    if is_python:
        return _migrate_python(source)
    return _migrate_js(source)


def _migrate_js(source: str) -> tuple[str, list[str]]:
    """Rewrite JS/TS OpenAI clients to the GA endpoint."""
    if "new OpenAI" not in source:
        # No OpenAI client constructed here; do not touch unrelated env usages.
        return source, []

    warnings: list[str] = []
    updated = source

    # Positional constructor: new OpenAI(process.env.OPENAI_API_KEY) / new OpenAI("sk-...").
    # The object form below cannot inject a baseURL here, so warn and skip rather
    # than half-migrate (swap the key but leave traffic pointed at OpenAI).
    positional = re.search(r"new\s+OpenAI\s*\(\s*(?!\{)", updated)
    if positional is not None:
        warnings.append(
            "OpenAI client uses a positional argument; cannot safely inject the GA "
            "baseURL. Migrate manually to `new OpenAI({ baseURL, apiKey })`."
        )
        return updated, warnings

    if "new OpenAI({" not in updated:
        return updated, warnings

    # Determine how the key is supplied so we don't ship a foreign secret to GA.
    apikey_match = re.search(r"apiKey\s*:\s*([^,\n}]+)", updated)
    safe_to_redirect = False
    if apikey_match is not None:
        expr = apikey_match.group(1).strip()
        if expr == f"process.env.{STANDARD_OPENAI_ENV_VAR}":
            safe_to_redirect = True
        else:
            warnings.append(
                "OpenAI client uses a non-standard API key "
                f"(`{expr}`); not redirecting it to General Augment. "
                f"Set {GENAUG_ENV_VAR} and update the client manually."
            )
    else:
        # apiKey resolved from the default OPENAI_API_KEY env var implicitly.
        if f"process.env.{STANDARD_OPENAI_ENV_VAR}" in updated:
            safe_to_redirect = True

    if safe_to_redirect:
        updated = updated.replace(
            f"process.env.{STANDARD_OPENAI_ENV_VAR}",
            f"process.env.{GENAUG_ENV_VAR}",
        )

    if "baseURL:" in updated or "baseUrl:" in updated:
        # Existing baseURL; do not clobber. Key may already be handled above.
        return updated, warnings

    # Each injected fragment ends with a newline + indent so it never merges with
    # (or comments out) whatever followed `new OpenAI({` on the same line.
    todo = "" if safe_to_redirect else f"/* {TODO_KEY} */\n  "
    updated = updated.replace(
        "new OpenAI({",
        (
            "new OpenAI({\n"
            "  baseURL: process.env.GENAUG_OPENAI_BASE_URL ?? "
            f'"{GENAUG_OPENAI_BASE_URL}",\n'
            f"  {todo}"
        ),
        1,
    )
    return updated, warnings


_PY_CLIENT_RE = re.compile(r"(?:openai\.)?(?:Async)?OpenAI\s*\(")


def _migrate_python(source: str) -> tuple[str, list[str]]:
    """Rewrite Python OpenAI clients to the GA endpoint."""
    match = _PY_CLIENT_RE.search(source)
    if match is None:
        # No OpenAI client constructed here; leave unrelated env usages alone.
        return source, []

    warnings: list[str] = []
    updated = source
    key_redirected = False

    # Identify any explicit API key env var read near the client.
    env_vars = {
        m.group(1)
        for m in re.finditer(
            r"(?:os\.environ\[|os\.getenv\()\s*[\"']([A-Z0-9_]+)[\"']", updated
        )
    }
    if STANDARD_OPENAI_ENV_VAR in updated:
        # Only redirect the standard env var; never repoint a foreign secret at GA.
        updated = updated.replace(STANDARD_OPENAI_ENV_VAR, GENAUG_ENV_VAR)
        key_redirected = True
        for name in sorted(env_vars):
            if name != STANDARD_OPENAI_ENV_VAR:
                warnings.append(
                    "OpenAI client also reads a non-standard API key env var "
                    f"(`{name}`); left untouched. Set {GENAUG_ENV_VAR} if it should "
                    "point at General Augment."
                )
    elif env_vars:
        # Explicit non-standard key env var(s) only: do not redirect them to GA.
        for name in sorted(env_vars):
            warnings.append(
                "OpenAI client uses a non-standard API key env var "
                f"(`{name}`); not redirecting it to General Augment. "
                f"Set {GENAUG_ENV_VAR} and update the client manually."
            )
    elif re.search(r"api_key\s*=\s*[\"']", updated):
        # Literal key; do not ship it to GA.
        warnings.append(
            "OpenAI client uses a literal API key; not redirecting it to "
            f"General Augment. Set {GENAUG_ENV_VAR} and update the client manually."
        )
    else:
        # No explicit key: SDK resolves OPENAI_API_KEY implicitly. Switching the
        # base_url is the load-bearing change; the key env var is handled in .env.
        key_redirected = True

    if "base_url" in updated:
        # Existing base_url; do not clobber.
        return updated, warnings

    # Inject base_url right after the (possibly namespaced) OpenAI( opening paren.
    # Every fragment is newline-terminated so any inline args that followed the
    # opening paren move to their own line and are never swallowed by a comment.
    insert_at = match.end()
    todo = "" if key_redirected else f"# {TODO_KEY}\n    "
    injection = (
        "\n    base_url=os.environ.get("
        f'"GENAUG_OPENAI_BASE_URL", "{GENAUG_OPENAI_BASE_URL}"),\n    {todo}'
    )
    # Drop redundant indentation if the original arg already started a new line.
    trailing = updated[insert_at:]
    if trailing[:1] in {"\n", "\r"}:
        injection = injection.rstrip(" ")
    updated = updated[:insert_at] + injection + updated[insert_at:]

    if not re.search(r"^\s*import os\b", updated, re.MULTILINE):
        # base_url injection references os; ensure it is imported.
        updated = "import os\n" + updated

    return updated, warnings


def _diff(original: str, migrated: str, relative: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            migrated.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
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
        # Only consider files that actually construct an OpenAI client, so we do
        # not clobber unrelated OPENAI_API_KEY usages elsewhere.
        if "new OpenAI" in text or _PY_CLIENT_RE.search(text):
            files.append(path)
    return files


def _plan_env_files(root: Path, *, apply: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Plan (and optionally apply) .env.example additions, returning diffs.

    The diff is produced identically for dry-run and apply so the preview is
    faithful; only the write happens behind ``apply``.
    """
    patches: list[dict[str, Any]] = []
    diffs: list[str] = []
    path = root / ".env.example"
    relative = ".env.example"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    migrated = _env_example_with_genaug(original)
    if migrated == original:
        return patches, diffs
    diffs.append(_diff(original, migrated, relative))
    if apply:
        path.write_text(migrated, encoding="utf-8")
    patches.append(
        {
            "file": relative,
            "kind": "env_example",
            "applies_cleanly": True,
            "applied": apply,
        }
    )
    return patches, diffs


def _env_example_with_genaug(existing: str) -> str:
    """Return .env.example content with the required GENAUG_* keys appended."""
    lines = existing.splitlines()
    required = {
        "GENAUG_API_KEY": "",
        "GENAUG_PROJECT_ID": "",
        "GENAUG_API_BASE_URL": GENAUG_API_BASE_URL,
        "GENAUG_OPENAI_BASE_URL": GENAUG_OPENAI_BASE_URL,
    }
    present = {
        line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")
    }
    for key, value in required.items():
        if key not in present:
            lines.append(f"{key}={value}")
    return "\n".join(lines).rstrip() + "\n"
