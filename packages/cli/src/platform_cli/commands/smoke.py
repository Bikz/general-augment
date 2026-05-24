"""Smoke-test app-facing General Augment endpoints."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import panel, print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import DEFAULT_DASHBOARD_URL, dashboard_project_url

DEFAULT_SMOKE_MESSAGE = "Reply exactly with: genaug-smoke-ok"
DEFAULT_STRUCTURED_MESSAGE = 'Return JSON with ok=true and label="genaug-smoke-ok".'
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
    include_support_bundle: Annotated[
        bool,
        typer.Option(
            "--include-support-bundle",
            help="Fetch and embed the project support bundle; requires --project and admin auth.",
        ),
    ] = False,
) -> None:
    """Run health plus one `/v1/responses` smoke request."""
    runtime: Runtime = ctx.obj
    if include_support_bundle and not project:
        raise CLIError("--include-support-bundle requires --project.")
    turn_id = uuid.uuid4().hex[:12]
    schema = _load_schema(schema_file) if schema_file else None
    structured = structured or schema is not None
    if structured and message == DEFAULT_SMOKE_MESSAGE:
        message = DEFAULT_STRUCTURED_MESSAGE
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
    with runtime.client() as client:
        ready = client.public("GET", "/health/ready")
        if project:
            project_payload = resolve_project(client, project)
            headers["X-Project-ID"] = str(project_payload["id"])
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
                "evidence": smoke_evidence,
            }
        )
        return

    rows: list[list[object]] = [["Ready", _status_text(ready)]]
    if isinstance(response, dict):
        rows.extend(
            [
                ["Response ID", response.get("id", "")],
                ["Status", response.get("status", "")],
                ["Model", response.get("model", metadata_payload.get("general_augment_model", ""))],
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
        if dashboard_urls.get("observability_url"):
            rows.append(["Dashboard", dashboard_urls["observability_url"]])
        if evidence_output is not None:
            rows.append(["Evidence", evidence_output])
    table("Smoke", ["Check", "Value"], rows)
    if isinstance(response, dict):
        panel("Output", _response_output_text(response) or "<empty>")
        panel("Support receipt", json.dumps(support_receipt, indent=2, sort_keys=True))
    if evidence_output is not None:
        print_success(f"Wrote smoke evidence to {evidence_output}.")


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
        if value:
            return value
    return None


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
        "cost_usd": _metadata_value(metadata, "general_augment_cost_usd", "cost_usd"),
        "ready_status": _status_text(ready),
    }


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
    query = urlencode(params)
    observability_url = (
        f"{DEFAULT_DASHBOARD_URL}/dashboard/observability?{query}"
        if query
        else f"{DEFAULT_DASHBOARD_URL}/dashboard/observability"
    )
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
        "support_bundle": support_bundle,
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
