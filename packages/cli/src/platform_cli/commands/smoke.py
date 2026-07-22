"""Smoke-test app-facing General Augment endpoints."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import panel, print_json, print_success, table
from platform_cli.redaction import redact_metadata
from platform_cli.runtime import Runtime
from platform_cli.self_serve import dashboard_observability_url, dashboard_project_url

DEFAULT_SMOKE_MESSAGE = "Reply exactly with: genaug-smoke-ok"
DEFAULT_STRUCTURED_MESSAGE = 'Return JSON with ok=true and label="genaug-smoke-ok".'
DEFAULT_MEMORY_RECALL_MESSAGE = (
    "Use durable memory for this user. Reply with only the General Augment smoke recall code."
)
EXPECTED_SMOKE_TOKEN = "genaug-smoke-ok"
_COMPLETED_STATUSES = {"completed", "complete", ""}
DEFAULT_STRUCTURED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "label": {"type": "string"},
    },
    "required": ["ok", "label"],
}


def smoke(
    ctx: typer.Context,
    message: Annotated[
        str,
        typer.Option("--message", "-m", help="Responses input for the smoke turn."),
    ] = DEFAULT_SMOKE_MESSAGE,
    user: Annotated[str, typer.Option(help="App user id for the smoke turn.")] = "genaug-smoke",
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Replay-safe key for retry/debug checks."),
    ] = None,
    request_id: Annotated[
        str | None,
        typer.Option(help="Caller request id to propagate."),
    ] = None,
    traceparent: Annotated[
        str | None,
        typer.Option(help="W3C traceparent header to propagate."),
    ] = None,
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    structured: Annotated[
        bool,
        typer.Option("--structured", help="Request a json_schema structured-output smoke."),
    ] = False,
    schema_file: Annotated[
        Path | None,
        typer.Option(
            "--schema-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="JSON Schema file for --structured smoke output.",
        ),
    ] = None,
    raw: Annotated[bool, typer.Option("--json", help="Print the raw response JSON.")] = False,
    evidence_output: Annotated[
        Path | None,
        typer.Option(
            "--evidence-output",
            "-o",
            help="Write redacted smoke evidence JSON for launch/support review.",
        ),
    ] = None,
    import_evidence: Annotated[
        bool,
        typer.Option(
            "--import-evidence",
            help="Retain the smoke evidence in project launch-readiness audit metadata.",
        ),
    ] = False,
    include_support_bundle: Annotated[
        bool,
        typer.Option(
            "--include-support-bundle",
            help="Fetch and embed the project support bundle; requires --project and admin auth.",
        ),
    ] = False,
    memory_recall: Annotated[
        bool,
        typer.Option(
            "--memory-recall",
            help="Seed and verify durable-memory recall through the app-facing Responses path.",
        ),
    ] = False,
) -> None:
    """Run health plus one `/v1/responses` smoke request."""
    runtime: Runtime = ctx.obj
    if include_support_bundle and not project:
        raise CLIError("--include-support-bundle requires --project.")
    if import_evidence and not project:
        raise CLIError("--import-evidence requires --project.")
    turn_id = uuid.uuid4().hex[:12]
    schema = _load_schema(schema_file) if schema_file else None
    structured = structured or schema is not None
    if structured and message == DEFAULT_SMOKE_MESSAGE:
        message = DEFAULT_STRUCTURED_MESSAGE
    memory_recall_code = f"genaug-memory-{turn_id}" if memory_recall else None
    if memory_recall and message == DEFAULT_SMOKE_MESSAGE:
        message = DEFAULT_MEMORY_RECALL_MESSAGE
    # We can only assert the exact echoed token for the built-in prompts and the
    # built-in schema. A custom --message/--schema-file or the memory-recall prompt
    # means we only require a well-formed, non-empty response (or valid JSON).
    expects_default_token = (
        message in {DEFAULT_SMOKE_MESSAGE, DEFAULT_STRUCTURED_MESSAGE} and schema is None
    )
    headers = _correlation_headers(
        idempotency_key=idempotency_key or f"genaug-smoke-{turn_id}",
        request_id=request_id or f"req_genaug_smoke_{turn_id}",
        traceparent=traceparent,
    )
    payload = {
        "model": "balanced",
        "user": user,
        "input": message,
        "metadata": {"source": "genaug-cli-smoke", **_metadata_pairs(metadata or [])},
    }
    if structured:
        payload["text"] = _structured_text_format(schema or DEFAULT_STRUCTURED_SCHEMA)
    project_payload: dict[str, Any] | None = None
    support_bundle: dict[str, Any] | None = None
    memory_seed: dict[str, Any] | None = None
    with runtime.client() as client:
        ready = client.public("GET", "/health/ready")
        project_headers: dict[str, str] = {}
        if project:
            project_payload = resolve_project(client, project)
            headers["X-Project-ID"] = str(project_payload["id"])
            project_headers["X-Project-ID"] = str(project_payload["id"])
        if memory_recall_code is not None:
            memory_seed = client.app(
                "POST",
                "/api/v1/agent/memory/store",
                json=_memory_recall_seed_payload(
                    user=user,
                    recall_code=memory_recall_code,
                    turn_id=turn_id,
                ),
                headers=project_headers,
            )
        response = client.app("POST", "/v1/responses", json=payload, headers=headers)
        metadata_payload = response.get("metadata", {}) if isinstance(response, dict) else {}
        if include_support_bundle and project_payload is not None:
            support_bundle = client.admin(
                "GET",
                (
                    f"/projects/{encode_path_segment(str(project_payload['id']))}"
                    "/observability/support-bundle"
                ),
                params=_support_bundle_params(
                    trace_id=_metadata_value(
                        metadata_payload,
                        "general_augment_trace_id",
                        "trace_id",
                    ),
                    response_id=response.get("id") if isinstance(response, dict) else None,
                    user_id=user,
                ),
            )

    support_receipt = _support_receipt(
        ready=ready,
        response=response,
        metadata=metadata_payload,
        project=project_payload,
    )
    memory_recall_evidence = _memory_recall_evidence(
        enabled=memory_recall,
        recall_code=memory_recall_code,
        seed_response=memory_seed,
        response=response,
        prompt=message,
    )
    dashboard_urls = _dashboard_evidence_urls(
        project=project_payload,
        trace_id=support_receipt.get("trace_id"),
        response_id=support_receipt.get("response_id"),
        user_id=user,
    )
    smoke_evidence = _smoke_evidence(
        ready=ready,
        response=response,
        support_receipt=support_receipt,
        dashboard_urls=dashboard_urls,
        support_bundle=support_bundle,
        memory_recall=memory_recall_evidence,
    )
    # Gate on the actual response body, not just an HTTP 200: a 200 with an empty or
    # wrong reply must fail so an agent that checks the exit code does not ship a
    # broken agent.
    verdict = _smoke_verdict(
        ready=ready,
        response=response,
        structured=structured,
        expects_default_token=expects_default_token,
    )
    smoke_failed = verdict["verdict"] != "PASS" or memory_recall_evidence.get("status") == "failed"
    evidence_import: dict[str, Any] | None = None
    if import_evidence and project_payload is not None:
        with runtime.client() as client:
            evidence_import = client.admin(
                "POST",
                (
                    f"/projects/{encode_path_segment(str(project_payload['id']))}"
                    "/launch-readiness/evidence"
                ),
                json={
                    "artifact": smoke_evidence,
                    "artifact_type": "smoke_evidence",
                    "source": "cli",
                    "artifact_path": str(evidence_output) if evidence_output else None,
                },
            )
    if evidence_output is not None:
        _write_evidence(evidence_output, smoke_evidence)
    if raw:
        print_json(
            {
                "ready": ready,
                "response": response,
                "response_id": response.get("id") if isinstance(response, dict) else None,
                "request_id": _metadata_value(
                    metadata_payload,
                    "general_augment_request_id",
                    "request_id",
                ),
                "trace_id": _metadata_value(
                    metadata_payload,
                    "general_augment_trace_id",
                    "trace_id",
                ),
                "support_receipt": support_receipt,
                "dashboard_urls": dashboard_urls,
                "memory_recall": memory_recall_evidence,
                "evidence_import": evidence_import,
                "evidence": smoke_evidence,
                "verdict": verdict["verdict"],
                "verdict_detail": verdict["detail"],
            }
        )
        if smoke_failed:
            raise typer.Exit(1)
        return

    rows: list[list[object]] = [["Ready", _status_text(ready)]]
    if isinstance(response, dict):
        rows.extend(
            [
                ["Response ID", response.get("id", "")],
                ["Status", response.get("status", "")],
                ["Model", response.get("model", metadata_payload.get("general_augment_model", ""))],
                [
                    "Latency",
                    _display_metric(
                        _metadata_value(metadata_payload, "general_augment_latency_ms"),
                        suffix=" ms",
                    ),
                ],
                [
                    "Tokens",
                    _token_summary(response=response, metadata=metadata_payload),
                ],
                [
                    "Cost",
                    _display_metric(
                        _metadata_value(
                            metadata_payload,
                            "general_augment_cost_usd",
                            "cost_usd",
                        )
                    ),
                ],
                [
                    "Request ID",
                    metadata_payload.get(
                        "request_id",
                        metadata_payload.get("general_augment_request_id", ""),
                    ),
                ],
                [
                    "Trace ID",
                    metadata_payload.get(
                        "trace_id",
                        metadata_payload.get("general_augment_trace_id", ""),
                    ),
                ],
            ]
        )
        if structured:
            rows.append(["Output Format", "json_schema"])
        if memory_recall_evidence.get("enabled"):
            rows.append(["Memory Recall", memory_recall_evidence.get("status", "unknown")])
        if dashboard_urls.get("observability_url"):
            rows.append(["Dashboard", dashboard_urls["observability_url"]])
        if evidence_output is not None:
            rows.append(["Evidence", evidence_output])
        if evidence_import is not None:
            rows.append(["Evidence Import", evidence_import.get("audit_event_id", "retained")])
    rows.append(["Verdict", verdict["verdict"]])
    table("Smoke", ["Check", "Value"], rows)
    if isinstance(response, dict):
        panel("Output", _response_output_text(response) or "<empty>")
        panel("Support receipt", json.dumps(support_receipt, indent=2, sort_keys=True))
    if evidence_output is not None:
        print_success(f"Wrote smoke evidence to {evidence_output}.")
    if smoke_failed:
        raise CLIError(verdict["detail"])
    print_success("Smoke passed: the agent returned the expected response.")


def _correlation_headers(
    *,
    idempotency_key: str,
    request_id: str,
    traceparent: str | None,
) -> dict[str, str]:
    """Build app-facing correlation headers."""
    headers = {
        "X-Idempotency-Key": idempotency_key,
        "X-Request-ID": request_id,
    }
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


def _metadata_pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated key=value metadata flags."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("--metadata values must use key=value.")
        parsed[key.strip()] = value
    return parsed


def _support_bundle_params(
    *,
    trace_id: object,
    response_id: object,
    user_id: str,
) -> dict[str, object]:
    """Build bounded support-bundle filters for a smoke turn."""
    params: dict[str, object] = {"limit": 25, "user_id": user_id}
    if trace_id:
        params["trace_id"] = str(trace_id)
    if response_id:
        params["response_id"] = str(response_id)
    return params


def _load_schema(path: Path) -> dict[str, Any]:
    """Load a JSON Schema object from disk."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("--schema-file must contain a JSON object schema.")
    return payload


def _structured_text_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Build a Responses json_schema text format."""
    return {
        "format": {
            "type": "json_schema",
            "name": "genaug_smoke",
            "schema": schema,
            "strict": True,
        }
    }


def _smoke_verdict(
    *,
    ready: object,
    response: object,
    structured: bool,
    expects_default_token: bool,
) -> dict[str, str]:
    """Decide PASS/FAIL from the actual response body, not just an HTTP 200.

    An agent gates on the exit code, so a 200 with an empty or wrong body must
    fail. The detail string doubles as the error message + next step on failure.
    """
    next_step = "Check the project model routing and provider setup, then rerun genaug smoke."
    if not _health_ok(ready):
        return _verdict_fail(f"Platform health check was not ready. {next_step}")
    if not isinstance(response, dict):
        return _verdict_fail(f"Responses call did not return a JSON object. {next_step}")
    if not str(response.get("id") or ""):
        return _verdict_fail(f"Response is missing an id; the turn did not complete. {next_step}")
    status = str(response.get("status") or "")
    if status not in _COMPLETED_STATUSES:
        return _verdict_fail(f"Response status was {status!r}, not completed. {next_step}")
    output_text = _response_output_text(response)
    if not output_text.strip():
        return _verdict_fail(f"Agent returned an empty response body. {next_step}")
    if structured:
        return _structured_verdict(output_text, expects_default_token, next_step)
    if expects_default_token and EXPECTED_SMOKE_TOKEN not in output_text:
        return _verdict_fail(
            f"Agent did not echo the expected smoke token {EXPECTED_SMOKE_TOKEN!r}; "
            f"got {output_text.strip()[:80]!r}. {next_step}"
        )
    return {"verdict": "PASS", "detail": "Agent returned a well-formed smoke response."}


def _structured_verdict(
    output_text: str,
    expects_default_token: bool,
    next_step: str,
) -> dict[str, str]:
    """Validate structured-output smoke responses parse and carry the expected fields."""
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        return _verdict_fail(f"Structured smoke output was not valid JSON. {next_step}")
    if not isinstance(parsed, dict):
        return _verdict_fail(f"Structured smoke output was not a JSON object. {next_step}")
    if expects_default_token:
        if not parsed.get("ok"):
            return _verdict_fail(f"Structured smoke output did not set ok=true. {next_step}")
        if str(parsed.get("label") or "") != EXPECTED_SMOKE_TOKEN:
            return _verdict_fail(
                f"Structured smoke output label was not {EXPECTED_SMOKE_TOKEN!r}. {next_step}"
            )
    return {"verdict": "PASS", "detail": "Agent returned valid structured smoke output."}


def _verdict_fail(detail: str) -> dict[str, str]:
    """Build a failing smoke verdict row."""
    return {"verdict": "FAIL", "detail": detail}


def _health_ok(payload: object) -> bool:
    """Return whether a health payload reports a ready/ok status."""
    if not isinstance(payload, dict):
        return False
    return str(payload.get("status") or "").lower() in {"ok", "ready", "healthy", "pass"}


def _status_text(payload: object) -> str:
    """Return a compact status string from health JSON."""
    if isinstance(payload, dict):
        return str(payload.get("status") or payload)
    return str(payload)


def _response_output_text(response: dict[str, Any]) -> str:
    """Extract text from the common Responses output shape."""
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        for content in item["content"]:
            if (
                isinstance(content, dict)
                and content.get("type") in {"output_text", "text"}
                and isinstance(content.get("text"), str)
            ):
                parts.append(content["text"])
    return "".join(parts)


def _metadata_value(metadata: object, *keys: str) -> object:
    """Return the first metadata value present for any key."""
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = metadata.get(key)
        if value is not None and value != "":
            return value
    return None


def _usage_value(
    response: dict[str, Any],
    metadata: object,
    usage_key: str,
    metadata_key: str,
) -> object:
    """Return a usage value from the Responses usage object or metadata fallback."""
    usage = response.get("usage")
    if isinstance(usage, dict):
        value = usage.get(usage_key)
        if value is not None and value != "":
            return value
    return _metadata_value(metadata, metadata_key)


def _token_summary(*, response: dict[str, Any], metadata: object) -> str:
    """Return a compact token summary for CLI smoke output."""
    input_tokens = _usage_value(
        response,
        metadata,
        "input_tokens",
        "general_augment_input_tokens",
    )
    output_tokens = _usage_value(
        response,
        metadata,
        "output_tokens",
        "general_augment_output_tokens",
    )
    total_tokens = _usage_value(response, metadata, "total_tokens", "general_augment_total_tokens")
    if (
        total_tokens is None
        and isinstance(input_tokens, (int, float))
        and isinstance(output_tokens, (int, float))
    ):
        total_tokens = input_tokens + output_tokens
    if total_tokens is None:
        return "n/a"
    return f"{total_tokens} total ({input_tokens or 0} input, {output_tokens or 0} output)"


def _display_metric(value: object, *, suffix: str = "") -> str:
    """Return a display-safe metric value."""
    if value is None or value == "":
        return "n/a"
    return f"{value}{suffix}"


def _support_receipt(
    *,
    ready: object,
    response: object,
    metadata: object,
    project: dict[str, Any] | None,
) -> dict[str, object]:
    """Build a redacted support receipt for replay/debug handoff."""
    response_payload = response if isinstance(response, dict) else {}
    model = response_payload.get("model")
    if not model:
        model = _metadata_value(metadata, "general_augment_model", "model")
    return {
        "source": "genaug-cli-smoke",
        "project_id": str(project["id"]) if project and project.get("id") else None,
        "project_slug": str(project["slug"]) if project and project.get("slug") else None,
        "response_id": response_payload.get("id"),
        "request_id": _metadata_value(
            metadata,
            "general_augment_request_id",
            "request_id",
        ),
        "trace_id": _metadata_value(
            metadata,
            "general_augment_trace_id",
            "trace_id",
        ),
        "model": model,
        "status": response_payload.get("status"),
        "latency_ms": _metadata_value(metadata, "general_augment_latency_ms"),
        "input_tokens": _usage_value(
            response_payload,
            metadata,
            "input_tokens",
            "general_augment_input_tokens",
        ),
        "output_tokens": _usage_value(
            response_payload,
            metadata,
            "output_tokens",
            "general_augment_output_tokens",
        ),
        "total_tokens": _usage_value(
            response_payload,
            metadata,
            "total_tokens",
            "general_augment_total_tokens",
        ),
        "cost_usd": _metadata_value(metadata, "general_augment_cost_usd", "cost_usd"),
        "ready_status": _status_text(ready),
        "next_action": (
            "Open the response in dashboard observability, then verify trace and "
            "memory evidence before production traffic."
        ),
    }


def _memory_recall_seed_payload(
    *,
    user: str,
    recall_code: str,
    turn_id: str,
) -> dict[str, object]:
    """Build a one-time memory fact for runtime recall smoke proof."""

    return {
        "user_id": user,
        "fact": f"The user's General Augment smoke recall code is {recall_code}.",
        "fact_type": "fact",
        "importance_score": 1.0,
        "source": "genaug-cli-smoke-memory",
        "idempotency_key": f"genaug-smoke-memory-{turn_id}",
        "metadata": {"source": "genaug-cli-smoke", "probe": "memory_recall"},
    }


def _memory_recall_evidence(
    *,
    enabled: bool,
    recall_code: str | None,
    seed_response: object,
    response: object,
    prompt: str,
) -> dict[str, object]:
    """Return bounded evidence for the optional memory-through-runtime smoke probe."""

    if not enabled:
        return {"enabled": False}
    response_text = _response_output_text(response) if isinstance(response, dict) else ""
    seed_payload = seed_response if isinstance(seed_response, dict) else {}
    expected_present = bool(recall_code and recall_code in response_text)
    prompt_included_expected = bool(recall_code and recall_code in prompt)
    return {
        "enabled": True,
        "status": "passed" if expected_present and not prompt_included_expected else "failed",
        "seed_memory_id": seed_payload.get("memory_id") or seed_payload.get("id"),
        "seed_status": seed_payload.get("status"),
        "expected_code_sha256": _sha256(recall_code or ""),
        "expected_present_in_response": expected_present,
        "prompt_included_expected_code": prompt_included_expected,
        "response_text_length": len(response_text),
    }


def _sha256(value: str) -> str:
    """Return a deterministic non-secret digest."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dashboard_evidence_urls(
    *,
    project: dict[str, Any] | None,
    trace_id: object,
    response_id: object,
    user_id: str,
) -> dict[str, str | None]:
    """Build dashboard URLs that let operators review smoke evidence quickly."""
    project_ref = None
    if project:
        project_ref = str(project.get("slug") or project.get("id") or "")
    params: dict[str, str] = {}
    if trace_id:
        params["trace_id"] = str(trace_id)
    if response_id:
        params["response_id"] = str(response_id)
    if user_id:
        params["user_id"] = user_id
    observability_url = dashboard_observability_url(project=project_ref, filters=params)
    return {
        "project_url": dashboard_project_url(project_ref) if project_ref else None,
        "observability_url": observability_url,
    }


def _smoke_evidence(
    *,
    ready: object,
    response: object,
    support_receipt: dict[str, object],
    dashboard_urls: dict[str, str | None],
    support_bundle: dict[str, Any] | None,
    memory_recall: dict[str, object],
) -> dict[str, object]:
    """Create the redacted smoke evidence artifact."""
    response_payload = response if isinstance(response, dict) else {}
    return {
        "schema_version": "general-augment-smoke-evidence/v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "ready": ready,
        "response": {
            "id": response_payload.get("id"),
            "status": response_payload.get("status"),
            "model": response_payload.get("model"),
        },
        "support_receipt": support_receipt,
        "dashboard_urls": dashboard_urls,
        "support_bundle": redact_metadata(support_bundle) if support_bundle is not None else None,
        "memory_recall": memory_recall,
        "security": {
            "raw_secrets_included": False,
            "raw_provider_credentials_included": False,
            "raw_response_payload_included": False,
        },
    }


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    """Write redacted smoke evidence to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
