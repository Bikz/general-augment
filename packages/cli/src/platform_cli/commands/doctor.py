"""CLI environment and platform preflight checks."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime


def doctor(
    ctx: typer.Context,
    raw: Annotated[bool, typer.Option("--json", help="Print machine-readable results.")] = False,
) -> None:
    """Check local CLI config, API reachability, and auth."""
    runtime: Runtime = ctx.obj
    checks: list[dict[str, str]] = []

    checks.append(_config_check(runtime))
    checks.append(_base_url_check(runtime))
    checks.append(_api_key_check(runtime))

    with runtime.client() as client:
        try:
            ready = client.public("GET", "/health/ready")
            checks.append(
                _check(
                    "api_ready",
                    "PASS",
                    _status_detail(ready),
                    "The platform API answered /health/ready.",
                )
            )
        except CLIError as exc:
            checks.append(
                _check(
                    "api_ready",
                    "FAIL",
                    str(exc),
                    "Check --base-url or GENAUG_ADMIN_BASE_URL, then retry.",
                )
            )

        if runtime.config.api_key:
            try:
                identity = client.admin("GET", "/me")
                project_ids = identity.get("project_ids", []) if isinstance(identity, dict) else []
                detail = (
                    f"auth_method={identity.get('auth_method', 'unknown')}, "
                    f"projects={len(project_ids or [])}"
                    if isinstance(identity, dict)
                    else "authenticated"
                )
                checks.append(
                    _check(
                        "auth",
                        "PASS",
                        detail,
                        "The configured key can call the admin API.",
                    )
                )
            except CLIError as exc:
                checks.append(
                    _check(
                        "auth",
                        "FAIL",
                        str(exc),
                        "Run genaug auth login with a valid key or fix API key env overrides.",
                    )
                )
        else:
            checks.append(
                _check(
                    "auth",
                    "FAIL",
                    "No API key configured.",
                    "Run genaug auth login or set GENAUG_ADMIN_API_KEY.",
                )
            )

    summary = {"verdict": _verdict(checks), "checks": checks}
    if raw:
        print_json(summary)
    else:
        table(
            "General Augment Doctor",
            ["Check", "Status", "Detail", "Next action"],
            [
                [item["name"], item["status"], item["detail"], item["next_action"]]
                for item in checks
            ],
        )

    if summary["verdict"] == "FAIL":
        raise typer.Exit(1)


def _config_check(runtime: Runtime) -> dict[str, str]:
    """Return the config-file check."""
    if runtime.loaded_config_path.exists():
        return _check(
            "config",
            "PASS",
            f"loaded={runtime.loaded_config_path}",
            "No action needed.",
        )
    return _check(
        "config",
        "WARN",
        f"no saved config at {runtime.loaded_config_path}",
        "Run genaug auth login to persist config, or keep using env overrides.",
    )


def _base_url_check(runtime: Runtime) -> dict[str, str]:
    """Return a base URL sanity check."""
    base_url = runtime.config.base_url.rstrip("/")
    if base_url.startswith(("http://", "https://")):
        return _check("base_url", "PASS", base_url, "No action needed.")
    return _check(
        "base_url",
        "FAIL",
        base_url or "<empty>",
        "Set --base-url or GENAUG_ADMIN_BASE_URL to an http(s) URL.",
    )


def _api_key_check(runtime: Runtime) -> dict[str, str]:
    """Return an API-key presence check without printing the key."""
    if runtime.config.api_key:
        return _check("api_key", "PASS", "configured", "No action needed.")
    return _check(
        "api_key",
        "FAIL",
        "missing",
        "Run genaug auth login or set GENAUG_ADMIN_API_KEY.",
    )


def _status_detail(payload: Any) -> str:
    """Format a compact health-check detail."""
    if isinstance(payload, dict):
        status = payload.get("status") or payload.get("state") or "unknown"
        db = payload.get("db")
        redis = payload.get("redis")
        dependencies = ", ".join(str(item) for item in (db, redis) if item)
        return f"status={status}" + (f", {dependencies}" if dependencies else "")
    return str(payload)


def _check(name: str, status: str, detail: str, next_action: str) -> dict[str, str]:
    """Return one doctor check row."""
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "next_action": next_action,
    }


def _verdict(checks: list[dict[str, str]]) -> str:
    """Return the aggregate doctor verdict."""
    if any(item["status"] == "FAIL" for item in checks):
        return "FAIL"
    if any(item["status"] == "WARN" for item in checks):
        return "WARN"
    return "PASS"
