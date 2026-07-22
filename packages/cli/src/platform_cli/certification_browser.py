"""Semantic validation for real-browser hosted certification evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from platform_cli.errors import CLIError


def validate_browser_evidence(
    browser: Mapping[str, Any],
    deployment: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> None:
    if browser.get("authentication_mode") != "two_real_clerk_test_users":
        raise CLIError("Browser certification must exercise two real Clerk test users.")
    required_browser_checks = {
        "application_browser_smoke",
        "read_only_application_capability",
        "memory_write_recall",
        "cross_user_memory_isolation",
        "secret_not_browser_visible",
        "app_owned_write_confirmation",
    }
    checks = browser.get("checks")
    if not isinstance(checks, list):
        raise CLIError("Browser certification evidence is missing its checks.")
    by_name = {
        str(row.get("name")): row
        for row in checks
        if isinstance(row, Mapping)
    }
    if not required_browser_checks.issubset(by_name):
        raise CLIError("Browser certification evidence is missing a required proof.")
    if any(by_name[name].get("status") != "PASS" for name in required_browser_checks):
        raise CLIError("Every required browser certification check must PASS.")

    smoke = _browser_check_evidence(by_name, "application_browser_smoke")
    identifiers = _mapping(deployment, "identifiers")
    for name in ("response_id", "trace_id"):
        if smoke.get(name) != identifiers.get(name):
            raise CLIError(f"Browser evidence {name} does not match deployment evidence.")
    attempt_id = str(browser.get("verification_attempt_id") or "")
    command_evidence = _check_evidence(verification, "application_browser_smoke")
    if not attempt_id or command_evidence.get("verification_attempt_id") != attempt_id:
        raise CLIError("Browser evidence is not bound to the current verification attempt.")
    if browser.get("manifest_fingerprint") != verification.get("manifest_fingerprint"):
        raise CLIError("Browser evidence is not bound to the verified manifest.")
    launch_evidence = _check_evidence(verification, "launch_session_approved")
    if browser.get("approved_session_fingerprint") != launch_evidence.get(
        "manifest_fingerprint"
    ):
        raise CLIError("Browser evidence is not bound to the approved launch session.")

    capability = _browser_check_evidence(by_name, "read_only_application_capability")
    expected_capability = {
        "capability": "habit_list_context",
        "classification": "read_only",
        "execution_owner": "application",
        "identity_binding": "authenticated_server_user",
        "response_id": smoke.get("response_id"),
        "trace_id": smoke.get("trace_id"),
    }
    if any(capability.get(name) != value for name, value in expected_capability.items()):
        raise CLIError("Browser capability evidence is incomplete or unbound.")

    memory = _browser_check_evidence(by_name, "memory_write_recall")
    if not (
        _identifier(memory.get("response_id"))
        and _identifier(memory.get("trace_id"))
        and _sha256(memory.get("marker_digest"))
    ):
        raise CLIError("Browser memory evidence is incomplete.")

    isolation = _browser_check_evidence(by_name, "cross_user_memory_isolation")
    if not (
        isolation.get("users_exercised") == 2
        and isolation.get("marker_absent") is True
        and _identifier(isolation.get("response_id"))
        and _identifier(isolation.get("trace_id"))
        and isolation.get("response_id") != memory.get("response_id")
    ):
        raise CLIError("Browser cross-user isolation evidence is incomplete.")

    secret_scan = _browser_check_evidence(by_name, "secret_not_browser_visible")
    required_locations = {
        "browser_requests",
        "browser_responses",
        "console",
        "html",
        "javascript_assets",
        "local_storage",
        "session_storage",
        "script_visible_cookies",
        "cache_storage",
    }
    locations = secret_scan.get("locations_scanned")
    if not (
        secret_scan.get("matches") == 0
        and isinstance(locations, list)
        and required_locations.issubset({str(item) for item in locations})
        and _sha256(secret_scan.get("runtime_key_fingerprint"))
    ):
        raise CLIError("Browser secret-scan evidence is incomplete.")

    write = _browser_check_evidence(by_name, "app_owned_write_confirmation")
    if not (
        write.get("unconfirmed_status") == 409
        and write.get("confirmed_status") == 200
        and write.get("persisted_across_requests") is True
    ):
        raise CLIError("Browser app-owned write evidence is incomplete.")

    hosted = _mapping(deployment, "deployment")
    if browser.get("fixture_url") != hosted.get("fixture_url"):
        raise CLIError("Browser evidence fixture URL does not match the hosted deployment.")
    dashboard = _mapping(deployment, "artifacts").get("dashboard")
    if (
        isinstance(dashboard, Mapping)
        and dashboard.get("provider") == "vercel"
        and dashboard.get("url") != hosted.get("dashboard_url")
    ):
        raise CLIError("Vercel provenance URL does not match the certified dashboard URL.")


def _browser_check_evidence(
    checks: Mapping[str, Mapping[str, Any]],
    name: str,
) -> Mapping[str, Any]:
    evidence = checks[name].get("evidence")
    if not isinstance(evidence, Mapping):
        raise CLIError(f"Browser certification has no {name} evidence.")
    return evidence


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _mapping(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise CLIError(f"Certification evidence is missing the {name} object.")
    return dict(value)


def _check_evidence(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise CLIError("Launch verification checks are unavailable.")
    for row in checks:
        if not isinstance(row, Mapping) or row.get("name") != name:
            continue
        evidence = row.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, Mapping):
                    return item
        break
    raise CLIError(f"Launch verification has no {name} evidence.")
