"""Run a local test-only General Augment HTTP mock server.

The mock is for app contract tests and fixtures. It does not run Hermes, call models,
enforce billing, validate credentials, or persist data outside this process.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse
from uuid import NAMESPACE_URL, uuid5

import yaml

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MOCK_CREATED_AT = "2026-01-01T00:00:00Z"
EXACT_REPLY_RE = re.compile(r"reply exactly with:\s*(.+)", re.IGNORECASE)
HEALTH_PATHS = {"/v1/health", "/health/ready", "/health/live"}


@dataclass(slots=True)
class MemoryFact:
    """One in-memory fact stored by the local mock."""

    memory_id: str
    user_id: str
    fact: str
    fact_type: str = "fact"
    importance_score: float | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = MOCK_CREATED_AT
    expires_at: str | None = None
    status: str = "active"
    supersedes_memory_id: str | None = None

    def as_result(self, score: float | None) -> dict[str, Any]:
        """Return a public memory-hit shape."""
        return {
            "id": self.memory_id,
            "memory_id": self.memory_id,
            "fact": self.fact,
            "fact_type": self.fact_type,
            "content": self.fact,
            "importance_score": self.importance_score,
            "similarity": score,
            "score": score,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "source": self.source,
            "status": self.status,
            "metadata": self.metadata,
            "supersedes_memory_id": self.supersedes_memory_id,
        }


class LocalGAMockStore:
    """Deterministic in-memory state for local app contract tests."""

    def __init__(self) -> None:
        """Initialize empty response replay and memory state."""
        self.response_replays: dict[str, tuple[str, dict[str, Any]]] = {}
        self.memory_replays: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self.memories: dict[str, list[MemoryFact]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.logs_by_project: dict[str, list[dict[str, Any]]] = {}
        self.project_traces: dict[str, list[dict[str, Any]]] = {}
        self.project_usage: dict[str, dict[str, Any]] = {}
        self.api_keys: dict[str, dict[str, Any]] = {}

    def list_projects(self, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """Return mock projects visible to any local management key."""

        scoped_project_id = self._scoped_project_id(headers)
        if scoped_project_id:
            project = self.projects.get(scoped_project_id)
            return HTTPStatus.OK, {"items": [project] if project else []}
        return HTTPStatus.OK, {"items": list(self.projects.values())}

    def me(self, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """Return a stable local admin identity for CLI auth preflight."""

        scoped_project_id = self._scoped_project_id(headers)
        if scoped_project_id:
            return HTTPStatus.OK, {
                "auth_method": "api_key",
                "project_id": scoped_project_id,
                "project_ids": [scoped_project_id],
            }
        return HTTPStatus.OK, {
            "auth_method": "api_key",
            "project_ids": sorted(self.projects),
        }

    def deploy_project(
        self, payload: dict[str, Any], project_id: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Create or update a mock project from a deploy payload."""

        yaml_content = str(payload.get("yaml_content") or "")
        manifest = yaml.safe_load(yaml_content) if yaml_content else {}
        manifest = manifest if isinstance(manifest, dict) else {}
        raw_metadata = manifest.get("metadata")
        metadata = cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
        slug = str(metadata.get("name") or project_id or "local-mock-project")
        name = str(metadata.get("display_name") or _display_name(slug))
        resolved_id = project_id or f"proj_mock_{_digest_json({'slug': slug})[:12]}"
        project = {
            "id": resolved_id,
            "name": name,
            "slug": slug,
            "status": "active",
            "channels": _manifest_channels(manifest),
            "enabled_tool_ids": _manifest_tool_ids(manifest),
            "soul_content": str(payload.get("soul_content") or ""),
            "skill_contents": [
                str(content) for content in payload.get("skills", []) if isinstance(content, str)
            ],
        }
        self.projects[resolved_id] = project
        self.project_usage.setdefault(resolved_id, {"agent_turns_count": 0, "total_cost_usd": 0.0})
        return HTTPStatus.OK, project

    def register_openapi_tools(
        self, project_id: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Pretend to register curated OpenAPI tools for a project."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        enabled = list(project.get("enabled_tool_ids") or [])
        generated_count = max(len(enabled), 1)
        return HTTPStatus.OK, {
            "generated_count": generated_count,
            "curated_count": generated_count,
            "enabled_tool_ids": enabled,
            "auto_deployed": bool(payload.get("auto_deploy", True)),
            "mcp_server": {"name": f"{project['slug']}-api"},
            "tools": [{"id": tool_id, "risk_level": "low"} for tool_id in enabled],
        }

    def list_tools(self) -> tuple[int, dict[str, Any]]:
        """Return a stable built-in plus generated tool registry shape."""

        tool_ids = {"web_search", "calendar_read"}
        for project in self.projects.values():
            tool_ids.update(str(tool_id) for tool_id in project.get("enabled_tool_ids") or [])
        return HTTPStatus.OK, {
            "items": [
                {"id": tool_id, "risk_level": "low", "requires_approval": False}
                for tool_id in sorted(tool_ids)
            ]
        }

    def test_project(self, project_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Run a deterministic mock project test and retain logs/trace rows."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        message = str(payload.get("message") or "")
        output = _mock_output_text(message, "mock-admin-user")
        trace_id = f"trace_mock_{_digest_json({'project_id': project_id, 'message': message})[:16]}"
        self._append_project_log(project_id, "user", message, trace_id)
        self._append_project_log(project_id, "assistant", output, trace_id)
        response_id = f"resp_mock_{trace_id.removeprefix('trace_mock_')}"
        self.project_traces.setdefault(project_id, []).append(
            {"trace_id": trace_id, "response_id": response_id}
        )
        self.project_usage.setdefault(project_id, {"agent_turns_count": 0, "total_cost_usd": 0.0})
        self.project_usage[project_id]["agent_turns_count"] += 1
        return HTTPStatus.OK, {
            "response": output,
            "response_text": output,
            "warnings": [],
            "metadata": {"trace_id": trace_id},
            "error": None,
            "model_used": "mock/balanced",
            "cost_usd": 0.0,
        }

    def project_logs(self, project_id: str, limit: int) -> tuple[int, dict[str, Any]]:
        """Return retained mock project logs."""

        return HTTPStatus.OK, {"items": self.logs_by_project.get(project_id, [])[-limit:]}

    def project_usage_detail(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return retained mock usage counters."""

        totals = self.project_usage.get(project_id, {"agent_turns_count": 0, "total_cost_usd": 0.0})
        return HTTPStatus.OK, {
            "project_id": project_id,
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "totals": totals,
            "days": [{"date": "2026-01-01", **totals}],
            "limits": {},
        }

    def project_observability(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return retained mock observability rows."""

        traces = self.project_traces.get(project_id, [])
        return HTTPStatus.OK, {
            "langfuse_enabled": False,
            "langfuse_project_id": None,
            "langfuse_url": None,
            "traces": traces,
            "metrics": {"trace_count": len(traces)},
            "messages_over_time": [],
            "model_distribution": [{"model": "mock/balanced", "count": len(traces)}],
            "tool_usage": [],
        }

    def project_channel_status(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return hosted-compatible channel readiness for one mock project."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        raw_channels = project.get("channels")
        configured: dict[str, Any] = raw_channels if isinstance(raw_channels, dict) else {}
        channels = [
            _channel_health("in_app", "In-app", configured=True),
            _channel_health("whatsapp", "WhatsApp", configured=bool(configured.get("whatsapp"))),
            _channel_health("telegram", "Telegram", configured=bool(configured.get("telegram"))),
            _channel_health("sms", "SMS", configured=bool(configured.get("sms"))),
        ]
        return HTTPStatus.OK, {"project_id": project_id, "channels": channels}

    def project_runtime_policy(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return a hosted-compatible, secret-free runtime policy summary."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        enabled_tool_ids = [
            str(tool_id) for tool_id in project.get("enabled_tool_ids") or []
        ]
        return HTTPStatus.OK, {
            "project_id": project_id,
            "model_routing": {
                "mode": "tiered_complexity",
                "tiers": {
                    "simple": "mock/simple",
                    "balanced": "mock/balanced",
                    "complex": "mock/complex",
                },
                "default_tier": "balanced",
                "auto_routes_by": [
                    "prompt complexity",
                    "enabled tools",
                    "conversation history",
                    "reasoning_effort override when supplied",
                ],
                "channel_parity": True,
            },
            "tool_discovery": {
                "mode": "auto",
                "direct_schema_tool_limit": 8,
                "max_search_results": 5,
            },
            "hermes_exposure": {
                "uses_dynamic_discovery_by_default": False,
                "turn_path": "shared_hermes",
            },
            "platform_tools": {
                "enabled_tool_ids": enabled_tool_ids,
                "unknown_tool_ids": [],
            },
            "mcp": {"enabled_tool_ids": []},
            "skills": {
                "names": [
                    _skill_name_from_content(content)
                    for content in project.get("skill_contents", []) or []
                ]
            },
        }

    def project_soul(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return SOUL.md content for one mock project."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        return HTTPStatus.OK, {
            "project_id": project_id,
            "content": str(project.get("soul_content") or ""),
        }

    def project_skills(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return SKILL.md summaries for one mock project."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        return HTTPStatus.OK, {
            "items": [
                _skill_summary(content)
                for content in project.get("skill_contents", []) or []
                if isinstance(content, str)
            ]
        }

    def get_project_skill(self, project_id: str, skill_name: str) -> tuple[int, dict[str, Any]]:
        """Return one mock SKILL.md file."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        for content in project.get("skill_contents", []) or []:
            if isinstance(content, str) and _skill_name_from_content(content) == skill_name:
                return HTTPStatus.OK, {
                    "name": skill_name,
                    "content": content,
                    "metadata": _skill_metadata(content),
                }
        return HTTPStatus.NOT_FOUND, _error("not_found", "Skill not found.")

    def add_project_skill(
        self, project_id: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Create or replace one mock SKILL.md file."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        content = str(payload.get("content") or "")
        if not content.strip():
            return HTTPStatus.BAD_REQUEST, _error("invalid_request", "Skill content is required.")
        skill_name = _skill_name_from_content(content)
        existing = [
            item
            for item in project.get("skill_contents", []) or []
            if isinstance(item, str) and _skill_name_from_content(item) != skill_name
        ]
        project["skill_contents"] = [*existing, content]
        return HTTPStatus.OK, {
            "name": skill_name,
            "content": content,
            "metadata": _skill_metadata(content),
        }

    def delete_project_skill(self, project_id: str, skill_name: str) -> tuple[int, dict[str, Any]]:
        """Delete one mock SKILL.md file."""

        project = self.projects.get(project_id)
        if project is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        existing = [
            item
            for item in project.get("skill_contents", []) or []
            if isinstance(item, str) and _skill_name_from_content(item) != skill_name
        ]
        if len(existing) == len(project.get("skill_contents", []) or []):
            return HTTPStatus.NOT_FOUND, _error("not_found", "Skill not found.")
        project["skill_contents"] = existing
        return HTTPStatus.OK, {"status": "deleted", "name": skill_name}

    def project_tool_call_audit(self, project_id: str) -> tuple[int, dict[str, Any]]:
        """Return a stable empty audit list for local verification."""

        if project_id not in self.projects:
            return HTTPStatus.NOT_FOUND, _error("not_found", "Project not found.")
        return HTTPStatus.OK, {"items": [], "next_cursor": None}

    def create_api_key(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Create a deterministic local API key row."""

        key_id = f"key_mock_{_digest_json(payload)[:12]}"
        runtime_mode = payload.get("runtime_mode")
        key_prefix = f"gab{runtime_mode}" if runtime_mode in {"test", "live"} else "gaadmlocal"
        raw_key = f"{key_prefix}_{_digest_json({'key_id': key_id})[:24]}"
        row = {
            "id": key_id,
            "name": str(payload.get("name") or "Local mock key"),
            "api_key": raw_key,
            "masked_key": f"{raw_key[:12]}...{raw_key[-4:]}",
            "scopes": payload.get("scopes") or ["admin"],
            "runtime_mode": runtime_mode,
            "project_id": payload.get("project_id"),
            "expires_at": payload.get("expires_at"),
            "created_by": "local_mock",
            "created_at": MOCK_CREATED_AT,
            "last_used_at": None,
        }
        self.api_keys[key_id] = row
        return HTTPStatus.OK, row

    def list_api_keys(self, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """List mock API keys without raw secrets."""

        scoped_project_id = self._scoped_project_id(headers)
        items = []
        for row in self.api_keys.values():
            if scoped_project_id and str(row.get("project_id") or "") != scoped_project_id:
                continue
            item = dict(row)
            item.pop("api_key", None)
            items.append(item)
        return HTTPStatus.OK, {"items": items}

    def _scoped_project_id(self, headers: Mapping[str, str] | None) -> str | None:
        """Return the project id for a raw mock project key."""

        credential = _auth_credential(headers or {})
        if not credential:
            return None
        for row in self.api_keys.values():
            if row.get("api_key") == credential and row.get("project_id"):
                return str(row["project_id"])
        return None

    def update_api_key(self, key_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Update mock API key metadata."""

        row = self.api_keys.get(key_id)
        if row is None:
            return HTTPStatus.NOT_FOUND, _error("not_found", "API key not found.")
        for key in ("name", "scopes", "expires_at"):
            if key in payload:
                row[key] = payload[key]
        item = dict(row)
        item.pop("api_key", None)
        return HTTPStatus.OK, item

    def revoke_api_key(self, key_id: str) -> tuple[int, dict[str, Any]]:
        """Revoke a mock API key."""

        self.api_keys.pop(key_id, None)
        return HTTPStatus.OK, {"status": "revoked", "id": key_id}

    def response(
        self,
        payload: dict[str, Any],
        headers: Mapping[str, str],
    ) -> tuple[int, dict[str, Any]]:
        """Return a deterministic Responses-shaped object."""
        idempotency_key = _header(headers, "X-Idempotency-Key")
        request_digest = _digest_json(payload)
        if idempotency_key and idempotency_key in self.response_replays:
            stored_digest, stored_response = self.response_replays[idempotency_key]
            if stored_digest != request_digest:
                return HTTPStatus.CONFLICT, _error(
                    "idempotency_key_conflict",
                    "X-Idempotency-Key was reused with a different mock request body.",
                )
            return HTTPStatus.OK, copy.deepcopy(stored_response)

        response = self._build_response(payload, headers, idempotency_key)
        project_id = _header(headers, "X-Project-ID")
        if project_id:
            self._record_response_turn(project_id, payload, response)
        if idempotency_key:
            self.response_replays[idempotency_key] = (request_digest, copy.deepcopy(response))
        return HTTPStatus.OK, response

    def _record_response_turn(
        self, project_id: str, payload: dict[str, Any], response: dict[str, Any]
    ) -> None:
        """Record an app-facing response in project logs and observability."""

        input_text = _extract_input_text(payload.get("input"))
        output_text = response["output"][0]["content"][0]["text"]
        trace_id = str(response["metadata"]["general_augment_trace_id"])
        self._append_project_log(project_id, "user", input_text, trace_id)
        self._append_project_log(project_id, "assistant", output_text, trace_id)
        self.project_traces.setdefault(project_id, []).append(
            {"trace_id": trace_id, "response_id": response["id"]}
        )
        self.project_usage.setdefault(project_id, {"agent_turns_count": 0, "total_cost_usd": 0.0})
        self.project_usage[project_id]["agent_turns_count"] += 1

    def _append_project_log(
        self, project_id: str, role: str, content: str, trace_id: str
    ) -> None:
        """Append one mock project log row."""

        digest = _digest_json({"project_id": project_id, "role": role, "content": content})
        log_id = f"msg_mock_{digest[:12]}"
        self.logs_by_project.setdefault(project_id, []).append(
            {
                "id": log_id,
                "created_at": MOCK_CREATED_AT,
                "session_id": _mock_user_uuid(f"{project_id}:session"),
                "user_id": _mock_user_uuid(f"{project_id}:user"),
                "role": role,
                "content": content,
                "observability_trace_id": trace_id,
                "model_used": "mock/balanced" if role == "assistant" else None,
                "cost_usd": 0.0 if role == "assistant" else None,
            }
        )

    def store_memory(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Store one explicit memory fact in process memory."""
        user_id = _external_user_id(payload)
        fact = str(payload.get("fact") or payload.get("content") or "").strip()
        if not fact:
            return HTTPStatus.BAD_REQUEST, _error("invalid_memory", "fact is required.")

        idempotency_key = _optional_string(payload.get("idempotency_key"))
        request_digest = _digest_json(payload)
        replay_key = (user_id, idempotency_key or "")
        if idempotency_key and replay_key in self.memory_replays:
            stored_digest, stored_response = self.memory_replays[replay_key]
            if stored_digest != request_digest:
                return HTTPStatus.CONFLICT, _error(
                    "memory_idempotency_key_conflict",
                    "idempotency_key was reused with a different memory write request.",
                )
            return HTTPStatus.OK, copy.deepcopy(stored_response)

        memory_id = f"mem_mock_{_digest_json({'user_id': user_id, 'fact': fact})[:16]}"
        metadata = _object_payload(payload.get("metadata"))
        fact_row = MemoryFact(
            memory_id=memory_id,
            user_id=user_id,
            fact=fact,
            fact_type=str(payload.get("fact_type") or "fact"),
            importance_score=_optional_float(payload.get("importance_score")),
            source=_optional_string(payload.get("source")),
            metadata=dict(metadata),
        )
        self.memories.setdefault(user_id, [])
        if not any(existing.memory_id == memory_id for existing in self.memories[user_id]):
            self.memories[user_id].append(fact_row)

        user_profile = _object_payload(payload.get("user_profile"))
        if user_profile:
            self.profiles.setdefault(user_id, {}).update(user_profile)

        response = {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "memory_id": memory_id,
            "content": fact,
            "fact": fact,
            "fact_type": fact_row.fact_type,
            "importance_score": fact_row.importance_score,
            "source": fact_row.source,
            "metadata": fact_row.metadata,
            "status": "stored",
        }
        if idempotency_key:
            self.memory_replays[replay_key] = (request_digest, copy.deepcopy(response))
        return HTTPStatus.OK, response

    def search_memory(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Search stored mock memory with deterministic lexical scores."""
        user_id = _external_user_id(payload)
        query = str(payload.get("query") or "")
        limit = _positive_int(payload.get("limit"), default=10)
        min_similarity = _optional_float(payload.get("min_similarity"))
        min_importance = _optional_float(payload.get("min_importance"))
        fact_type = _optional_string(payload.get("fact_type"))
        source = _optional_string(payload.get("source"))

        ranked: list[tuple[float, MemoryFact]] = []
        for fact in self.memories.get(user_id, []):
            if fact_type and fact.fact_type != fact_type:
                continue
            if source and fact.source != source:
                continue
            if min_importance is not None and (fact.importance_score or 0.0) < min_importance:
                continue
            score = _lexical_score(query, fact.fact)
            if min_similarity is not None and score < min_similarity:
                continue
            ranked.append((score, fact))

        ranked.sort(key=lambda item: (-item[0], item[1].memory_id))
        facts = [fact.as_result(round(score, 4)) for score, fact in ranked[:limit]]
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "facts": facts,
        }

    def memory_profile(self, user_id: str) -> tuple[int, dict[str, Any]]:
        """Return the mock profile and recent facts for one app user."""
        facts = self.memories.get(user_id, [])
        profile = {
            "external_user_id": user_id,
            "external_provider": "local_mock",
            **self.profiles.get(user_id, {}),
        }
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "profile": profile,
            "recent_facts": [fact.as_result(None) for fact in facts],
            "total_facts": len(facts),
        }

    def memory_lineage(self, memory_id: str, user_id: str) -> tuple[int, dict[str, Any]]:
        """Return a bounded mock lineage for one memory fact."""
        facts = [
            fact
            for fact in self.memories.get(user_id, [])
            if fact.memory_id == memory_id or fact.supersedes_memory_id == memory_id
        ]
        if not facts:
            return HTTPStatus.NOT_FOUND, _error("memory_not_found", "No scoped memory fact found.")
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "memory_id": memory_id,
            "facts": [fact.as_result(None) for fact in facts],
            "related_count": len(facts),
        }

    def correct_memory(self, memory_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Correct one active mock memory fact by appending a replacement."""
        user_id = _external_user_id(payload)
        fact = str(payload.get("fact") or payload.get("content") or "").strip()
        if not fact:
            return HTTPStatus.BAD_REQUEST, _error("invalid_memory", "fact is required.")
        facts = self.memories.get(user_id, [])
        existing = next(
            (item for item in facts if item.memory_id == memory_id and item.status == "active"),
            None,
        )
        if existing is None:
            return HTTPStatus.NOT_FOUND, _error("memory_not_found", "No active memory fact found.")
        existing.status = "superseded"
        corrected_id = f"mem_mock_{_digest_json({'user_id': user_id, 'correction': fact})[:16]}"
        metadata = _object_payload(payload.get("metadata"))
        corrected = MemoryFact(
            memory_id=corrected_id,
            user_id=user_id,
            fact=fact,
            fact_type=str(payload.get("fact_type") or existing.fact_type),
            importance_score=_optional_float(payload.get("importance_score"))
            if payload.get("importance_score") is not None
            else existing.importance_score,
            source=_optional_string(payload.get("source")) or existing.source,
            metadata=dict(metadata or existing.metadata),
            supersedes_memory_id=memory_id,
        )
        facts.append(corrected)
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "memory_id": memory_id,
            "corrected_memory_id": corrected_id,
            "content": fact,
            "source": corrected.source,
            "metadata": corrected.metadata,
            "status": "corrected",
        }

    def delete_memory(self, memory_id: str, user_id: str) -> tuple[int, dict[str, Any]]:
        """Delete one memory for the scoped app user."""
        facts = self.memories.get(user_id, [])
        kept = [fact for fact in facts if fact.memory_id != memory_id]
        deleted_count = len(facts) - len(kept)
        self.memories[user_id] = kept
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "memory_id": memory_id,
            "deleted_ids": [memory_id] if deleted_count else [],
            "deleted_count": deleted_count,
            "status": "deleted" if deleted_count else "not_found",
        }

    def purge_user_memory(self, user_id: str) -> tuple[int, dict[str, Any]]:
        """Delete all memory rows for one app user."""
        deleted_ids = [fact.memory_id for fact in self.memories.get(user_id, [])]
        self.memories[user_id] = []
        return HTTPStatus.OK, {
            "user_id": user_id,
            "general_augment_user_id": _mock_user_uuid(user_id),
            "memory_id": None,
            "deleted_ids": deleted_ids,
            "deleted_count": len(deleted_ids),
            "status": "purged",
        }

    def _build_response(
        self,
        payload: dict[str, Any],
        headers: Mapping[str, str],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Build one mock Responses object."""
        input_text = _extract_input_text(payload.get("input"))
        output_text = _mock_response_text(
            payload,
            input_text,
            str(payload.get("user") or "mock-user"),
        )
        digest = _digest_json(
            {
                "input": input_text,
                "user": payload.get("user"),
                "model": payload.get("model"),
                "idempotency_key": idempotency_key,
            }
        )
        response_id = f"resp_mock_{digest[:20]}"
        request_id = _header(headers, "X-Request-ID") or f"req_mock_{digest[:16]}"
        trace_id = f"trace_mock_{digest[:16]}"
        model = str(payload.get("model") or "balanced")
        input_tokens = max(1, len(input_text.split()))
        output_tokens = max(1, len(output_text.split()))
        metadata = _object_payload(payload.get("metadata"))
        response_metadata = dict(metadata)
        response_metadata.update(
            {
                "general_augment_response_id": response_id,
                "general_augment_request_id": request_id,
                "general_augment_trace_id": trace_id,
                "general_augment_model": f"mock/{model}",
                "general_augment_input_tokens": input_tokens,
                "general_augment_output_tokens": output_tokens,
                "general_augment_cost_usd": 0.0,
                "general_augment_latency_ms": 0,
            }
        )
        response_metadata.setdefault("trace_id", trace_id)
        response_metadata.setdefault("request_id", request_id)
        _copy_trace_header(headers, response_metadata, "traceparent")
        _copy_trace_header(headers, response_metadata, "tracestate")

        return {
            "id": response_id,
            "object": "response",
            "created_at": MOCK_CREATED_AT,
            "status": "completed",
            "model": f"mock/{model}",
            "output": [
                {
                    "id": f"msg_mock_{digest[:20]}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": input_tokens,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": output_tokens,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": input_tokens + output_tokens,
            },
            "previous_response_id": payload.get("previous_response_id"),
            "metadata": response_metadata,
            "error": None,
        }


def sse_events_for_response(response: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return semantic SSE events for a completed mock response."""
    output_item = response["output"][0]
    output_text = output_item["content"][0]["text"]
    response_stub = {
        "id": response["id"],
        "object": "response",
        "status": "in_progress",
        "model": response["model"],
        "output": [],
        "metadata": response["metadata"],
    }
    return [
        (
            "response.created",
            {"type": "response.created", "sequence_number": 0, "response": response_stub},
        ),
        (
            "response.in_progress",
            {"type": "response.in_progress", "sequence_number": 1, "response": response_stub},
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "sequence_number": 2,
                "output_index": 0,
                "item": output_item,
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": 3,
                "item_id": output_item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": output_text,
            },
        ),
        (
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "sequence_number": 4,
                "item_id": output_item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": output_text,
            },
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "sequence_number": 5,
                "response": response,
            },
        ),
    ]


class LocalGAMockHandler(BaseHTTPRequestHandler):
    """HTTP handler for the local test mock."""

    store = LocalGAMockStore()
    quiet = False
    server_version = "GeneralAugmentLocalMock/1.0"

    def do_GET(self) -> None:
        """Handle health and memory profile requests."""
        parsed = urlparse(self.path)
        if parsed.path in HEALTH_PATHS:
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "db": "connected (local mock)",
                    "redis": "connected (local mock)",
                },
            )
            return
        if parsed.path == "/api/v1/admin/projects":
            status, payload = self.store.list_projects(_headers_dict(self.headers))
            self._send_json(status, payload)
            return
        if parsed.path == "/api/v1/admin/me":
            status, payload = self.store.me(_headers_dict(self.headers))
            self._send_json(status, payload)
            return
        if parsed.path == "/api/v1/admin/tools":
            status, payload = self.store.list_tools()
            self._send_json(status, payload)
            return
        if parsed.path == "/api/v1/admin/keys":
            status, payload = self.store.list_api_keys(_headers_dict(self.headers))
            self._send_json(status, payload)
            return
        admin_match = _admin_project_route(parsed.path)
        if admin_match:
            project_id, suffix = admin_match
            query = parse_qs(parsed.query)
            if suffix == "logs":
                limit = _positive_int(query.get("limit", ["50"])[0], default=50)
                status, payload = self.store.project_logs(project_id, limit)
                self._send_json(status, payload)
                return
            if suffix == "usage":
                status, payload = self.store.project_usage_detail(project_id)
                self._send_json(status, payload)
                return
            if suffix == "observability":
                status, payload = self.store.project_observability(project_id)
                self._send_json(status, payload)
                return
            if suffix == "channels/status":
                status, payload = self.store.project_channel_status(project_id)
                self._send_json(status, payload)
                return
            if suffix == "runtime-policy":
                status, payload = self.store.project_runtime_policy(project_id)
                self._send_json(status, payload)
                return
            if suffix == "soul":
                status, payload = self.store.project_soul(project_id)
                self._send_json(status, payload)
                return
            if suffix == "skills":
                status, payload = self.store.project_skills(project_id)
                self._send_json(status, payload)
                return
            if suffix.startswith("skills/"):
                skill_name = unquote(suffix.removeprefix("skills/"))
                status, payload = self.store.get_project_skill(project_id, skill_name)
                self._send_json(status, payload)
                return
            if suffix == "audit/tool-calls":
                status, payload = self.store.project_tool_call_audit(project_id)
                self._send_json(status, payload)
                return
        if parsed.path.startswith("/api/v1/agent/memory/profile/"):
            user_id = _path_suffix(parsed.path, "/api/v1/agent/memory/profile/")
            status, payload = self.store.memory_profile(user_id)
            self._send_json(status, payload)
            return
        if parsed.path.startswith("/api/v1/agent/memory/lineage/"):
            memory_id = _path_suffix(parsed.path, "/api/v1/agent/memory/lineage/")
            query = parse_qs(parsed.query)
            user_id = query.get("user_id", [""])[0]
            status, payload = self.store.memory_lineage(memory_id, user_id)
            self._send_json(status, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", "No mock route matched."))

    def do_POST(self) -> None:
        """Handle Responses and memory write/search requests."""
        parsed = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            return
        if parsed.path == "/v1/responses":
            status, response = self.store.response(payload, _headers_dict(self.headers))
            if status == HTTPStatus.OK and payload.get("stream") is True:
                self._send_sse(response)
                return
            self._send_json(status, response)
            return
        if parsed.path == "/api/v1/admin/projects/from-config":
            status, response = self.store.deploy_project(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/v1/admin/keys":
            status, response = self.store.create_api_key(payload)
            self._send_json(status, response)
            return
        admin_match = _admin_project_route(parsed.path)
        if admin_match:
            project_id, suffix = admin_match
            if suffix == "tools/from-openapi":
                status, response = self.store.register_openapi_tools(project_id, payload)
                self._send_json(status, response)
                return
            if suffix == "test":
                status, response = self.store.test_project(project_id, payload)
                self._send_json(status, response)
                return
            if suffix == "skills":
                status, response = self.store.add_project_skill(project_id, payload)
                self._send_json(status, response)
                return
        if parsed.path.startswith("/api/v1/agent/memory/") and parsed.path.endswith("/correct"):
            memory_id = _path_suffix(parsed.path, "/api/v1/agent/memory/").removesuffix(
                "/correct"
            )
            status, response = self.store.correct_memory(memory_id, payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/v1/agent/memory/store":
            status, response = self.store.store_memory(payload)
            self._send_json(status, response)
            return
        if parsed.path == "/api/v1/agent/memory/search":
            status, response = self.store.search_memory(payload)
            self._send_json(status, response)
            return
        self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", "No mock route matched."))

    def do_DELETE(self) -> None:
        """Handle memory delete and purge requests."""
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/v1/agent/memory/user/"):
            user_id = _path_suffix(parsed.path, "/api/v1/agent/memory/user/")
            status, payload = self.store.purge_user_memory(user_id)
            self._send_json(status, payload)
            return
        if parsed.path.startswith("/api/v1/agent/memory/"):
            memory_id = _path_suffix(parsed.path, "/api/v1/agent/memory/")
            query = parse_qs(parsed.query)
            user_id = query.get("user_id", [""])[0]
            status, payload = self.store.delete_memory(memory_id, user_id)
            self._send_json(status, payload)
            return
        if parsed.path.startswith("/api/v1/admin/keys/"):
            key_id = _path_suffix(parsed.path, "/api/v1/admin/keys/")
            status, payload = self.store.revoke_api_key(key_id)
            self._send_json(status, payload)
            return
        admin_match = _admin_project_route(parsed.path)
        if admin_match:
            project_id, suffix = admin_match
            if suffix.startswith("skills/"):
                skill_name = unquote(suffix.removeprefix("skills/"))
                status, payload = self.store.delete_project_skill(project_id, skill_name)
                self._send_json(status, payload)
                return
        self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", "No mock route matched."))

    def do_PUT(self) -> None:
        """Handle project config updates."""

        parsed = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            return
        admin_match = _admin_project_route(parsed.path)
        if admin_match:
            project_id, suffix = admin_match
            if suffix == "config":
                status, response = self.store.deploy_project(payload, project_id=project_id)
                self._send_json(status, response)
                return
            if suffix.startswith("skills/"):
                status, response = self.store.add_project_skill(project_id, payload)
                self._send_json(status, response)
                return
        self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", "No mock route matched."))

    def do_PATCH(self) -> None:
        """Handle API key metadata updates."""

        parsed = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            return
        if parsed.path.startswith("/api/v1/admin/keys/"):
            key_id = _path_suffix(parsed.path, "/api/v1/admin/keys/")
            status, response = self.store.update_api_key(key_id, payload)
            self._send_json(status, response)
            return
        self._send_json(HTTPStatus.NOT_FOUND, _error("not_found", "No mock route matched."))

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet unless the server is run verbosely."""
        if not self.quiet:
            super().log_message(format, *args)

    def _read_json(self) -> dict[str, Any] | None:
        """Read a JSON object request body."""
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, _error("invalid_json", "Body must be JSON."))
            return None
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_json", "Body must be an object."),
            )
            return None
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """Send a JSON response."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, response: dict[str, Any]) -> None:
        """Send a semantic SSE response."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event, payload in sse_events_for_response(response):
            data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())


def make_handler(store: LocalGAMockStore, *, quiet: bool) -> type[LocalGAMockHandler]:
    """Return a handler class bound to one mock store."""

    class BoundLocalGAMockHandler(LocalGAMockHandler):
        """Handler with injected process-local state."""

    BoundLocalGAMockHandler.store = store
    BoundLocalGAMockHandler.quiet = quiet
    return BoundLocalGAMockHandler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse local mock CLI arguments."""
    parser = argparse.ArgumentParser(description="Run a local General Augment mock server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def run_server(host: str, port: int, *, quiet: bool = False) -> None:
    """Run the mock HTTP server until interrupted."""
    store = LocalGAMockStore()
    handler = make_handler(store, quiet=quiet)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"General Augment local mock listening at http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    try:
        run_server(args.host, args.port, quiet=args.quiet)
    except KeyboardInterrupt:
        print("\nStopped General Augment local mock.", file=sys.stderr)
    return 0


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Return a case-insensitive header value from mapping-like headers."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return None


def _headers_dict(headers: Any) -> dict[str, str]:
    """Return a plain string mapping for request headers."""
    return {str(key): str(value) for key, value in headers.items()}


def _auth_credential(headers: Mapping[str, str]) -> str | None:
    """Return either admin or bearer credential from request headers."""

    admin_key = _header(headers, "X-Admin-Key")
    if admin_key:
        return admin_key
    authorization = _header(headers, "Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(None, 1)[1]
    return None


def _path_suffix(path: str, prefix: str) -> str:
    """Return a URL-decoded path suffix after a known route prefix."""
    return unquote(path.removeprefix(prefix))


def _admin_project_route(path: str) -> tuple[str, str] | None:
    """Parse /api/v1/admin/projects/{project_id}/{suffix} routes."""

    prefix = "/api/v1/admin/projects/"
    if not path.startswith(prefix):
        return None
    tail = path.removeprefix(prefix)
    if "/" not in tail:
        return unquote(tail), ""
    raw_project_id, suffix = tail.split("/", 1)
    return unquote(raw_project_id), suffix


def _manifest_tool_ids(manifest: dict[str, Any]) -> list[str]:
    """Extract enabled generated tool ids from a manifest."""

    tools = manifest.get("tools") if isinstance(manifest.get("tools"), dict) else {}
    mcp_servers = tools.get("mcp") if isinstance(tools, dict) else []
    tool_ids: list[str] = []
    if not isinstance(mcp_servers, list):
        return tool_ids
    for server in mcp_servers:
        if not isinstance(server, dict):
            continue
        include = _manifest_server_include(server)
        if isinstance(include, list):
            tool_ids.extend(str(tool_id) for tool_id in include)
    return sorted(set(tool_ids))


def _manifest_server_include(server: dict[str, Any]) -> list[Any] | None:
    """Return tool includes from legacy or current generated MCP server shape."""

    include = server.get("include")
    nested_tools = server.get("tools")
    if include is None and isinstance(nested_tools, dict):
        include = nested_tools.get("include")
    return include if isinstance(include, list) else None


def _manifest_channels(manifest: dict[str, Any]) -> dict[str, Any]:
    """Extract configured channel blocks from a generated agent manifest."""

    channels = manifest.get("channels")
    if not isinstance(channels, dict):
        return {}
    return {str(name): value for name, value in channels.items() if isinstance(value, dict)}


def _skill_metadata(content: str) -> dict[str, Any]:
    """Return frontmatter metadata from a local mock SKILL.md file."""

    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    loaded = yaml.safe_load(parts[1]) or {}
    return loaded if isinstance(loaded, dict) else {}


def _skill_name_from_content(content: str) -> str:
    """Return the display name for a local mock SKILL.md file."""

    metadata = _skill_metadata(content)
    name = metadata.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or "Untitled Skill"
    return "Untitled Skill"


def _skill_summary(content: str) -> dict[str, Any]:
    """Return a hosted-like skill summary."""

    metadata = _skill_metadata(content)
    return {
        "name": _skill_name_from_content(content),
        "description": str(metadata.get("description") or ""),
        "version": str(metadata.get("version") or "1.0"),
        "tags": [str(tag) for tag in metadata.get("tags", [])]
        if isinstance(metadata.get("tags"), list)
        else [],
        "tools": [str(tool) for tool in metadata.get("tools", [])]
        if isinstance(metadata.get("tools"), list)
        else [],
        "path": f"skills/{_slug(_skill_name_from_content(content))}/SKILL.md",
    }


def _channel_health(channel: str, label: str, *, configured: bool) -> dict[str, Any]:
    """Return one hosted-like local channel health row."""

    return {
        "channel": channel,
        "label": label,
        "status": "configured" if configured else "open",
        "sender": f"local-mock:{channel}" if configured else None,
        "provider_status": "local_mock",
        "delivery": "available" if configured else "not_configured",
        "last_message_at": None,
        "message_count_24h": 0,
        "details": {},
    }


def _object_payload(value: Any) -> dict[str, Any]:
    """Return a string-keyed object payload or an empty mapping."""
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _copy_trace_header(
    headers: Mapping[str, str],
    metadata: dict[str, Any],
    header_name: str,
) -> None:
    """Copy bounded trace context into mock metadata."""
    value = _header(headers, header_name)
    if not value or len(value) > 512:
        return
    metadata[f"general_augment_{header_name}"] = value
    metadata.setdefault(header_name, value)


def _digest_json(value: Any) -> str:
    """Return a stable digest for JSON-compatible values."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _error(code: str, message: str) -> dict[str, Any]:
    """Return a small General Augment-like error body."""
    return {
        "code": code,
        "reason": code,
        "message": message,
        "detail": {"code": code, "reason": code, "message": message},
    }


def _extract_input_text(value: Any) -> str:
    """Extract readable text from common Responses input shapes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _extract_input_text(item)))
    if isinstance(value, dict):
        if "content" in value:
            return _extract_input_text(value["content"])
        if "text" in value:
            return str(value["text"])
        if "input_text" in value:
            return str(value["input_text"])
    return json.dumps(value, sort_keys=True, default=str)


def _mock_output_text(input_text: str, user_id: str) -> str:
    """Return deterministic mock assistant text."""
    exact = EXACT_REPLY_RE.search(input_text)
    if exact:
        return exact.group(1).strip().strip('"`')
    clipped = " ".join(input_text.split())[:160] or "empty input"
    return f"Mock General Augment response for {user_id}: {clipped}"


def _mock_response_text(payload: dict[str, Any], input_text: str, user_id: str) -> str:
    """Return deterministic text or schema-shaped JSON for a mock response."""
    text_format = _object_payload(_object_payload(payload.get("text")).get("format"))
    if text_format.get("type") == "json_schema":
        schema = _object_payload(text_format.get("schema"))
        value = _mock_value_for_schema(schema, input_text)
        return json.dumps(value, sort_keys=True)
    if text_format.get("type") == "json_object":
        return json.dumps({"ok": True, "label": _structured_label(input_text)}, sort_keys=True)
    return _mock_output_text(input_text, user_id)


def _mock_value_for_schema(schema: dict[str, Any], input_text: str) -> Any:
    """Create a small valid-ish JSON value for common smoke-test schemas."""
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), schema_type[0])

    if schema_type == "object" or (
        schema_type is None and isinstance(schema.get("properties"), dict)
    ):
        properties = _object_payload(schema.get("properties"))
        required = schema.get("required")
        if isinstance(required, list) and required:
            names = [str(name) for name in required if str(name) in properties]
        else:
            names = list(properties)
        return {
            name: _mock_named_value(name, _object_payload(properties.get(name)), input_text)
            for name in names
        }
    if schema_type == "array":
        return [_mock_value_for_schema(_object_payload(schema.get("items")), input_text)]
    if schema_type == "boolean":
        return True
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "null":
        return None
    return _structured_label(input_text)


def _mock_named_value(name: str, schema: dict[str, Any], input_text: str) -> Any:
    """Return a deterministic value with nicer defaults for common smoke fields."""
    normalized = name.lower().replace("-", "_")
    if normalized in {"ok", "success", "passed", "valid"} and "type" not in schema:
        return True
    if normalized in {"label", "status", "message", "summary"} and "type" not in schema:
        return _structured_label(input_text)
    return _mock_value_for_schema(schema, input_text)


def _structured_label(input_text: str) -> str:
    """Return a stable label for structured mock responses."""
    exact = EXACT_REPLY_RE.search(input_text)
    if exact:
        return exact.group(1).strip().strip('"`')
    if "genaug-structured-ok" in input_text:
        return "genaug-structured-ok"
    if "genaug-smoke-ok" in input_text:
        return "genaug-smoke-ok"
    return "genaug-smoke-ok"


def _display_name(value: str) -> str:
    """Create a readable display name from a slug."""

    return value.replace("-", " ").replace("_", " ").title()


def _slug(value: str) -> str:
    """Create a stable local mock slug."""

    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug or "skill"


def _external_user_id(payload: dict[str, Any]) -> str:
    """Resolve app user fields accepted by the mock memory endpoints."""
    return str(payload.get("user_id") or payload.get("user") or "mock-user")


def _mock_user_uuid(user_id: str) -> str:
    """Return a deterministic UUID-shaped General Augment user id."""
    return str(uuid5(NAMESPACE_URL, f"general-augment-local-mock:{user_id}"))


def _optional_string(value: Any) -> str | None:
    """Return a non-empty string or None."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_float(value: Any) -> float | None:
    """Return a float if the value is present and numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any, *, default: int) -> int:
    """Parse a positive integer with a default."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _lexical_score(query: str, fact: str) -> float:
    """Return a deterministic lexical similarity score."""
    normalized_query = query.strip().lower()
    normalized_fact = fact.lower()
    if not normalized_query:
        return 1.0
    if normalized_query in normalized_fact:
        return 0.99
    query_terms = set(normalized_query.split())
    fact_terms = set(normalized_fact.split())
    if not query_terms:
        return 1.0
    return len(query_terms & fact_terms) / len(query_terms)


if __name__ == "__main__":
    raise SystemExit(main())
