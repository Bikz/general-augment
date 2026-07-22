"""Opinionated one-prompt activation workflow."""

from __future__ import annotations

import hashlib
import json
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from platform_cli.application_environment import (
    LocalEnvFileAdapter,
    reviewed_activation_environment_values,
)
from platform_cli.client import encode_path_segment
from platform_cli.commands.setup import _project_items, _select_or_create_project
from platform_cli.config import CLIConfig, save_config
from platform_cli.deploy_helpers import deploy_path_with_installer
from platform_cli.errors import CLIError
from platform_cli.launch_contract import (
    MANIFEST_SCHEMA_VERSION,
    bind_launch_context,
    build_launch_manifest,
    compatibility_status,
    launch_session_artifact,
    write_launch_manifest,
)
from platform_cli.launch_guidance import (
    apply_launch_answers,
    launch_questions,
    load_launch_answers,
)
from platform_cli.launch_release import finalize_verified_release, provision_release_preview
from platform_cli.launch_verification import (
    REQUIRED_BETA_CHECKS,
    bind_application_command_contract,
    check_result,
    collect_application_checks,
    collect_hosted_checks,
    collect_hosted_preflight_checks,
    correlate_application_checks,
    evaluate_launch_verification,
    evidence_is_fresh,
    launch_session_fingerprint,
    manifest_fingerprint,
    write_verification_receipt,
)
from platform_cli.openapi import load_deploy_payload, validate_local_agent_config
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.secure_filesystem import (
    assert_no_symlink_components,
    atomic_write_text_no_follow,
    confined_path,
    read_text_no_follow,
)
from platform_cli.self_serve import (
    dashboard_launch_url,
    installer_access_token,
    installer_auth_metadata,
)
from platform_cli.workspace_inspector import inspect_workspace

_LAUNCH_RUNTIME_KEY_NAME = "One-prompt launch app backend"
_RUNTIME_KEY_SCOPES = ("responses:create",)
_PROVISIONING_RECEIPT_SCHEMA = "general-augment-provisioning-receipt/v1"


def launch(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option(help="Application workspace to inspect.")] = Path("."),
    inspect: Annotated[
        bool,
        typer.Option("--inspect", help="Inspect only; never write files or call hosted APIs."),
    ] = False,
    questions: Annotated[
        bool,
        typer.Option(
            "--questions",
            help="Return unresolved account, app, Agent, and release questions without writes.",
        ),
    ] = False,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Generate or update the declarative launch contract."),
    ] = False,
    review: Annotated[
        bool,
        typer.Option("--review", help="Persist the sanitized plan and return its review URL."),
    ] = False,
    activate: Annotated[
        bool,
        typer.Option(
            "--activate",
            help=(
                "Request policy evaluation and provision immediately only when the exact "
                "plan is already approved or bounded safe auto-approval succeeds."
            ),
        ),
    ] = False,
    provision: Annotated[
        bool,
        typer.Option("--provision", help="Provision an approved plan idempotently."),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write structured coding-agent application instructions."),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option("--verify", help="Run deterministic local and hosted verification."),
    ] = False,
    finalize: Annotated[
        bool,
        typer.Option(
            "--finalize",
            help="Verify and promote an evidence-backed candidate, then install its durable key.",
        ),
    ] = False,
    open_dashboard: Annotated[
        bool,
        typer.Option("--open-dashboard", help="Open the exact launch review page."),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option(help="Existing project id, slug, or name."),
    ] = None,
    account_workspace: Annotated[
        str | None,
        typer.Option(
            "--account-workspace",
            help="Existing General Augment Workspace id, slug, or name.",
        ),
    ] = None,
    answers_file: Annotated[
        Path | None,
        typer.Option(
            "--answers-file",
            help="Secret-free structured answers used by --plan.",
        ),
    ] = None,
    manifest: Annotated[
        Path | None,
        typer.Option(help="Manifest path; defaults to WORKSPACE/genaug-agent.yaml."),
    ] = None,
    approve_session: Annotated[
        str | None,
        typer.Option(
            "--approve-session",
            help="Approved launch session id required by --provision.",
        ),
    ] = None,
    auto_approve_safe: Annotated[
        bool,
        typer.Option(
            "--auto-approve-safe",
            help=(
                "Ask the server to apply the Project's human-owned safe auto-approval policy; "
                "never grants authority by itself."
            ),
        ),
    ] = False,
    configure_application_env: Annotated[
        bool,
        typer.Option(
            "--configure-application-env",
            help="Explicitly write runtime variables to a verified ignored env file.",
        ),
    ] = False,
    remove_application_env: Annotated[
        bool,
        typer.Option(
            "--remove-application-env",
            help="Remove only General Augment-managed entries from the application env file.",
        ),
    ] = False,
    application_env_file: Annotated[
        Path | None,
        typer.Option(help="Application env target; defaults to WORKSPACE/.env.local."),
    ] = None,
    rotate_runtime_key: Annotated[
        bool,
        typer.Option(
            "--rotate-runtime-key",
            help="Revoke the launch runtime key and create a replacement.",
        ),
    ] = False,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print URLs without opening a browser."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print stable machine-readable JSON."),
    ] = False,
) -> None:
    """Inspect, review, provision, apply, and verify one safe-mode app integration."""
    phases = {
        "inspect": inspect,
        "questions": questions,
        "plan": plan,
        "review": review,
        "activate": activate,
        "provision": provision,
        "apply": apply,
        "verify": verify,
        "finalize": finalize,
        "open_dashboard": open_dashboard,
    }
    selected = [name for name, enabled in phases.items() if enabled]
    if len(selected) > 1:
        raise CLIError("Choose one launch phase at a time.")
    if configure_application_env and remove_application_env:
        raise CLIError(
            "Choose either --configure-application-env or --remove-application-env."
        )
    phase = selected[0] if selected else "plan"
    if phase not in {"provision", "activate", "finalize"} and (
        configure_application_env
        or remove_application_env
        or application_env_file is not None
        or rotate_runtime_key
    ):
        raise CLIError(
            "Application environment and runtime-key options require --provision or --activate."
        )
    if answers_file is not None and phase != "plan":
        raise CLIError("--answers-file is only supported with --plan.")
    if auto_approve_safe and phase not in {"review", "activate"}:
        raise CLIError("--auto-approve-safe is only supported with --review or --activate.")
    if phase == "finalize" and remove_application_env:
        raise CLIError("Use the explicit deactivate workflow to remove application credentials.")
    runtime: Runtime = ctx.obj
    root = workspace.expanduser().resolve()
    selected_manifest = (manifest or root / "genaug-agent.yaml").expanduser()
    manifest_path = confined_path(
        root,
        selected_manifest if selected_manifest.is_absolute() else root / selected_manifest,
        description="launch manifest path",
    )
    assert_no_symlink_components(root, manifest_path, description="launch manifest path")
    inspection = inspect_workspace(root)

    if phase == "inspect":
        _emit({"phase": "inspect", "status": "INSPECTED", "inspection": inspection}, json_output)
        return

    if phase == "questions":
        _emit(_launch_questions(runtime, inspection), json_output)
        return

    manifest_payload = build_launch_manifest(
        root,
        inspection,
        project_ref=project or runtime.config.active_project,
    )

    if phase == "plan":
        existing_contract = False
        existing_text = read_text_no_follow(
            root,
            manifest_path,
            description="launch manifest path",
        )
        if existing_text is not None:
            existing_payload = yaml.safe_load(existing_text)
            if (
                isinstance(existing_payload, dict)
                and existing_payload.get("apiVersion") == MANIFEST_SCHEMA_VERSION
                and existing_payload.get("kind") == "Project"
            ):
                # A v2 manifest is the customer's declarative source of truth. Inspection
                # evidence is returned separately and the reviewed fingerprint protects
                # provisioning, so a plan rerun must not replace explicit safety limits,
                # capability boundaries, Agent topology, or application integration paths.
                manifest_payload = existing_payload
                existing_contract = True
        if answers_file is not None:
            manifest_payload = apply_launch_answers(
                manifest_payload,
                load_launch_answers(root, answers_file),
            )
        if existing_contract and answers_file is None:
            path = manifest_path
        else:
            path = write_launch_manifest(
                manifest_path,
                manifest_payload,
                workspace=root,
                preserve_reviewed_contract=existing_contract,
            )
        manifest_payload = _load_manifest(root, path)
        artifact = _launch_artifact(root, path, inspection, manifest_payload)
        validation = validate_local_agent_config(
            path,
            yaml_content=_manifest_text(root, path),
        )
        payload = {
            "phase": "plan",
            "status": "REVIEW_REQUIRED",
            "session_id": artifact["session_id"],
            "manifest_path": str(path),
            "manifest_schema_version": artifact["manifest_schema_version"],
            "skill_version": artifact["skill_version"],
            "validation": {
                "status": validation.status,
                "errors": validation.errors,
                "warnings": validation.warnings,
            },
            "next": "genaug launch --review --json",
        }
        _emit(payload, json_output)
        return

    manifest_payload = _load_manifest(root, manifest_path)
    artifact = _launch_artifact(root, manifest_path, inspection, manifest_payload)

    if phase in {"review", "activate"}:
        payload = _persist_review(
            runtime,
            root,
            manifest_path,
            inspection,
            project,
            account_workspace,
            request_safe_auto=auto_approve_safe or phase == "activate",
        )
        if payload["status"] == "REVIEW_REQUIRED" and not no_browser:
            webbrowser.open(str(payload["dashboard_review_url"]))
        if phase == "activate" and payload["status"] == "APPROVED":
            project_value = payload.get("project")
            selected_project = str(
                project_value.get("id") if isinstance(project_value, dict) else ""
            )
            if not selected_project:
                raise CLIError("Approved launch did not return its linked Project id.")
            provisioned = _provision(
                runtime,
                root,
                manifest_path,
                selected_project,
                str(payload["session_id"]),
                _launch_artifact(
                    root,
                    manifest_path,
                    inspection,
                    _load_manifest(root, manifest_path),
                ),
                configure_application_env=configure_application_env,
                remove_application_env=remove_application_env,
                application_env_file=application_env_file,
                rotate_runtime_key=rotate_runtime_key,
            )
            handoff = _write_handoff(
                root,
                manifest_path,
                _launch_artifact(
                    root,
                    manifest_path,
                    inspection,
                    _load_manifest(root, manifest_path),
                ),
            )
            _emit(
                {
                    "phase": "activate",
                    "status": "ACTIVATED",
                    "approval": payload,
                    "provisioning": provisioned,
                    "handoff_path": str(handoff),
                },
                json_output,
            )
            return
        _emit(payload, json_output)
        return

    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("No linked project. Run genaug launch --review first.")

    if phase == "provision":
        if not approve_session:
            raise CLIError("--provision requires --approve-session from the dashboard review.")
        payload = _provision(
            runtime,
            root,
            manifest_path,
            project_ref,
            approve_session,
            artifact,
            configure_application_env=configure_application_env,
            remove_application_env=remove_application_env,
            application_env_file=application_env_file,
            rotate_runtime_key=rotate_runtime_key,
        )
        _emit(payload, json_output)
        return

    if phase == "apply":
        handoff = _write_handoff(root, manifest_path, artifact)
        _emit(
            {
                "phase": "apply",
                "status": "CODING_AGENT_ACTION_REQUIRED",
                "session_id": artifact["session_id"],
                "handoff_path": str(handoff),
                "manifest_path": str(manifest_path),
                "next": (
                    "Apply the official generalaugment-launch skill, then run "
                    "genaug launch --verify --json."
                ),
            },
            json_output,
        )
        return

    if phase == "finalize":
        payload = _finalize_launch(
            runtime,
            root,
            manifest_path,
            artifact,
            str(project_ref),
            configure_application_env=configure_application_env,
            application_env_file=application_env_file,
            activation_values=reviewed_activation_environment_values(artifact),
        )
        _emit(payload, json_output)
        return

    review_url = _review_url(str(project_ref), str(artifact["session_id"]))
    if phase == "open_dashboard":
        if not no_browser:
            webbrowser.open(review_url)
        _emit(
            {
                "phase": "open_dashboard",
                "status": "OPENED" if not no_browser else "URL_READY",
                "dashboard_review_url": review_url,
            },
            json_output,
        )
        return

    payload = _verify_launch(runtime, root, manifest_path, str(project_ref), artifact)
    _emit(payload, json_output)
    if payload["verdict"] == "BLOCKED":
        raise CLIError("Launch verification is blocked; inspect the JSON reason codes.")


def _persist_review(
    runtime: Runtime,
    workspace: Path,
    manifest_path: Path,
    inspection: dict[str, Any],
    project_ref: str | None,
    workspace_ref: str | None,
    *,
    request_safe_auto: bool,
) -> dict[str, Any]:
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Run genaug auth login before genaug launch --review.")
    token = installer_access_token(runtime)
    manifest = _load_manifest(workspace, manifest_path)
    metadata_value = manifest.get("metadata")
    metadata: dict[str, Any] = (
        dict(metadata_value) if isinstance(metadata_value, dict) else {}
    )
    with runtime.client() as client:
        workspace_payload = client.installer("GET", "/workspaces", token=token)
        workspace_row = _select_or_create_workspace(
            client,
            token=token,
            payload=workspace_payload,
            reference=workspace_ref or runtime.config.active_workspace,
            manifest=manifest,
        )
        workspace_id = str(workspace_row["id"])
        projects_payload = client.installer("GET", "/projects", token=token)
        scoped_projects = {
            "items": [
                row
                for row in _project_items(projects_payload)
                if str(row.get("workspace_id") or "") == workspace_id
            ]
        }
        declared_project = _manifest_context(manifest)
        declared_ref = str(declared_project.get("ref") or "") or None
        create_project = declared_project.get("create") is True
        project_payload = _select_or_create_project(
            client,
            token=token,
            workspace=workspace,
            projects_payload=scoped_projects,
            project=project_ref or declared_ref,
            project_name=(
                str(declared_project.get("name") or metadata.get("display_name") or workspace.name)
                if create_project or not scoped_projects["items"]
                else None
            ),
            project_slug=(
                str(declared_project.get("slug") or metadata.get("name") or workspace.name)
                if create_project or not scoped_projects["items"]
                else None
            ),
            workspace_id=workspace_id,
        )
        selected = str(project_payload["id"])
        manifest = bind_launch_context(
            manifest,
            workspace_id=workspace_id,
            project_id=selected,
        )
        write_launch_manifest(
            manifest_path,
            manifest,
            workspace=workspace,
            preserve_reviewed_contract=True,
        )
        manifest = _load_manifest(workspace, manifest_path)
        artifact = _launch_artifact(workspace, manifest_path, inspection, manifest)
        record = client.installer(
            "POST",
            f"/projects/{encode_path_segment(selected)}/launch-sessions",
            token=token,
            json={
                **artifact,
                "approval_mode": "safe_auto" if request_safe_auto else "required",
            },
        )
        if isinstance(record, dict):
            record = client.installer(
                "GET",
                f"/projects/{encode_path_segment(selected)}/launch-sessions/"
                f"{encode_path_segment(str(record.get('session_id') or artifact['session_id']))}",
                token=token,
            )
    save_config(
        runtime.config.model_copy(
            update={"active_workspace": workspace_id, "active_project": selected}
        ),
        runtime.config_path,
    )
    session_id = str(record.get("session_id") or artifact["session_id"])
    approved = isinstance(record, dict) and record.get("status") == "approved"
    return {
        "phase": "review",
        "status": "APPROVED" if approved else "REVIEW_REQUIRED",
        "approval_source": (
            str(record.get("approval_source") or "human")
            if isinstance(record, dict) and approved
            else "none"
        ),
        "approval_reason": (
            str(record.get("approval_reason") or "launch_review_required")
            if isinstance(record, dict)
            else "launch_review_required"
        ),
        "session_id": session_id,
        "project": _redacted_project(project_payload),
        "workspace": _redacted_workspace(workspace_row),
        "dashboard_review_url": _review_url(selected, session_id),
        "next": (
            f"genaug launch --provision --approve-session {session_id} --json"
            if approved
            else (
                "Approve in the dashboard, then run genaug launch --provision "
                f"--approve-session {session_id} --json"
            )
        ),
    }


def _launch_questions(runtime: Runtime, inspection: dict[str, Any]) -> dict[str, Any]:
    installer = installer_auth_metadata(runtime.config)
    workspaces: list[dict[str, object]] = []
    projects: list[dict[str, object]] = []
    if installer is not None:
        token = installer_access_token(runtime)
        with runtime.client() as client:
            workspace_payload = client.installer("GET", "/workspaces", token=token)
            project_payload = client.installer("GET", "/projects", token=token)
        workspaces = _items(workspace_payload)
        projects = _project_items(project_payload)
    return launch_questions(
        inspection,
        authenticated=installer is not None,
        active_workspace=runtime.config.active_workspace,
        active_project=runtime.config.active_project,
        workspaces=workspaces,
        projects=projects,
    )


def _select_or_create_workspace(
    client: Any,
    *,
    token: str,
    payload: object,
    reference: str | None,
    manifest: dict[str, Any],
) -> dict[str, object]:
    rows = _items(payload)
    declared = _manifest_context(manifest).get("workspace")
    choice = declared if isinstance(declared, dict) else {}
    selected_ref = reference or str(choice.get("ref") or "") or None
    if selected_ref:
        matches = [
            row
            for row in rows
            if selected_ref
            in {
                str(row.get("id") or ""),
                str(row.get("slug") or ""),
                str(row.get("name") or ""),
            }
        ]
        if len(matches) == 1:
            return matches[0]
        if choice.get("create") is not True:
            raise CLIError("Workspace reference did not match exactly one visible Workspace.")
    if choice.get("create") is True:
        return dict(
            client.installer(
                "POST",
                "/workspaces",
                token=token,
                json={"name": str(choice["name"]), "slug": str(choice["slug"])},
            )
        )
    if len(rows) == 1:
        return rows[0]
    raise CLIError(
        "Multiple Workspaces are available. Pass --account-workspace or run "
        "genaug launch --questions --json."
    )


def _manifest_context(manifest: dict[str, Any]) -> dict[str, Any]:
    contract = manifest.get("x-general-augment-launch")
    if not isinstance(contract, dict):
        return {}
    project = contract.get("project")
    return dict(project) if isinstance(project, dict) else {}


def _items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [dict(row) for row in payload["items"] if isinstance(row, dict)]


def _redacted_workspace(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload.get(key, "")
        for key in ("id", "name", "slug", "kind", "role")
    }


def _launch_artifact(
    workspace: Path,
    manifest_path: Path,
    inspection: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Bind review identity to exact deploy bytes and executable repository contract."""

    try:
        deploy_payload = load_deploy_payload(
            manifest_path,
            yaml_content=_manifest_text(workspace, manifest_path),
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    artifact = launch_session_artifact(
        inspection,
        manifest,
        configuration=deploy_payload,
    )
    return bind_application_command_contract(artifact, workspace, manifest)


def _provision(
    runtime: Runtime,
    workspace: Path,
    manifest_path: Path,
    project_ref: str,
    session_id: str,
    artifact: dict[str, Any],
    *,
    configure_application_env: bool,
    remove_application_env: bool,
    application_env_file: Path | None,
    rotate_runtime_key: bool,
) -> dict[str, Any]:
    """Apply an approved plan and separately provision its runtime credential."""
    if session_id != artifact["session_id"]:
        raise CLIError("Approved session does not match the current manifest and inspection plan.")
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Installer auth expired. Run genaug auth login again.")
    if rotate_runtime_key and not configure_application_env:
        raise CLIError(
            "Runtime-key rotation requires --configure-application-env so the replacement "
            "is installed before the prior key is revoked."
        )
    token = installer_access_token(runtime)
    runtime_mode = _release_intent(artifact)
    activation_values = reviewed_activation_environment_values(artifact)
    environment: dict[str, object] | None = None
    with runtime.client() as client:
        projects_payload = client.installer("GET", "/projects", token=token)
        items = _project_items(projects_payload)
        project_payload = next(
            (
                item
                for item in items
                if project_ref
                in {str(item.get("id", "")), str(item.get("slug", "")), str(item.get("name", ""))}
            ),
            None,
        )
        if project_payload is None:
            raise CLIError(f"Linked launch project not found: {project_ref}")
        selected = str(project_payload["id"])
        review = client.installer(
            "GET",
            f"/projects/{encode_path_segment(selected)}/launch-sessions/{encode_path_segment(session_id)}",
            token=token,
        )
        if not isinstance(review, dict) or review.get("status") != "approved":
            raise CLIError("Launch session is not approved in the dashboard.")
        deployed = deploy_path_with_installer(
            runtime,
            manifest_path,
            selected,
            token=token,
            launch_session_id=session_id,
            yaml_content=_manifest_text(workspace, manifest_path),
        )
        release_payload = client.installer(
            "POST",
            f"/projects/{encode_path_segment(selected)}/releases",
            token=token,
        )
        if not isinstance(release_payload, dict) or not release_payload.get("id"):
            raise CLIError("Project release creation returned an invalid candidate.")
        if runtime_mode != "test":
            raise CLIError("Candidate certification may provision only the Test runtime.")
        preview = provision_release_preview(
            client,
            token=token,
            config=runtime.config,
            project_id=selected,
            launch_session_id=session_id,
            release=dict(release_payload),
            rotate=rotate_runtime_key,
        )
        updated_config = preview.config
        key_metadata = preview.key
        key_action = preview.action
        confirmed_matching = [key_metadata]
        save_config(updated_config, runtime.config_path)

    if environment is None:
        environment = _application_environment_handoff(
            workspace,
            application_env_file,
            config=updated_config,
            configure=configure_application_env,
            remove=remove_application_env,
            activation_values=activation_values,
        )
    receipt_path = _write_provisioning_receipt(
        workspace,
        manifest_path=manifest_path,
        session_id=session_id,
        project_id=selected,
        approved_fingerprint=str(review.get("fingerprint") or ""),
        key=confirmed_matching[0],
        key_action=key_action,
        active_matching_count=len(confirmed_matching),
        environment=environment,
        release=release_payload,
    )
    return {
        "phase": "provision",
        "status": "PROVISIONED",
        "session_id": session_id,
        "project": _redacted_project(deployed),
        "runtime_key": {
            "action": key_action,
            "id": confirmed_matching[0].get("id"),
            "masked_key": confirmed_matching[0].get("masked_key"),
            "scopes": list(_RUNTIME_KEY_SCOPES),
            "stored_as": "runtime_api_key",
            "active_matching_count": len(confirmed_matching),
            "authority": "candidate_test_preview",
            "expires_at": key_metadata.get("expires_at"),
        },
        "control_plane_authority": "installer",
        "application_authority": "runtime_api_key",
        "release": {
            "id": str(release_payload.get("id") or ""),
            "fingerprint": str(release_payload.get("fingerprint") or ""),
            "status": str(release_payload.get("status") or "candidate"),
            "intent": _release_intent(artifact),
        },
        "environment": environment,
        "provisioning_receipt": str(receipt_path),
        "safe_mode": True,
        "next": (
            "genaug launch --apply --json"
            if environment["status"] in {"configured", "removed"}
            else (
                "Re-run provision with --configure-application-env, then run "
                "genaug launch --apply --json"
            )
        ),
    }


def _verify_launch(
    runtime: Runtime,
    workspace: Path,
    manifest_path: Path,
    project_ref: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    manifest = _load_manifest(workspace, manifest_path)
    validation = validate_local_agent_config(
        manifest_path,
        yaml_content=_manifest_text(workspace, manifest_path),
    )
    compatible, compatibility_reasons = compatibility_status(
        cli_version=str(artifact["cli_version"]),
        skill_version=str(artifact["skill_version"]),
        manifest_schema_version=str(artifact["manifest_schema_version"]),
    )
    if validation.errors:
        compatible = False
        compatibility_reasons = [
            *compatibility_reasons,
            "manifest_validation_failed",
        ]
    installer = installer_auth_metadata(runtime.config)
    # Verification can run well after the browser login completed. Resolve installer
    # authority through the refresh-aware path instead of reusing a stale access token.
    installer_token = installer_access_token(runtime) if installer else None
    runtime_api_key_value = getattr(runtime.config, "runtime_api_key", None)
    runtime_api_key = (
        str(runtime_api_key_value)
        if isinstance(runtime_api_key_value, str) and runtime_api_key_value
        else None
    )
    local_checks: list[dict[str, Any]] = []
    local_artifact: dict[str, Any] = {}
    hosted_checks: list[dict[str, Any]] = []
    hosted_artifact: dict[str, Any] = {}
    if not validation.errors:
        with runtime.client() as client:
            preflight_checks, preflight_artifact = collect_hosted_preflight_checks(
                client,
                installer_token=installer_token,
                project_id=project_ref,
                artifact=artifact,
                compatible=compatible,
                compatibility_reasons=compatibility_reasons,
            )
            if all(row.get("status") == "PASS" for row in preflight_checks):
                plan_value = artifact.get("plan")
                plan = plan_value if isinstance(plan_value, dict) else {}
                command_contract_value = plan.get("application_command_contract")
                command_contract = (
                    command_contract_value
                    if isinstance(command_contract_value, dict)
                    else {}
                )
                local_checks, local_artifact = collect_application_checks(
                    manifest_path.parent,
                    manifest,
                    runtime_api_key=runtime_api_key,
                    runtime_api_base_url=getattr(runtime.config, "base_url", None),
                    project_id=project_ref,
                    approved_command_contract_sha=str(command_contract.get("sha256") or ""),
                    launch_fingerprint_value=launch_session_fingerprint(artifact),
                    manifest_fingerprint_value=manifest_fingerprint(manifest),
                )
            hosted_checks, hosted_artifact = collect_hosted_checks(
                client,
                installer_token=installer_token,
                runtime_api_key=runtime_api_key,
                runtime_key_id=getattr(runtime.config, "runtime_key_id", None),
                runtime_key_scopes=getattr(runtime.config, "runtime_key_scopes", ()),
                project_id=project_ref,
                artifact=artifact,
                manifest=manifest,
                manifest_path=manifest_path,
                provisioning_receipt_path=workspace / ".genaug" / "provisioning-receipt.json",
                compatible=compatible,
                compatibility_reasons=compatibility_reasons,
                preflight_checks=preflight_checks,
                preflight_artifact=preflight_artifact,
            )
            correlated_checks = correlate_application_checks(
                client,
                [*hosted_checks, *local_checks],
                installer_token=installer_token,
                project_id=project_ref,
                artifact=artifact,
                manifest=manifest,
            )
            hosted_names = {str(row.get("name") or "") for row in hosted_checks}
            hosted_checks = [
                row for row in correlated_checks if str(row.get("name") or "") in hosted_names
            ]
            local_checks = [
                row for row in correlated_checks if str(row.get("name") or "") not in hosted_names
            ]
    else:
        hosted_checks = [
            check_result(
                "cli_api_skill_manifest_compatibility",
                "FAIL",
                "manifest_validation_failed",
                "The launch manifest failed local validation.",
                evidence=[{"manifest_fingerprint": manifest_fingerprint(manifest)}],
            )
        ]
    verification = evaluate_launch_verification(
        manifest,
        [*hosted_checks, *local_checks],
        optional_warnings=("manifest_validation_warning" for _ in validation.warnings),
    )
    review_url = _review_url(project_ref, str(artifact["session_id"]))
    payload = {
        "phase": "verify",
        **verification,
        "session_id": artifact["session_id"],
        "compatibility": {
            "ok": compatible,
            "cli_version": artifact["cli_version"],
            "skill_version": artifact["skill_version"],
            "manifest_schema_version": artifact["manifest_schema_version"],
        },
        "manifest": {
            "status": validation.status,
            "errors": validation.errors,
            "warnings": validation.warnings,
        },
        "hosted": hosted_artifact,
        "application": local_artifact,
        "dashboard_review_url": review_url,
    }
    receipt_path = write_verification_receipt(
        workspace / ".genaug" / "launch-verification.json",
        payload,
        workspace=workspace,
    )
    payload["verification_receipt_path"] = str(receipt_path)
    payload["activation"] = {
        "performed": False,
        "reason": "verification_is_side_effect_free",
        "next_action": "Run `genaug launch --finalize --json` after reviewing READY evidence.",
    }
    return payload


def _finalize_launch(
    runtime: Runtime,
    workspace: Path,
    manifest_path: Path,
    artifact: dict[str, Any],
    project_id: str,
    *,
    configure_application_env: bool,
    application_env_file: Path | None,
    activation_values: dict[str, str],
) -> dict[str, Any]:
    """Promote only a complete READY receipt, then replace preview authority."""
    verification = _read_json_receipt(
        workspace,
        workspace / ".genaug" / "launch-verification.json",
        description="launch verification receipt",
    )
    provisioning = _read_json_receipt(
        workspace,
        workspace / ".genaug" / "provisioning-receipt.json",
        description="launch provisioning receipt",
    )
    if verification.get("verdict") != "READY":
        raise CLIError("Launch finalization requires a current READY verification receipt.")
    if not evidence_is_fresh(verification.get("verified_at")):
        raise CLIError("Launch verification evidence is missing or stale; run verify again.")
    current_manifest = _load_manifest(workspace, manifest_path)
    if verification.get("manifest_fingerprint") != manifest_fingerprint(current_manifest):
        raise CLIError("Launch verification does not match the current manifest.")
    if any(
        str(receipt.get("session_id") or "") != str(artifact.get("session_id") or "")
        for receipt in (verification, provisioning)
    ):
        raise CLIError("Launch receipts do not match the current reviewed session.")
    manifest_content = read_text_no_follow(
        workspace,
        manifest_path,
        description="launch manifest path",
    )
    if manifest_content is None:
        raise CLIError("Launch manifest disappeared before finalization.")
    if any(
        (
            provisioning.get("schema_version") != _PROVISIONING_RECEIPT_SCHEMA,
            str(provisioning.get("project_id") or "") != project_id,
            str(provisioning.get("approved_plan_fingerprint") or "")
            != launch_session_fingerprint(artifact),
            str(provisioning.get("manifest_sha256") or "")
            != hashlib.sha256(manifest_content.encode("utf-8")).hexdigest(),
            not evidence_is_fresh(provisioning.get("checked_at")),
        )
    ):
        raise CLIError("Provisioning receipt is stale or does not match the reviewed plan.")
    checks_value = verification.get("checks")
    checks = [dict(row) for row in checks_value if isinstance(row, dict)] if isinstance(
        checks_value, list
    ) else []
    names = [str(row.get("name") or "") for row in checks]
    if sorted(names) != sorted(REQUIRED_BETA_CHECKS) or any(
        row.get("required") is not True or row.get("status") != "PASS" for row in checks
    ):
        raise CLIError("READY receipt is missing a required PASS result.")
    release_value = provisioning.get("release")
    release = dict(release_value) if isinstance(release_value, dict) else {}
    release_id = str(release.get("id") or "")
    release_fingerprint = str(release.get("fingerprint") or "")
    if not release_id or len(release_fingerprint) != 64:
        raise CLIError("Provisioning receipt is missing the candidate release identity.")
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Installer auth expired. Run genaug auth login again.")
    with runtime.client() as client:
        finalization = finalize_verified_release(
            client,
            token=installer_access_token(runtime),
            config=runtime.config,
            project_id=project_id,
            release_id=release_id,
            release_fingerprint=release_fingerprint,
            checks=[_server_release_check(row) for row in checks],
        )
        environment_value = provisioning.get("environment")
        prior_environment = (
            dict(environment_value) if isinstance(environment_value, dict) else {}
        )
        configured_before = prior_environment.get("status") == "configured"
        target_value = application_env_file or (
            Path(str(prior_environment["target"])) if prior_environment.get("target") else None
        )
        try:
            environment = _application_environment_handoff(
                workspace,
                target_value,
                config=finalization.config,
                configure=configure_application_env or configured_before,
                remove=False,
                activation_values=activation_values,
            )
            save_config(finalization.config, runtime.config_path)
        except Exception:
            if finalization.action == "created":
                key_id = str(finalization.key.get("id") or "")
                if key_id:
                    try:
                        client.installer(
                            "DELETE",
                            (
                                f"/projects/{encode_path_segment(project_id)}/runtime-keys/"
                                f"{encode_path_segment(key_id)}"
                            ),
                            token=installer_access_token(runtime),
                        )
                    except Exception:
                        pass
            raise
    receipt = {
        "schema_version": "general-augment-launch-finalization/v1",
        "session_id": artifact["session_id"],
        "project_id": project_id,
        "release_id": release_id,
        "release_fingerprint": release_fingerprint,
        "runtime_mode": "test",
        "runtime_key_id": str(finalization.key.get("id") or ""),
        "runtime_key_action": finalization.action,
        "environment": environment,
        "finalized_at": datetime.now(UTC).isoformat(),
    }
    receipt_path = workspace / ".genaug" / "launch-finalization.json"
    atomic_write_text_no_follow(
        workspace,
        receipt_path,
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        description="launch finalization receipt",
    )
    return {
        "phase": "finalize",
        "status": "FINALIZED",
        "session_id": artifact["session_id"],
        "release": {
            "id": release_id,
            "fingerprint": release_fingerprint,
            "status": "verified",
            "runtime_mode": "test",
        },
        "runtime_key": {
            "action": finalization.action,
            "id": finalization.key.get("id"),
            "masked_key": finalization.key.get("masked_key"),
            "scopes": list(_RUNTIME_KEY_SCOPES),
            "authority": "durable_test_runtime",
        },
        "environment": environment,
        "finalization_receipt": str(receipt_path),
        "next": "Open the exact run and trace links from the READY receipt.",
    }


def _read_json_receipt(workspace: Path, path: Path, *, description: str) -> dict[str, Any]:
    content = read_text_no_follow(workspace, path, description=description)
    if content is None:
        raise CLIError(f"Missing {description}.")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Invalid {description}.") from exc
    if not isinstance(payload, dict):
        raise CLIError(f"Invalid {description}.")
    return dict(payload)


def _server_release_check(row: dict[str, Any]) -> dict[str, Any]:
    """Project one normalized CLI result into the server-owned verification schema."""
    raw_evidence = row.get("evidence")
    evidence: list[Any] = raw_evidence if isinstance(raw_evidence, list) else []
    evidence_ids: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        for key in ("response_id", "run_id", "trace_id", "request_id", "artifact_sha256", "url"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in evidence_ids:
                evidence_ids.append(value)
    if not evidence_ids and evidence:
        digest = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        evidence_ids.append(f"sha256:{digest}")
    return {
        "name": str(row.get("name") or ""),
        "status": str(row.get("status") or "SKIP"),
        "reason_code": str(row.get("reason_code") or "verification_result_missing"),
        "detail": str(row.get("detail") or "Verification result did not include detail."),
        "evidence_ids": evidence_ids[:20],
        "timestamp": str(row.get("checked_at") or datetime.now(UTC).isoformat()),
    }


def _runtime_key_items(payload: object) -> list[dict[str, Any]]:
    """Return secret-free runtime-key rows from one installer response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [dict(item) for item in payload["items"] if isinstance(item, dict)]


def _matching_launch_runtime_keys(
    items: list[dict[str, Any]],
    *,
    runtime_mode: str,
) -> list[dict[str, Any]]:
    """Return active keys that implement the one-prompt runtime role."""
    expected = set(_RUNTIME_KEY_SCOPES)
    return [
        item
        for item in items
        if item.get("name") == _LAUNCH_RUNTIME_KEY_NAME
        and item.get("runtime_mode") == runtime_mode
        and {str(scope) for scope in item.get("scopes", [])} == expected
    ]


def _reusable_runtime_key(
    config: CLIConfig,
    project_id: str,
    matching: list[dict[str, Any]],
    *,
    runtime_mode: str,
) -> dict[str, Any] | None:
    """Return the matching stored runtime key only when its role is unambiguous."""
    if (
        not config.runtime_api_key
        or config.runtime_key_project_id != project_id
        or set(config.runtime_key_scopes) != set(_RUNTIME_KEY_SCOPES)
        or config.runtime_key_mode != runtime_mode
        or not config.runtime_key_id
    ):
        return None
    return next(
        (item for item in matching if str(item.get("id") or "") == config.runtime_key_id),
        None,
    )


def _application_environment_handoff(
    workspace: Path,
    target: Path | None,
    *,
    config: CLIConfig,
    configure: bool,
    remove: bool,
    activation_values: dict[str, str],
) -> dict[str, object]:
    """Apply or remove the local runtime environment without returning values."""
    resolved_target = target
    if target is not None and not target.is_absolute():
        resolved_target = workspace / target
    adapter = LocalEnvFileAdapter(workspace, resolved_target)
    if remove:
        return adapter.remove().as_dict()
    if not configure:
        return {
            "status": "action_required",
            "target": str(adapter.target),
            "variables": [
                "GENAUG_API_KEY",
                "GENAUG_PROJECT_ID",
                "GENAUG_API_BASE_URL",
                *sorted(activation_values),
            ],
            "detail": "Explicit --configure-application-env approval was not supplied.",
        }
    if not config.runtime_api_key:
        raise CLIError("Runtime key is unavailable for application environment configuration.")
    return adapter.apply(
        {
            "GENAUG_API_KEY": config.runtime_api_key,
            "GENAUG_PROJECT_ID": str(config.runtime_key_project_id or ""),
            "GENAUG_API_BASE_URL": config.base_url.rstrip("/"),
            **activation_values,
        }
    ).as_dict()


def _write_provisioning_receipt(
    workspace: Path,
    *,
    manifest_path: Path,
    session_id: str,
    project_id: str,
    approved_fingerprint: str,
    key: dict[str, Any],
    key_action: str,
    active_matching_count: int,
    environment: dict[str, object],
    release: dict[str, Any],
) -> Path:
    """Persist deterministic non-secret provisioning evidence for strict verify."""
    if not approved_fingerprint:
        raise CLIError("Approved launch session did not return its plan fingerprint.")
    target = workspace / ".genaug" / "provisioning-receipt.json"
    manifest_content = read_text_no_follow(
        workspace,
        manifest_path,
        description="launch manifest path",
    )
    if manifest_content is None:
        raise CLIError("Launch manifest disappeared before the provisioning receipt was written.")
    payload = {
        "schema_version": _PROVISIONING_RECEIPT_SCHEMA,
        "session_id": session_id,
        "approved_plan_fingerprint": approved_fingerprint,
        "manifest_sha256": hashlib.sha256(manifest_content.encode("utf-8")).hexdigest(),
        "project_id": project_id,
        "runtime_key": {
            "id": str(key.get("id") or ""),
            "masked_key": str(key.get("masked_key") or ""),
            "scopes": list(_RUNTIME_KEY_SCOPES),
            "action": key_action,
            "active_matching_count": active_matching_count,
            "authority": "candidate_test_preview",
            "preview_binding_id": str(key.get("preview_binding_id") or ""),
            "expires_at": str(key.get("expires_at") or ""),
        },
        "release": {
            "id": str(release.get("id") or ""),
            "fingerprint": str(release.get("fingerprint") or ""),
            "status": str(release.get("status") or "candidate"),
        },
        "authorities": {
            "configuration": "installer",
            "runtime_execution": "candidate_test_preview",
            "dashboard_review": "human_dashboard",
        },
        "environment": {
            "status": environment.get("status"),
            "target": environment.get("target"),
            "variables": environment.get("variables", []),
        },
        "checked_at": datetime.now(UTC).isoformat(),
    }
    atomic_write_text_no_follow(
        workspace,
        target,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        description="provisioning receipt",
    )
    return target


def _release_intent(artifact: dict[str, Any]) -> str:
    """Return the exact reviewed Test/Live intent, failing closed on unknown values."""
    raw_plan = artifact.get("plan")
    plan: dict[str, Any] = dict(raw_plan) if isinstance(raw_plan, dict) else {}
    raw_release = plan.get("release")
    release: dict[str, Any] = (
        dict(raw_release) if isinstance(raw_release, dict) else {}
    )
    intent = str(release.get("intent") or "test")
    if intent not in {"test", "live"}:
        raise CLIError("Reviewed release intent must be test or live.")
    return intent


def _write_handoff(root: Path, manifest_path: Path, artifact: dict[str, Any]) -> Path:
    target = root / ".genaug" / "launch-handoff.md"
    atomic_write_text_no_follow(
        root,
        target,
        "\n".join(
            [
                "# General Augment launch handoff",
                "",
                f"Session: `{artifact['session_id']}`",
                f"Manifest: `{manifest_path}`",
                "",
                "Use the official `generalaugment-launch` skill version "
                f"`{artifact['skill_version']}`.",
                "",
                "- Preserve the application's authentication and authorization.",
                "- Resolve the signed-in user on the server and pass that stable ID as `user`.",
                "- Keep `GENAUG_API_KEY` server-only.",
                "- Add one read-only application capability.",
                "- Keep writes in the application's existing confirmation path.",
                "- Add focused tests and run the manifest verification commands.",
                "- Finish with `genaug launch --verify --json`.",
                "",
            ]
        ),
        description="launch handoff",
    )
    return target


def _review_url(project_id: str, session_id: str) -> str:
    return dashboard_launch_url(project_id, session_id)


def _load_manifest(workspace: Path, path: Path) -> dict[str, Any]:
    content = _manifest_text(workspace, path)
    payload = yaml.safe_load(content)
    if not isinstance(payload, dict):
        raise CLIError("Launch manifest must contain a YAML object.")
    return payload


def _manifest_text(workspace: Path, path: Path) -> str:
    content = read_text_no_follow(
        workspace,
        path,
        description="launch manifest path",
    )
    if content is None:
        raise CLIError("Launch manifest not found. Run genaug launch --plan first.")
    return content


def _redacted_project(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {key: payload.get(key) for key in ("id", "slug", "name") if payload.get(key)}


def _emit(payload: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print_json(payload)
        return
    rows = [
        ["Phase", payload.get("phase", "launch")],
        ["Status", payload.get("status") or payload.get("verdict")],
    ]
    if payload.get("session_id"):
        rows.append(["Session", payload["session_id"]])
    if payload.get("dashboard_review_url"):
        rows.append(["Review", payload["dashboard_review_url"]])
    table("General Augment launch", ["Field", "Value"], rows)
    next_action = payload.get("next")
    if next_action:
        print_success(f"Next: {next_action}")
