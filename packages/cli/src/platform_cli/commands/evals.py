"""Trace-backed agent eval commands."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer
from jsonschema import ValidationError as JsonSchemaValidationError  # type: ignore[import-untyped]
from jsonschema import validate as validate_json_schema

from platform_cli.output import print_json, print_success, table

app = typer.Typer(help="Run trace-backed agent eval suites.")
EVAL_SUITE_SCHEMA_VERSION = "genaug.agent_eval_suite.v1"
EVAL_RUN_SCHEMA_VERSION = "genaug.agent_eval_run.v1"
EvalMode = Literal["local", "hosted"]
SECRET_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{4,}\b"),
    re.compile(r"\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+", re.I),
)


@app.command("run")
def run_eval_suite_command(
    suite: Annotated[
        Path,
        typer.Argument(help="Path to a genaug.agent_eval_suite.v1 JSON fixture."),
    ],
    artifact_dir: Annotated[
        Path,
        typer.Option("--artifact-dir", help="Directory for secret-free eval artifacts."),
    ] = Path("artifacts/agent-evals"),
    mode: Annotated[
        Literal["local", "hosted"],
        typer.Option(help="Eval execution mode. Hosted requires platform credentials."),
    ] = "local",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
    gate: Annotated[
        bool,
        typer.Option("--gate", help="Evaluate the suite as a CI gate and fail on regressions."),
    ] = False,
) -> None:
    """Run a local eval suite and write a redacted artifact."""
    artifact = (
        run_eval_suite_gate(suite, artifact_dir=artifact_dir, mode=mode)
        if gate
        else run_eval_suite(suite, artifact_dir=artifact_dir, mode=mode)
    )
    if json_output:
        print_json(artifact)
        if gate and artifact.get("verdict") != "PASS":
            raise typer.Exit(1)
        return
    summary = artifact.get("summary", {}) if isinstance(artifact, dict) else {}
    rows = [
        ["Suite", artifact.get("suite", {}).get("name", suite.stem)],
        ["Mode", artifact.get("mode", mode)],
        ["Total", summary.get("total", 0)],
        ["Passed", summary.get("passed", 0)],
        ["Failed", summary.get("failed", 0)],
    ]
    if gate:
        rows.append(["Verdict", artifact.get("verdict", "FAIL")])
    rows.append(["Artifact", artifact.get("artifact_path", "")])
    table(
        "Agent evals",
        ["Field", "Value"],
        rows,
    )
    if gate and artifact.get("verdict") != "PASS":
        raise typer.Exit(1)
    print_success("Eval run completed.")


def run_eval_suite(
    suite_path: str | Path,
    *,
    artifact_dir: str | Path = "artifacts/agent-evals",
    mode: EvalMode = "local",
) -> dict[str, Any]:
    """Run a deterministic local eval suite and write a secret-free artifact."""
    if mode == "hosted":
        raise RuntimeError("Hosted eval mode is not available in the public CLI package yet.")
    path = Path(suite_path)
    suite = _load_suite(path)
    cases = [_score_case(case) for case in suite["cases"]]
    passed = sum(1 for case in cases if case["passed"])
    artifact = {
        "schema_version": EVAL_RUN_SCHEMA_VERSION,
        "suite": {
            "name": suite.get("name", path.stem),
            "path": str(path),
            "schema_version": suite.get("schema_version"),
            "version": suite.get("version"),
            "dataset": suite.get("dataset", {}),
            "scorers": suite.get("scorers", []),
        },
        "mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "total": len(cases),
            "passed": passed,
            "failed": len(cases) - passed,
        },
        "cases": cases,
    }
    artifact = _redact_secrets(artifact)
    out_dir = Path(artifact_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = out_dir / f"{path.stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    artifact["artifact_path"] = str(artifact_path)
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return cast(dict[str, Any], artifact)


def run_eval_suite_gate(
    suite_path: str | Path,
    *,
    artifact_dir: str | Path = "artifacts/agent-evals",
    mode: EvalMode = "local",
) -> dict[str, Any]:
    """Run an eval suite and attach a CI-friendly PASS/FAIL verdict."""
    path = Path(suite_path)
    suite = _load_suite(path)
    artifact = run_eval_suite(path, artifact_dir=artifact_dir, mode=mode)
    summary = artifact.get("summary", {})
    total = int(summary.get("total") or 0) if isinstance(summary, dict) else 0
    passed = int(summary.get("passed") or 0) if isinstance(summary, dict) else 0
    pass_rate = (passed / total) if total else 0.0
    gate_config = suite.get("gate") if isinstance(suite.get("gate"), dict) else {}
    min_pass_rate = float(cast(dict[str, Any], gate_config).get("min_pass_rate", 1.0))
    required_case_ids = [
        str(case_id) for case_id in cast(dict[str, Any], gate_config).get("required_case_ids", [])
    ]
    required_tags = [str(tag) for tag in cast(dict[str, Any], gate_config).get("required_tags", [])]
    cases = [case for case in artifact.get("cases", []) if isinstance(case, dict)]
    failed_required_case_ids = [
        str(case.get("id"))
        for case in cases
        if str(case.get("id")) in required_case_ids and not bool(case.get("passed"))
    ]
    missing_required_tags = [
        tag
        for tag in required_tags
        if not any(
            bool(case.get("passed")) and tag in {str(case_tag) for case_tag in case.get("tags", [])}
            for case in cases
        )
    ]
    artifact["gate"] = {
        "min_pass_rate": min_pass_rate,
        "pass_rate": pass_rate,
        "required_case_ids": required_case_ids,
        "failed_required_case_ids": failed_required_case_ids,
        "required_tags": required_tags,
        "missing_required_tags": missing_required_tags,
    }
    artifact["verdict"] = (
        "PASS"
        if pass_rate >= min_pass_rate and not failed_required_case_ids and not missing_required_tags
        else "FAIL"
    )
    artifact = _redact_secrets(artifact)
    artifact_path = Path(str(artifact["artifact_path"]))
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return cast(dict[str, Any], artifact)


def _load_suite(path: Path) -> dict[str, Any]:
    """Load and validate a checked-in eval suite."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Eval suite must be a JSON object.")
    if payload.get("schema_version") != EVAL_SUITE_SCHEMA_VERSION:
        raise ValueError(f"Eval suite schema_version must be {EVAL_SUITE_SCHEMA_VERSION}.")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Eval suite must include at least one case.")
    return cast(dict[str, Any], payload)


def _score_case(case: Any) -> dict[str, Any]:
    """Score one deterministic eval case."""
    if not isinstance(case, dict):
        raise ValueError("Eval case must be a JSON object.")
    scores: dict[str, bool] = {}
    response = str(case.get("response") or "")
    if "expected_json_schema" in case:
        scores["json_schema_valid"] = _json_schema_valid(response, case["expected_json_schema"])
    if "final_answer_contains" in case:
        expected = str(case["final_answer_contains"])
        scores["final_answer_assertion"] = expected.casefold() in response.casefold()
    if "expected_tool_choice" in case:
        scores["tool_choice"] = case.get("tool_choice") == case.get("expected_tool_choice")
    if "forbidden_tools" in case:
        called = {str(item) for item in case.get("tool_calls", []) if isinstance(item, str)}
        forbidden = {str(item) for item in case.get("forbidden_tools", []) if isinstance(item, str)}
        scores["forbidden_tool_avoidance"] = not bool(called.intersection(forbidden))
    if "expected_approval_required" in case:
        scores["approval_behavior"] = bool(case.get("approval_required")) is bool(
            case["expected_approval_required"]
        )
    if "expected_guardrail_reasons" in case:
        scores["guardrail_expected"] = _guardrail_reasons_match(
            str(case.get("input") or ""),
            [str(reason) for reason in case.get("expected_guardrail_reasons", [])],
        )
    if "max_latency_ms" in case:
        scores["latency_ceiling"] = int(case.get("latency_ms") or 0) <= int(case["max_latency_ms"])
    return {
        "id": str(case.get("id") or "unnamed"),
        "tags": [str(tag) for tag in case.get("tags", []) if isinstance(tag, str)],
        "golden": bool(case.get("golden", False)),
        "passed": all(scores.values()) if scores else True,
        "scores": scores,
        "evidence": {
            "response_id": case.get("response_id"),
            "trace_id": case.get("trace_id"),
            "support_bundle": case.get("support_bundle"),
            "guardrail_reasons": case.get("expected_guardrail_reasons", []),
        },
    }


def _json_schema_valid(response: str, schema: Any) -> bool:
    """Return whether a response string parses as JSON and validates."""
    if not isinstance(schema, dict):
        return False
    try:
        parsed = json.loads(response)
        validate_json_schema(parsed, schema)
    except (json.JSONDecodeError, JsonSchemaValidationError):
        return False
    return True


def _guardrail_reasons_match(input_text: str, expected: list[str]) -> bool:
    """Return whether deterministic public checks include the expected reasons."""
    actual = set[str]()
    lower = input_text.casefold()
    if "ignore previous instructions" in lower or "developer prompt" in lower:
        actual.add("prompt_injection")
    return set(expected).issubset(actual)


def _redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking strings before writing artifacts."""
    if isinstance(value, dict):
        return {key: _redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SECRET_TEXT_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted
    return value
