"""Contract tests for one-prompt launch verification."""

from __future__ import annotations

import json
import os
import shlex
import sys
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from platform_cli.deploy_helpers import project_config_fingerprint
from platform_cli.errors import APIError, CLIError
from platform_cli.launch_verification import (
    APPLICATION_EVIDENCE_SCHEMA_VERSION,
    EXPECTED_EVIDENCE_PRODUCERS,
    REQUIRED_BETA_CHECKS,
    application_command_contract_sha,
    bind_application_command_contract,
    check_result,
    collect_application_checks,
    collect_hosted_checks,
    correlate_application_checks,
    evaluate_launch_verification,
    evidence_is_fresh,
    launch_session_fingerprint,
    manifest_fingerprint,
    write_verification_receipt,
)


def test_cli_project_config_fingerprint_matches_control_plane_contract() -> None:
    payload = {
        "yaml_content": "apiVersion: genaug/v1\n",
        "soul_content": "# Soul\n",
        "skills": ["one", "two"],
    }

    assert project_config_fingerprint(payload) == (
        "a72f5914426af10e625b2420498030d07b2520a2ac9a422fe12dc2120df6c367"
    )


def _manifest(*, commands: list[str] | None = None) -> dict[str, object]:
    return {
        "apiVersion": "genaug/v1",
        "kind": "Agent",
        "x-general-augment-launch": {
            "capabilities": [
                {
                    "name": "habit_list_context",
                    "classification": "read_only",
                }
            ],
            "verification": {
                "required_checks": list(REQUIRED_BETA_CHECKS),
                "application_commands": commands or [],
                "application_evidence_path": ".genaug/application-verification.json",
            },
            "rollback": {
                "disable": "Remove the server route and General Augment environment entries.",
                "data": "Delete or export project memory before archival.",
            },
        },
    }


def _passing_checks() -> list[dict[str, object]]:
    now = datetime.now(UTC)
    return [
        check_result(
            name,
            "PASS",
            f"{name}_passed",
            "Verified with deterministic evidence.",
            evidence=[{"artifact_sha256": f"sha256-{index}"}],
            checked_at=now,
            producer=EXPECTED_EVIDENCE_PRODUCERS[name],
        )
        for index, name in enumerate(REQUIRED_BETA_CHECKS)
    ]


def _collect_application_checks(
    workspace: Path,
    manifest: dict[str, object],
    *,
    runtime_api_key: str | None,
    runtime_api_base_url: str | None = None,
    project_id: str | None = None,
) -> tuple[list[dict[str, object]], dict[str, Any]]:
    return collect_application_checks(
        workspace,
        manifest,
        runtime_api_key=runtime_api_key,
        runtime_api_base_url=runtime_api_base_url,
        project_id=project_id,
        approved_command_contract_sha=application_command_contract_sha(workspace, manifest),
        launch_fingerprint_value="a" * 64,
        manifest_fingerprint_value=manifest_fingerprint(manifest),
    )


def test_every_required_pass_returns_ready_exactly_once() -> None:
    payload = evaluate_launch_verification(_manifest(), _passing_checks())

    assert payload["verdict"] == "READY"
    assert payload["reason_codes"] == []
    assert [row["name"] for row in payload["checks"]] == list(REQUIRED_BETA_CHECKS)
    assert all(row["required"] is True for row in payload["checks"])
    assert all(row["status"] == "PASS" for row in payload["checks"])


@pytest.mark.parametrize("status", ["FAIL", "SKIP"])
def test_required_non_pass_is_blocking(status: str) -> None:
    checks = _passing_checks()
    checks[4] = check_result(
        REQUIRED_BETA_CHECKS[4],
        status,  # type: ignore[arg-type]
        "runtime_key_not_proven",
        "Runtime execution evidence was unavailable.",
    )

    payload = evaluate_launch_verification(_manifest(), checks)

    assert payload["verdict"] == "BLOCKED"
    assert payload["reason_codes"] == ["runtime_key_not_proven"]


def test_missing_and_duplicate_required_results_are_blocking() -> None:
    checks = _passing_checks()
    missing = checks.pop(5)
    checks.extend([checks[0], checks[0]])

    payload = evaluate_launch_verification(_manifest(), checks)
    by_name = {row["name"]: row for row in payload["checks"]}

    assert payload["verdict"] == "BLOCKED"
    assert by_name[missing["name"]]["status"] == "SKIP"
    assert by_name[checks[0]["name"]]["status"] == "FAIL"


def test_boolean_only_pass_assertion_is_rejected() -> None:
    checks = _passing_checks()
    checks[0] = check_result(
        REQUIRED_BETA_CHECKS[0],
        "PASS",
        "claimed_pass",
        "Untrusted client assertion.",
        evidence=[{"tests_passed": True}],
        producer=EXPECTED_EVIDENCE_PRODUCERS[REQUIRED_BETA_CHECKS[0]],
    )

    payload = evaluate_launch_verification(_manifest(), checks)

    assert payload["verdict"] == "BLOCKED"
    assert payload["checks"][0]["reason_code"].endswith("unverifiable_pass_evidence")


def test_stale_pass_evidence_is_rejected() -> None:
    checks = _passing_checks()
    checks[0] = check_result(
        REQUIRED_BETA_CHECKS[0],
        "PASS",
        "claimed_pass",
        "Old evidence must not certify a current launch.",
        evidence=[{"artifact_sha256": "old-artifact"}],
        checked_at=datetime.now(UTC) - timedelta(days=2),
        producer=EXPECTED_EVIDENCE_PRODUCERS[REQUIRED_BETA_CHECKS[0]],
    )

    payload = evaluate_launch_verification(_manifest(), checks)

    assert payload["verdict"] == "BLOCKED"
    assert payload["checks"][0]["reason_code"].endswith("evidence_stale")


def test_manifest_cannot_remove_or_reorder_required_contract() -> None:
    manifest = _manifest()
    verification = manifest["x-general-augment-launch"]["verification"]  # type: ignore[index]
    verification["required_checks"] = list(reversed(REQUIRED_BETA_CHECKS))

    payload = evaluate_launch_verification(manifest, _passing_checks())

    assert payload["verdict"] == "BLOCKED"
    assert payload["checks"][0]["reason_code"] == "manifest_required_check_contract_mismatch"


def test_repository_capability_claim_cannot_promote_ready() -> None:
    checks = _passing_checks()
    index = REQUIRED_BETA_CHECKS.index("read_only_application_capability")
    checks[index] = check_result(
        "read_only_application_capability",
        "PASS",
        "read_only_application_capability_passed",
        "Repository-authored claims are not an authoritative beta check.",
        evidence=[{"response_id": "fabricated-response-id"}],
        producer="repository_claim",
    )

    payload = evaluate_launch_verification(_manifest(), checks)

    assert payload["verdict"] == "BLOCKED"
    assert payload["checks"][index]["reason_code"] == (
        "read_only_application_capability_unauthorized_evidence_producer"
    )


def test_optional_skip_only_returns_ready_with_warnings() -> None:
    optional = check_result(
        "preview_deployment",
        "SKIP",
        "preview_not_configured",
        "Preview deployment is optional for local certification.",
        required=False,
    )

    payload = evaluate_launch_verification(_manifest(), [*_passing_checks(), optional])

    assert payload["verdict"] == "READY_WITH_WARNINGS"
    assert payload["reason_codes"] == ["preview_not_configured"]


def test_sensitive_evidence_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Sensitive verification evidence"):
        check_result(
            "runtime_key_execution",
            "PASS",
            "runtime_key_execution_passed",
            "Should never serialize this evidence.",
            evidence=[{"api_key": "synthetic-never-write"}],
        )
    with pytest.raises(ValueError, match=r"nested\.authorization"):
        check_result(
            "runtime_key_execution",
            "PASS",
            "runtime_key_execution_passed",
            "Nested evidence must be safe too.",
            evidence=[{"nested": {"authorization": "Bearer synthetic"}}],
        )


def test_evidence_freshness_has_bounded_window() -> None:
    now = datetime.now(UTC)
    assert evidence_is_fresh((now - timedelta(minutes=1)).isoformat(), now=now)
    assert not evidence_is_fresh((now - timedelta(days=2)).isoformat(), now=now)
    assert not evidence_is_fresh((now + timedelta(minutes=1)).isoformat(), now=now)


def test_local_commands_capability_and_secret_scan_produce_verifiable_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / ".next" / "static").mkdir(parents=True)
    (workspace / ".next" / "static" / "app.js").write_text(
        "console.log('browser-safe');\n", encoding="utf-8"
    )
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "tsc --noEmit",
                    "build": "next build",
                    "test:e2e": "playwright test",
                }
            }
        ),
        encoding="utf-8",
    )

    def run_command(argv: list[str], **kwargs: object) -> SimpleNamespace:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        attempt_id = str(env["GENAUG_VERIFICATION_ATTEMPT_ID"])
        assert env["GENAUG_LAUNCH_SESSION_FINGERPRINT"] == "a" * 64
        assert env["GENAUG_MANIFEST_FINGERPRINT"] == manifest_fingerprint(manifest)
        if argv[-1] == "build":
            target = workspace / ".next" / "build-manifest.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"pages": {}}\n', encoding="utf-8")
        if "test" in argv:
            assert env["GENAUG_E2E_MODE"] == "hosted"
            assert env["GENAUG_API_KEY"] == "ga_runtime_synthetic_test_only"
            assert env["GENAUG_API_BASE_URL"] == "https://api.example.test"
            assert env["GENAUG_PROJECT_ID"] == "project-1"
            output_arg = next(item for item in argv if item.startswith("--output="))
            output_dir = Path(output_arg.removeprefix("--output="))
            result = output_dir / ".last-run.json"
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text('{"status": "passed"}\n', encoding="utf-8")
            trace = output_dir / "fixture" / "trace.zip"
            trace.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(trace, "w") as archive:
                archive.writestr("trace.network", "safe browser traffic")
            target = workspace / ".genaug" / "application-verification.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "schema_version": APPLICATION_EVIDENCE_SCHEMA_VERSION,
                        "generated_at": datetime.now(UTC).isoformat(),
                        "verification_attempt_id": attempt_id,
                        "checks": [
                            {
                                "name": "read_only_application_capability",
                                "status": "PASS",
                                "evidence": {
                                    "capability": "habit_list_context",
                                    "classification": "read_only",
                                    "identity_binding": "authenticated_server_user",
                                    "response_id": "resp_fixture_1",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("platform_cli.launch_verification.subprocess.run", run_command)
    monkeypatch.setattr(
        "platform_cli.launch_verification._trusted_command_argv",
        lambda _workspace, _category, argv: argv,
    )
    commands = [
        "npm run typecheck",
        "npm run build",
        "npm run test:e2e",
    ]
    manifest = _manifest(commands=commands)

    checks, receipts = _collect_application_checks(
        workspace,
        manifest,
        runtime_api_key="ga_runtime_synthetic_test_only",
        runtime_api_base_url="https://api.example.test/",
        project_id="project-1",
    )
    by_name = {row["name"]: row for row in checks}

    assert by_name["application_typecheck_or_equivalent"]["status"] == "PASS"
    assert by_name["application_build"]["status"] == "PASS"
    assert by_name["application_browser_smoke"]["status"] == "PASS"
    assert by_name["read_only_application_capability"]["status"] == "PASS"
    assert by_name["read_only_application_capability"]["producer"] == "repository_claim"
    assert by_name["secret_not_browser_visible"]["status"] == "PASS"
    assert by_name["rollback_documented"]["status"] == "PASS"
    assert all("stdout" not in receipt for receipt in receipts["receipts"])
    serialized = json.dumps({"checks": checks, "receipts": receipts})
    assert "ga_runtime_synthetic_test_only" not in serialized


def test_exact_runtime_key_in_browser_asset_fails_without_echoing_it(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    asset = workspace / ".next" / "static" / "app.js"
    asset.parent.mkdir(parents=True)
    runtime_key = "ga_runtime_synthetic_browser_leak"
    asset.write_text(f"window.__key='{runtime_key}'", encoding="utf-8")

    checks, _ = _collect_application_checks(
        workspace,
        _manifest(),
        runtime_api_key=runtime_key,
    )
    row = next(item for item in checks if item["name"] == "secret_not_browser_visible")

    assert row["status"] == "FAIL"
    assert runtime_key not in json.dumps(row)


def test_exact_runtime_key_in_prerendered_html_fails_without_echoing_it(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    html = workspace / ".next" / "server" / "app" / "assistant.html"
    html.parent.mkdir(parents=True)
    runtime_key = "ga_runtime_synthetic_prerender_leak"
    html.write_text(f"<script>window.__key='{runtime_key}'</script>", encoding="utf-8")

    checks, _ = _collect_application_checks(
        workspace,
        _manifest(),
        runtime_api_key=runtime_key,
    )
    row = next(item for item in checks if item["name"] == "secret_not_browser_visible")

    assert row["status"] == "FAIL"
    assert runtime_key not in json.dumps(row)


def test_runtime_key_in_fresh_playwright_trace_fails_without_echoing_it(tmp_path: Path) -> None:
    """A safe static bundle cannot hide a credential observed in browser traffic."""

    launch_verification = import_module("platform_cli.launch_verification")
    workspace = tmp_path / "app"
    asset = workspace / ".next" / "static" / "app.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("console.log('safe')", encoding="utf-8")
    output_dir = tmp_path / "verifier-output"
    trace = output_dir / "fixture" / "trace.zip"
    trace.parent.mkdir(parents=True)
    runtime_key = "ga_runtime_synthetic_trace_leak"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("trace.network", f"authorization: Bearer {runtime_key}")

    evidence = launch_verification._browser_runtime_evidence(
        output_dir,
        runtime_api_key=runtime_key,
        stdout=b"",
        stderr=b"",
        command_succeeded=True,
    )
    receipt = {
        "exit_code": 0,
        "semantic_evidence": True,
        **evidence,
    }
    row = launch_verification._browser_secret_check(
        workspace,
        runtime_key,
        browser_receipts=[receipt],
    )

    assert row["status"] == "FAIL"
    assert row["reason_code"] == "secret_browser_artifact_match"
    assert runtime_key not in json.dumps(row)


def test_safe_static_bundle_without_fresh_runtime_trace_fails_closed(tmp_path: Path) -> None:
    launch_verification = import_module("platform_cli.launch_verification")
    workspace = tmp_path / "app"
    asset = workspace / ".next" / "static" / "app.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("console.log('safe')", encoding="utf-8")

    row = launch_verification._browser_secret_check(
        workspace,
        "ga_runtime_synthetic_missing_trace",
        browser_receipts=[],
    )

    assert row["status"] == "FAIL"
    assert row["reason_code"] == "secret_browser_runtime_evidence_missing"


def test_failing_canonical_typecheck_blocks_typecheck_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "tsc --noEmit",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "platform_cli.launch_verification.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
    )
    monkeypatch.setattr(
        "platform_cli.launch_verification._trusted_command_argv",
        lambda _workspace, _category, argv: argv,
    )

    checks, _ = _collect_application_checks(
        workspace,
        _manifest(
                commands=[
                    "npm run typecheck",
                ]
        ),
        runtime_api_key="ga_runtime_synthetic_test_only",
    )
    row = next(
        item for item in checks if item["name"] == "application_typecheck_or_equivalent"
    )

    assert row["status"] == "FAIL"
    assert row["reason_code"] == "application_typecheck_or_equivalent_command_failed"


def test_keyword_labelled_arbitrary_commands_are_never_executed(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    sentinel = workspace / "executed"
    script = workspace / "payload.py"
    script.write_text(
        "from pathlib import Path\nPath('executed').write_text('unsafe')\n",
        encoding="utf-8",
    )
    python = shlex.quote(sys.executable)

    checks, receipts = _collect_application_checks(
        workspace,
        _manifest(
            commands=[
                "npm test",
                f"{python} payload.py typecheck",
                f"{python} payload.py build",
                f"{python} payload.py playwright",
            ]
        ),
        runtime_api_key="ga_runtime_synthetic_test_only",
    )

    assert not sentinel.exists()
    assert {row["outcome"] for row in receipts["receipts"]} == {"unsupported"}
    by_name = {row["name"]: row for row in checks}
    assert by_name["application_typecheck_or_equivalent"]["status"] == "SKIP"
    assert by_name["application_build"]["status"] == "SKIP"
    assert by_name["application_browser_smoke"]["status"] == "SKIP"


def test_matching_hosted_run_without_capability_proof_fails_closed() -> None:
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {"session_id": "launch_fixture_1", "plan": {}}
    manifest = _manifest()
    now = datetime.now(UTC).isoformat()

    class Client:
        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "project_id": project_id,
                "latest_run": {
                    "id": "run-fixture-1",
                    "response_id": "resp-fixture-1",
                    "trace_id": "trace-fixture-1",
                    "status": "completed",
                    "completed_at": now,
                },
            }

    local = check_result(
        "read_only_application_capability",
        "PASS",
        "read_only_application_capability_passed",
        "Repository claim awaiting hosted correlation.",
        evidence=[
            {
                "capability": "habit_list_context",
                "identity_binding": "authenticated_server_user",
                "response_id": "resp-fixture-1",
                "verification_attempt_id": "attempt-fixture-1",
            }
        ],
        producer="repository_claim",
    )

    correlated = correlate_application_checks(
        Client(),
        [local],
        installer_token="installer-synthetic",
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
    )

    assert correlated[0]["status"] == "FAIL"
    assert correlated[0]["reason_code"] == (
        "read_only_application_capability_hosted_correlation_failed"
    )


def test_hosted_capability_proof_binds_current_attempt_and_reviewed_plan() -> None:
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {"session_id": "launch_fixture_1", "plan": {}}
    manifest = _manifest()
    now = datetime.now(UTC).isoformat()

    class Client:
        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "project_id": project_id,
                "latest_run": {
                    "id": "run-fixture-1",
                    "response_id": "resp-fixture-1",
                    "trace_id": "trace-fixture-1",
                    "status": "completed",
                    "completed_at": now,
                },
                "application_capability": {
                    "capability": "habit_list_context",
                    "classification": "read_only",
                    "identity_binding": "authenticated_server_user",
                    "response_id": "resp-fixture-1",
                    "run_id": "run-fixture-1",
                    "step_id": "step-fixture-1",
                    "evidence_source": "platform_tool_execution",
                },
            }

    local = check_result(
        "read_only_application_capability",
        "PASS",
        "read_only_application_capability_passed",
        "Repository claim awaiting hosted correlation.",
        evidence=[
            {
                "capability": "habit_list_context",
                "identity_binding": "authenticated_server_user",
                "response_id": "resp-fixture-1",
                "verification_attempt_id": "attempt-fixture-1",
            }
        ],
        producer="repository_claim",
    )

    correlated = correlate_application_checks(
        Client(),
        [local],
        installer_token="installer-synthetic",
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
    )

    assert correlated[0]["status"] == "PASS"
    assert correlated[0]["producer"] == "hosted_correlated"


def test_app_owned_context_binds_cli_browser_proof_to_hosted_run() -> None:
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {"session_id": "launch_fixture_1", "plan": {}}
    manifest = _manifest()
    launch = manifest["x-general-augment-launch"]
    assert isinstance(launch, dict)
    launch["capabilities"] = [
        {
            "name": "habit_list_context",
            "classification": "read_only",
            "execution_owner": "application",
            "source": {"kind": "app_owned_context"},
        }
    ]
    now = datetime.now(UTC).isoformat()

    class Client:
        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "project_id": project_id,
                "latest_run": {
                    "id": "run-fixture-1",
                    "response_id": "resp-fixture-1",
                    "trace_id": "trace-fixture-1",
                    "status": "completed",
                    "completed_at": now,
                },
            }

    local = check_result(
        "read_only_application_capability",
        "PASS",
        "read_only_application_capability_passed",
        "CLI-owned browser proof awaiting hosted correlation.",
        evidence=[
            {
                "artifact_sha256": "a" * 64,
                "capability": "habit_list_context",
                "classification": "read_only",
                "execution_owner": "application",
                "identity_binding": "authenticated_server_user",
                "response_id": "resp-fixture-1",
                "verification_attempt_id": "attempt-fixture-1",
            }
        ],
        producer="repository_claim",
    )

    correlated = correlate_application_checks(
        Client(),
        [local],
        installer_token="installer-synthetic",
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
    )

    assert correlated[0]["status"] == "PASS"
    assert correlated[0]["producer"] == "hosted_correlated"
    assert correlated[0]["evidence"][0]["evidence_source"] == ("cli_verified_application_context")

    unbound = dict(local)
    unbound["evidence"] = [
        {
            "capability": "habit_list_context",
            "classification": "read_only",
            "execution_owner": "application",
            "identity_binding": "authenticated_server_user",
            "response_id": "resp-fixture-1",
            "verification_attempt_id": "attempt-fixture-1",
        }
    ]
    rejected = correlate_application_checks(
        Client(),
        [unbound],
        installer_token="installer-synthetic",
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
    )
    assert rejected[0]["status"] == "FAIL"


def test_unsafe_package_script_mutation_blocks_before_repository_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_command = import_module("platform_cli.commands.launch")
    workspace = tmp_path / "app"
    workspace.mkdir()
    manifest_path = workspace / "genaug-agent.yaml"
    manifest = _manifest(commands=["npm run typecheck"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    package_path = workspace / "package.json"
    package_path.write_text(
        json.dumps({"scripts": {"typecheck": "tsc --noEmit"}}),
        encoding="utf-8",
    )
    base_artifact: dict[str, object] = {
        "session_id": "launch_fixture_1",
        "cli_version": "0.3.0",
        "skill_version": "1.0.0",
        "manifest_schema_version": "genaug/v1",
        "plan": {},
    }
    reviewed = bind_application_command_contract(base_artifact, workspace, manifest)
    sentinel = workspace / "repository-code-ran"
    (workspace / "payload.js").write_text(
        "require('fs').writeFileSync('repository-code-ran', 'unsafe')\n",
        encoding="utf-8",
    )

    class Client:
        def public(self, method: str, path: str) -> dict[str, object]:
            package_path.write_text(
                json.dumps(
                    {
                        "scripts": {
                            "typecheck": "tsc --noEmit && node payload.js",
                        }
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "ok", "version": "0.1.0", "build_sha": "build-1"}

        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "session_id": reviewed["session_id"],
                "project_id": "project-fixture-1",
                "status": "approved",
                "fingerprint": launch_session_fingerprint(reviewed),
                "created_at": datetime.now(UTC).isoformat(),
            }

    class Runtime:
        config = SimpleNamespace(runtime_api_key="ga_runtime_synthetic")

        @contextmanager
        def client(self):  # type: ignore[no-untyped-def]
            yield Client()

    monkeypatch.setattr(
        launch_command,
        "validate_local_agent_config",
        lambda path, **kwargs: SimpleNamespace(errors=[], warnings=[], status="valid"),
    )
    monkeypatch.setattr(launch_command, "_load_manifest", lambda workspace, path: manifest)
    monkeypatch.setattr(launch_command, "compatibility_status", lambda **kwargs: (True, []))
    monkeypatch.setattr(
        launch_command,
        "installer_auth_metadata",
        lambda config: {
            "access_token": "installer-expired-synthetic",
            "refresh_token": "installer-refresh-synthetic",
        },
    )
    installer_access_calls: list[object] = []

    def resolve_installer_access(runtime: object) -> str:
        installer_access_calls.append(runtime)
        return "installer-refreshed-synthetic"

    monkeypatch.setattr(
        launch_command,
        "installer_access_token",
        resolve_installer_access,
    )
    monkeypatch.setattr(launch_command, "collect_hosted_checks", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        launch_command,
        "correlate_application_checks",
        lambda _client, checks, **kwargs: checks,
    )
    monkeypatch.setattr(
        launch_command,
        "write_verification_receipt",
        lambda path, payload, **kwargs: path,
    )
    monkeypatch.setattr(
        "platform_cli.launch_verification.subprocess.run",
        lambda *args, **kwargs: pytest.fail("repository command must not execute"),
    )

    runtime = Runtime()
    payload = launch_command._verify_launch(
        runtime,
        workspace,
        manifest_path,
        "project-fixture-1",
        reviewed,
    )

    assert payload["verdict"] == "BLOCKED"
    assert installer_access_calls == [runtime]
    assert not sentinel.exists()
    typecheck = next(
        row
        for row in payload["checks"]
        if row["name"] == "application_typecheck_or_equivalent"
    )
    assert typecheck["reason_code"] == "application_typecheck_or_equivalent_command_missing"


def test_command_contract_survives_expected_coding_agent_apply(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    manifest = _manifest(
        commands=["npm run typecheck", "npm run build", "npm run test:e2e"]
    )
    base_artifact: dict[str, object] = {
        "session_id": "launch_fixture_1",
        "cli_version": "0.3.0",
        "skill_version": "1.0.0",
        "manifest_schema_version": "genaug/v1",
        "plan": {},
    }

    reviewed = bind_application_command_contract(base_artifact, workspace, manifest)
    approved_sha = application_command_contract_sha(workspace, manifest)

    (workspace / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "tsc --noEmit",
                    "build": "next build",
                    "test:e2e": "playwright test",
                }
            }
        ),
        encoding="utf-8",
    )
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (workspace / "playwright.config.ts").write_text("export default {};\n", encoding="utf-8")

    rebound = bind_application_command_contract(base_artifact, workspace, manifest)
    assert rebound["session_id"] == reviewed["session_id"]
    assert application_command_contract_sha(workspace, manifest) == approved_sha


def test_shell_tail_in_canonical_package_script_is_never_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    sentinel = workspace / "repository-code-ran"
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "typecheck": "tsc --noEmit && node payload.js",
                }
            }
        ),
        encoding="utf-8",
    )
    (workspace / "payload.js").write_text(
        "require('fs').writeFileSync('repository-code-ran', 'unsafe')\n",
        encoding="utf-8",
    )
    manifest = _manifest(commands=["npm run typecheck"])
    monkeypatch.setattr(
        "platform_cli.launch_verification.subprocess.run",
        lambda *args, **kwargs: pytest.fail("repository shell tail must not execute"),
    )

    checks, receipts = _collect_application_checks(
        workspace,
        manifest,
        runtime_api_key="ga_runtime_synthetic_test_only",
    )

    assert not sentinel.exists()
    assert receipts["receipts"][0]["outcome"] == "unsupported"
    typecheck = next(
        row for row in checks if row["name"] == "application_typecheck_or_equivalent"
    )
    assert typecheck["status"] == "SKIP"


def test_verification_receipt_is_owner_readable(tmp_path: Path) -> None:
    target = write_verification_receipt(
        tmp_path / ".genaug" / "launch-verification.json",
        {"verdict": "BLOCKED", "checks": []},
    )

    assert json.loads(target.read_text(encoding="utf-8"))["verdict"] == "BLOCKED"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_verification_receipt_refuses_symlinked_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / ".genaug").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CLIError, match="symlinked"):
        write_verification_receipt(
            workspace / ".genaug" / "launch-verification.json",
            {"verdict": "BLOCKED"},
            workspace=workspace,
        )

    assert list(outside.iterdir()) == []


def test_verification_command_refuses_symlinked_node_modules_parent(tmp_path: Path) -> None:
    launch_verification = import_module("platform_cli.launch_verification")
    workspace = tmp_path / "app"
    workspace.mkdir()
    outside = tmp_path / "outside-typescript"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "tsc").write_text(
        "require('fs').writeFileSync('outside-entrypoint-ran', 'unsafe')\n",
        encoding="utf-8",
    )
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "typescript").symlink_to(
        outside,
        target_is_directory=True,
    )
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"typecheck": "tsc --noEmit"}}),
        encoding="utf-8",
    )
    (workspace / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    manifest = _manifest(commands=["npm run typecheck"])

    receipt = launch_verification._run_application_command(
        workspace,
        "npm run typecheck",
        manifest=manifest,
        approved_command_contract_sha=application_command_contract_sha(workspace, manifest),
        timeout_seconds=5,
        verification_attempt_id="attempt-fixture",
        launch_fingerprint_value="launch-fixture",
        manifest_fingerprint_value="manifest-fixture",
    )

    assert receipt["outcome"] == "unsupported"
    assert receipt["exit_code"] == 126
    assert not (workspace / "outside-entrypoint-ran").exists()


def test_manifest_fingerprint_is_stable_across_mapping_order() -> None:
    assert manifest_fingerprint({"a": 1, "b": 2}) == manifest_fingerprint({"b": 2, "a": 1})


class _HostedClient:
    def __init__(
        self,
        *,
        artifact: dict[str, object],
        project_id: str,
        api_version: str = "0.1.0",
        runtime_key_id: str = "key-fixture-1",
    ) -> None:
        self.artifact = artifact
        self.project_id = project_id
        self.api_version = api_version
        self.runtime_key_id = runtime_key_id
        self.marker = ""

    def public(self, method: str, path: str) -> dict[str, object]:
        assert (method, path) == ("GET", "/health/ready")
        return {
            "status": "ok",
            "version": self.api_version,
            "build_sha": "build-fixture-1",
        }

    def installer(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        token: str | None = None,
    ) -> dict[str, object]:
        assert method == "GET"
        assert token == "installer-synthetic"
        now = datetime.now(UTC).isoformat()
        if path.endswith(str(self.artifact["session_id"])):
            return {
                "session_id": self.artifact["session_id"],
                "project_id": self.project_id,
                "status": "approved",
                "fingerprint": launch_session_fingerprint(self.artifact),
                "created_at": now,
            }
        if path.endswith("/verification-evidence"):
            response_id = params.get("response_id") if params else None
            return {
                "project_id": self.project_id,
                "runtime_keys": [
                    {
                        "id": self.runtime_key_id,
                        "scopes": ["responses:create"],
                        "created_at": now,
                    }
                ],
                "latest_run": (
                    {
                        "id": "run-fixture-1",
                        "response_id": response_id,
                        "trace_id": "trace-fixture-1",
                        "status": "completed",
                        "created_at": now,
                        "completed_at": now,
                    }
                    if response_id
                    else None
                ),
                "usage": {
                    "event_count": 1,
                    "quantity": 1,
                    "latest_at": now,
                    "run_id": "run-fixture-1" if response_id else None,
                    "response_id": response_id,
                    "input_tokens": 12 if response_id else None,
                    "output_tokens": 4 if response_id else None,
                    "completed_at": now if response_id else None,
                },
                "generated_at": now,
            }
        raise AssertionError(path)

    def runtime_app(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del params, headers
        payload = json or {}
        if path == "/v1/responses":
            user = str(payload.get("user") or "")
            metadata = payload.get("metadata")
            feature = metadata.get("feature") if isinstance(metadata, dict) else None
            if feature == "stable-user-memory-recall":
                return {"id": "resp-recall-a", "status": "completed", "output_text": self.marker}
            if feature == "cross-user-memory-isolation":
                assert user.endswith("user-b")
                return {"id": "resp-recall-b", "status": "completed", "output_text": "none"}
            return {"id": "resp-runtime-1", "status": "completed", "output_text": "works"}
        if path == "/api/v1/agent/memory/store":
            self.marker = str(payload.get("fact") or "").rsplit(" ", 1)[-1].rstrip(".")
            return {"memory_id": "memory-fixture-1"}
        if path == "/api/v1/agent/memory/search":
            user = str(payload.get("user_id") or "")
            return {
                "facts": (
                    [{"memory_id": "memory-fixture-1", "fact": self.marker}]
                    if user.endswith("user-a")
                    else []
                )
            }
        if method == "DELETE" and path.endswith("memory-fixture-1"):
            return {"deleted_count": 1}
        raise AssertionError((method, path))

    def runtime_response_event_stream(
        self,
        *,
        json: dict[str, object],
    ) -> list[dict[str, object]]:
        assert json["stream"] is True
        return [
            {"event": "response.created", "data": {"type": "response.created"}},
            {
                "event": "response.output_text.delta",
                "data": {"type": "response.output_text.delta", "delta": "works"},
            },
            {"event": "response.completed", "data": {"type": "response.completed"}},
        ]


def test_runtime_checks_classify_usage_limit_without_leaking_api_detail() -> None:
    launch_verification = import_module("platform_cli.launch_verification")

    class LimitedRecallClient(_HostedClient):
        def runtime_app(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
            payload = kwargs.get("json") or {}
            metadata = payload.get("metadata") if isinstance(payload, dict) else None
            if (
                path == "/v1/responses"
                and isinstance(metadata, dict)
                and metadata.get("feature") == "stable-user-memory-recall"
            ):
                raise APIError(402, {"message": "private-provider-detail"})
            return super().runtime_app(method, path, **kwargs)

    client = LimitedRecallClient(artifact={}, project_id="project-fixture-1")
    checks, artifact = launch_verification._runtime_execution_checks(
        client,
        project_id="project-fixture-1",
        runtime_api_key="ga-runtime-synthetic",
    )
    by_name = {row["name"]: row for row in checks}

    assert by_name["stable_user_continuity"]["reason_code"] == (
        "stable_user_continuity_runtime_limit_reached"
    )
    assert by_name["memory_write_recall"]["reason_code"] == (
        "memory_write_recall_runtime_limit_reached"
    )
    assert by_name["stable_user_continuity"]["status"] == "FAIL"
    assert "private-provider-detail" not in json.dumps({"checks": checks, "artifact": artifact})


def test_runtime_checks_classify_stream_rate_limit() -> None:
    launch_verification = import_module("platform_cli.launch_verification")

    class RateLimitedStreamClient(_HostedClient):
        def runtime_response_event_stream(
            self, *, json: dict[str, object]
        ) -> list[dict[str, object]]:
            del json
            raise APIError(429, {"message": "provider-rate-limit-detail"})

    checks, artifact = launch_verification._runtime_execution_checks(
        RateLimitedStreamClient(artifact={}, project_id="project-fixture-1"),
        project_id="project-fixture-1",
        runtime_api_key="ga-runtime-synthetic",
    )
    stream = next(row for row in checks if row["name"] == "streaming_event_sequence")

    assert stream["status"] == "FAIL"
    assert stream["reason_code"] == "streaming_event_sequence_runtime_rate_limited"
    assert "provider-rate-limit-detail" not in json.dumps({"checks": checks, "artifact": artifact})


def test_runtime_checks_classify_dependency_failure() -> None:
    launch_verification = import_module("platform_cli.launch_verification")

    class UnavailableRuntimeClient(_HostedClient):
        def runtime_app(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
            if path == "/v1/responses":
                raise APIError(503, {"message": "private-upstream-detail"})
            return super().runtime_app(method, path, **kwargs)

    checks, artifact = launch_verification._runtime_execution_checks(
        UnavailableRuntimeClient(artifact={}, project_id="project-fixture-1"),
        project_id="project-fixture-1",
        runtime_api_key="ga-runtime-synthetic",
    )
    by_name = {row["name"]: row for row in checks}

    assert by_name["runtime_key_execution"]["reason_code"] == (
        "runtime_key_execution_runtime_dependency_unavailable"
    )
    assert by_name["non_streaming_response"]["reason_code"] == (
        "non_streaming_response_runtime_dependency_unavailable"
    )
    assert "private-upstream-detail" not in json.dumps({"checks": checks, "artifact": artifact})


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (
            APIError(401, {"message": "private-auth-detail"}),
            "runtime_key_execution_runtime_authorization_failed",
        ),
        (
            APIError(403, {"message": "private-auth-detail"}),
            "runtime_key_execution_runtime_authorization_failed",
        ),
        (
            CLIError("private-transport-detail"),
            "runtime_key_execution_runtime_transport_failed",
        ),
    ],
)
def test_runtime_checks_classify_auth_and_transport_failures_without_detail(
    failure: Exception,
    expected_reason: str,
) -> None:
    launch_verification = import_module("platform_cli.launch_verification")

    class FailingRuntimeClient(_HostedClient):
        def runtime_app(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
            if path == "/v1/responses":
                raise failure
            return super().runtime_app(method, path, **kwargs)

    checks, artifact = launch_verification._runtime_execution_checks(
        FailingRuntimeClient(artifact={}, project_id="project-fixture-1"),
        project_id="project-fixture-1",
        runtime_api_key="ga-runtime-synthetic",
    )
    execution = next(row for row in checks if row["name"] == "runtime_key_execution")
    serialized = json.dumps({"checks": checks, "artifact": artifact})

    assert execution["status"] == "FAIL"
    assert execution["reason_code"] == expected_reason
    assert "private-auth-detail" not in serialized
    assert "private-transport-detail" not in serialized


def test_runtime_checks_create_second_user_before_isolation_search() -> None:
    launch_verification = import_module("platform_cli.launch_verification")

    class StrictUserLifecycleClient(_HostedClient):
        user_b_created = False

        def runtime_app(self, method: str, path: str, **kwargs: Any) -> dict[str, object]:
            payload = kwargs.get("json") or {}
            metadata = payload.get("metadata") if isinstance(payload, dict) else None
            if (
                path == "/v1/responses"
                and isinstance(metadata, dict)
                and metadata.get("feature") == "cross-user-memory-isolation"
            ):
                self.user_b_created = True
            if path == "/api/v1/agent/memory/search":
                user_id = str(payload.get("user_id") or "")
                if user_id.endswith("user-b") and not self.user_b_created:
                    raise APIError(404, {"message": "User not found."})
            return super().runtime_app(method, path, **kwargs)

    checks, _ = launch_verification._runtime_execution_checks(
        StrictUserLifecycleClient(artifact={}, project_id="project-fixture-1"),
        project_id="project-fixture-1",
        runtime_api_key="ga-runtime-synthetic",
    )
    isolation = next(row for row in checks if row["name"] == "cross_user_memory_isolation")

    assert isolation["status"] == "PASS"
    assert isolation["reason_code"] == "cross_user_memory_isolation_passed"


def test_hosted_collector_proves_separated_runtime_and_installer_evidence(
    tmp_path: Path,
) -> None:
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {
        "session_id": "launch_fixture_1",
        "cli_version": "0.3.0",
        "skill_version": "1.0.0",
        "manifest_schema_version": "genaug/v1",
        "status": "review_required",
        "inspection": {},
        "plan": {},
    }
    manifest = _manifest()
    manifest_path = tmp_path / "genaug-agent.yaml"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt_path = tmp_path / ".genaug" / "provisioning-receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "general-augment-provisioning-receipt/v1",
                "session_id": artifact["session_id"],
                "approved_plan_fingerprint": launch_session_fingerprint(artifact),
                "manifest_sha256": __import__("hashlib").sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "project_id": project_id,
                "runtime_key": {
                    "id": "key-fixture-1",
                    "masked_key": "ga...fixture",
                    "scopes": ["responses:create"],
                    "action": "reused",
                    "active_matching_count": 1,
                },
                "authorities": {
                    "control_plane": "installer_session",
                    "runtime": "application_runtime_key",
                },
                "environment": {"path": ".env.local", "configured": True},
                "checked_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    client = _HostedClient(artifact=artifact, project_id=project_id)

    checks, safe_artifact = collect_hosted_checks(
        client,
        installer_token="installer-synthetic",
        runtime_api_key="ga-runtime-synthetic",
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
        manifest_path=manifest_path,
        provisioning_receipt_path=receipt_path,
        compatible=True,
        compatibility_reasons=[],
    )
    by_name = {row["name"]: row for row in checks}

    for name in REQUIRED_BETA_CHECKS[:10]:
        assert by_name[name]["status"] == "PASS", (name, by_name[name])
    assert by_name["trace_visibility"]["status"] == "FAIL"
    assert by_name["trace_visibility"]["reason_code"] == (
        "trace_visibility_application_response_binding_missing"
    )
    assert by_name["usage_visibility"]["status"] == "FAIL"
    assert by_name["usage_visibility"]["reason_code"] == (
        "usage_visibility_application_response_binding_missing"
    )
    serialized = json.dumps({"checks": checks, "artifact": safe_artifact})
    assert "ga-runtime-synthetic" not in serialized
    assert "installer-synthetic" not in serialized


def test_hosted_collector_uses_current_durable_key_after_preview_finalization(
    tmp_path: Path,
) -> None:
    """An expired preview receipt must not shadow the configured durable key."""
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {
        "session_id": "launch_fixture_1",
        "cli_version": "0.3.0",
        "skill_version": "1.0.0",
        "manifest_schema_version": "genaug/v1",
        "status": "review_required",
        "inspection": {},
        "plan": {},
    }
    manifest = _manifest()
    manifest_path = tmp_path / "genaug-agent.yaml"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt_path = tmp_path / ".genaug" / "provisioning-receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "general-augment-provisioning-receipt/v1",
                "session_id": artifact["session_id"],
                "approved_plan_fingerprint": launch_session_fingerprint(artifact),
                "manifest_sha256": __import__("hashlib").sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "project_id": project_id,
                "runtime_key": {
                    "id": "expired-preview-key",
                    "scopes": ["responses:create"],
                    "action": "reused",
                    "active_matching_count": 1,
                },
                "authorities": {
                    "control_plane": "installer_session",
                    "runtime": "application_runtime_key",
                },
                "checked_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    checks, _ = collect_hosted_checks(
        _HostedClient(
            artifact=artifact,
            project_id=project_id,
            runtime_key_id="durable-key",
        ),
        installer_token="installer-synthetic",
        runtime_api_key="ga-runtime-synthetic",
        runtime_key_id="durable-key",
        runtime_key_scopes=["responses:create"],
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
        manifest_path=manifest_path,
        provisioning_receipt_path=receipt_path,
        compatible=True,
        compatibility_reasons=[],
    )
    by_name = {row["name"]: row for row in checks}

    assert by_name["provisioning_idempotent"]["status"] == "PASS"
    assert by_name["runtime_key_scope"]["status"] == "PASS"
    assert by_name["runtime_key_scope"]["evidence"] == [
        {"runtime_key_id": "durable-key", "scopes": ["responses:create"]}
    ]


def test_hosted_collector_blocks_incompatible_api_version(tmp_path: Path) -> None:
    project_id = "project-fixture-1"
    artifact: dict[str, object] = {
        "session_id": "launch_fixture_1",
        "cli_version": "0.3.0",
        "skill_version": "1.0.0",
        "manifest_schema_version": "genaug/v1",
        "status": "review_required",
        "inspection": {},
        "plan": {},
    }
    manifest = _manifest()
    manifest_path = tmp_path / "genaug-agent.yaml"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checks, _ = collect_hosted_checks(
        _HostedClient(
            artifact=artifact,
            project_id=project_id,
            api_version="0.2.0",
        ),
        installer_token=None,
        runtime_api_key=None,
        project_id=project_id,
        artifact=artifact,
        manifest=manifest,
        manifest_path=manifest_path,
        provisioning_receipt_path=tmp_path / ".genaug" / "missing.json",
        compatible=True,
        compatibility_reasons=[],
    )
    compatibility = next(
        row for row in checks if row["name"] == "cli_api_skill_manifest_compatibility"
    )

    assert compatibility["status"] == "FAIL"
    assert compatibility["reason_code"] == "hosted_api_version_incompatible"
    assert all(
        row["status"] == "SKIP"
        for row in checks
        if row["name"] != "cli_api_skill_manifest_compatibility"
    )
