"""Standalone OpenAPI-to-agent scaffold helpers for the CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import yaml
from pydantic import BaseModel, Field

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
PUBLIC_MANIFEST_FILENAME = "genaug-agent.yaml"
CODING_AGENT_PROMPT_FILENAME = "CODING_AGENT_PROMPT.md"
PUBLIC_API_VERSION = "genaug/v1"
MODEL_KEYS = {"simple", "balanced", "complex"}
VALID_MODEL_PREFIXES = (
    "anthropic/",
    "claude-",
    "gemini-",
    "google/gemini-",
    "openai/",
)
TOOL_DISCOVERY_MODES = {"auto", "always", "direct"}
DEFAULT_TOOL_DISCOVERY: dict[str, int | str] = {
    "mode": "auto",
    "direct_schema_tool_limit": 10,
    "max_search_results": 5,
}
SENSITIVE_KEY_MARKERS = ("auth", "authorization", "api_key", "apikey", "key", "secret", "token")
SECRET_PLACEHOLDER_RE = re.compile(r"\$\{\{\s*(secrets|credentials)\.[A-Za-z0-9_.-]+\s*\}\}")


class ToolCandidate(BaseModel):
    """Generated API tool metadata."""

    tool_id: str
    name: str
    description: str
    http_method: str
    path: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: str
    requires_approval: bool
    enabled: bool = True


class ParsedAPI(BaseModel):
    """Parsed OpenAPI specification fields needed by the CLI."""

    title: str
    version: str
    description: str
    base_url: str
    auth_schemes: list[str]
    tools: list[ToolCandidate]


@dataclass(frozen=True)
class ScaffoldResult:
    """Local files generated from an OpenAPI specification."""

    root: Path
    config_path: Path
    soul_path: Path
    tools_dir: Path
    env_path: Path
    agent_prompt_path: Path
    parsed_api: ParsedAPI
    tools: list[ToolCandidate]


@dataclass(frozen=True)
class BasicScaffoldResult:
    """Local files generated for a starter agent without an OpenAPI spec."""

    root: Path
    config_path: Path
    soul_path: Path
    skills_dir: Path
    tools_dir: Path
    env_path: Path
    agent_prompt_path: Path
    builtin_tools: list[str]


@dataclass(frozen=True)
class LocalValidationResult:
    """Local validation result for a genaug-agent.yaml manifest."""

    config_path: Path
    status: str
    project_name: str | None
    errors: list[str]
    warnings: list[str]
    soul_file: Path | None
    skills_dir: Path | None
    skill_count: int
    builtin_tools: list[str]
    mcp_servers: list[str]
    tool_discovery: dict[str, int | str]


def parse_openapi(spec_source: str) -> ParsedAPI:
    """Parse an OpenAPI spec from URL, file path, or raw JSON/YAML."""
    spec = _load_spec(spec_source)
    raw_info = spec.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    raw_servers = spec.get("servers")
    servers: list[Any] = raw_servers if isinstance(raw_servers, list) else []
    first_server = servers[0] if servers and isinstance(servers[0], dict) else {}
    first_server_data: dict[str, Any] = cast(dict[str, Any], first_server)
    title = str(info.get("title") or "API")
    tools = _extract_tools(spec)
    return ParsedAPI(
        title=title,
        version=str(info.get("version") or "1.0.0"),
        description=str(info.get("description") or ""),
        base_url=str(first_server_data.get("url") or ""),
        auth_schemes=_extract_auth_schemes(spec),
        tools=tools,
    )


def auto_curate(tools: list[ToolCandidate], target_count: int) -> list[ToolCandidate]:
    """Curate generated tools using simple local heuristics."""
    visible = [
        tool
        for tool in tools
        if not any(marker in tool.path.lower() for marker in ("/admin", "/internal", "/debug"))
    ]
    ranked = sorted(visible, key=lambda tool: (_risk_rank(tool.risk_level), tool.tool_id))
    curated = ranked[:target_count]
    return [
        tool.model_copy(update={"enabled": tool.risk_level != "high"})
        for tool in curated
    ]


def scaffold_basic_agent(
    *,
    name: str,
    output_dir: Path | None,
    display_name: str | None = None,
    description: str | None = None,
    builtin_tools: list[str] | None = None,
    force: bool = False,
) -> BasicScaffoldResult:
    """Generate a deployable starter agent project without requiring an OpenAPI spec."""
    slug = _slugify(name)
    resolved_display_name = display_name or _display_name(name)
    project_description = (
        description
        or f"{resolved_display_name} helps app users complete useful work with memory and tools."
    )
    root = output_dir or (Path.cwd() / f"{slug}-agent")
    config_path = root / PUBLIC_MANIFEST_FILENAME
    soul_path = root / "SOUL.md"
    skills_dir = root / "skills"
    tools_dir = root / "tools"
    env_path = root / ".env.example"
    agent_prompt_path = root / CODING_AGENT_PROMPT_FILENAME
    files = [
        config_path,
        soul_path,
        env_path,
        agent_prompt_path,
        skills_dir / "README.md",
        tools_dir / "README.md",
    ]
    existing = [path for path in files if path.exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing files: {names}")
    skills_dir.mkdir(parents=True, exist_ok=True)
    tools_dir.mkdir(parents=True, exist_ok=True)
    normalized_tools = _normalize_builtin_tools(builtin_tools or [])
    config_path.write_text(
        _agent_yaml(
            slug=slug,
            display_name=resolved_display_name,
            role=f"{resolved_display_name} Agent",
            description=project_description,
            tools=[],
            api_version=PUBLIC_API_VERSION,
            builtin_tools=normalized_tools,
        ),
        encoding="utf-8",
    )
    soul_path.write_text(
        _soul_md(
            display_name=resolved_display_name,
            role=f"{resolved_display_name} Agent",
            description=project_description,
        ),
        encoding="utf-8",
    )
    env_path.write_text(_env_example([]), encoding="utf-8")
    agent_prompt_path.write_text(
        _coding_agent_prompt(
            slug=slug,
            display_name=resolved_display_name,
            description=project_description,
        ),
        encoding="utf-8",
    )
    (skills_dir / "README.md").write_text(
        "# Skills\n\nAdd SKILL.md files here for repeatable tenant workflows.\n",
        encoding="utf-8",
    )
    (tools_dir / "README.md").write_text(
        (
            "# Tools\n\n"
            "Use `genaug tools toggle`, `genaug mcp add`, or `genaug integrate` "
            "to add governed tools after the starter agent is created.\n"
        ),
        encoding="utf-8",
    )
    return BasicScaffoldResult(
        root=root,
        config_path=config_path,
        soul_path=soul_path,
        skills_dir=skills_dir,
        tools_dir=tools_dir,
        env_path=env_path,
        agent_prompt_path=agent_prompt_path,
        builtin_tools=normalized_tools,
    )


def validate_local_agent_config(config_path: Path) -> LocalValidationResult:
    """Validate a local genaug-agent.yaml manifest without calling the hosted API."""
    errors: list[str] = []
    warnings: list[str] = []
    payload = _load_yaml_mapping(config_path, errors)
    project_name: str | None = None
    soul_file: Path | None = None
    skills_dir: Path | None = None
    skill_count = 0
    builtin_tools: list[str] = []
    mcp_servers: list[str] = []
    tool_discovery = dict(DEFAULT_TOOL_DISCOVERY)
    if payload is not None:
        _validate_manifest_identity(payload, errors)
        project_name = _validate_metadata(payload.get("metadata"), errors)
        _validate_model_routes(payload.get("model"), errors, warnings)
        soul_file = _validate_personality(payload.get("personality"), config_path, errors, warnings)
        builtin_tools, mcp_servers = _validate_tools(payload.get("tools"), errors, warnings)
        skills_dir, skill_count = _validate_skills(payload.get("skills"), config_path, warnings)
        tool_discovery = _validate_behavior(payload.get("behavior"), errors)
        _validate_channels(payload.get("channels"), warnings)
    return LocalValidationResult(
        config_path=config_path,
        status="FAIL" if errors else "PASS",
        project_name=project_name,
        errors=errors,
        warnings=warnings,
        soul_file=soul_file,
        skills_dir=skills_dir,
        skill_count=skill_count,
        builtin_tools=builtin_tools,
        mcp_servers=mcp_servers,
        tool_discovery=tool_discovery,
    )


def scaffold_from_openapi(
    spec_source: str,
    *,
    output_dir: Path | None,
    name: str | None,
    description: str | None,
    target_count: int = 15,
    force: bool = False,
) -> ScaffoldResult:
    """Generate a deployable local agent project from an OpenAPI spec."""
    parsed = parse_openapi(spec_source)
    tools = auto_curate(parsed.tools, target_count=target_count)
    slug = _slugify(name or parsed.title)
    display_name = _display_name(name or parsed.title)
    root = output_dir or (Path.cwd() / f"{slug}-agent")
    config_path = root / PUBLIC_MANIFEST_FILENAME
    soul_path = root / "SOUL.md"
    tools_dir = root / "tools"
    env_path = root / ".env.example"
    agent_prompt_path = root / CODING_AGENT_PROMPT_FILENAME
    skill_readme = root / "skills" / "README.md"
    tool_paths = [tools_dir / f"{tool.tool_id}.yaml" for tool in tools]
    files = [
        config_path,
        soul_path,
        env_path,
        agent_prompt_path,
        skill_readme,
        *tool_paths,
    ]
    existing = [path for path in files if path.exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing files: {names}")
    tools_dir.mkdir(parents=True, exist_ok=True)
    skill_readme.parent.mkdir(parents=True, exist_ok=True)
    project_description = description or parsed.description or f"{display_name} API assistant."
    public_manifest = _agent_yaml(
        slug=slug,
        display_name=display_name,
        role=f"{parsed.title} Assistant",
        description=project_description,
        tools=tools,
        api_version=PUBLIC_API_VERSION,
    )
    config_path.write_text(public_manifest, encoding="utf-8")
    soul_path.write_text(
        _soul_md(
            display_name=display_name,
            role=f"{parsed.title} Assistant",
            description=project_description,
        ),
        encoding="utf-8",
    )
    env_path.write_text(_env_example(parsed.auth_schemes), encoding="utf-8")
    agent_prompt_path.write_text(
        _coding_agent_prompt(
            slug=slug,
            display_name=display_name,
            description=project_description,
        ),
        encoding="utf-8",
    )
    skill_readme.write_text(
        "# Skills\n\nAdd SKILL.md files here for repeatable workflows.\n",
        encoding="utf-8",
    )
    for tool in tools:
        (tools_dir / f"{tool.tool_id}.yaml").write_text(
            yaml.safe_dump(tool.model_dump(), sort_keys=False),
            encoding="utf-8",
        )
    return ScaffoldResult(
        root=root,
        config_path=config_path,
        soul_path=soul_path,
        tools_dir=tools_dir,
        env_path=env_path,
        agent_prompt_path=agent_prompt_path,
        parsed_api=parsed,
        tools=tools,
    )


def load_deploy_payload(config_path: Path) -> dict[str, Any]:
    """Validate and load local agent config plus optional SOUL.md and skills."""
    validation = validate_local_agent_config(config_path)
    if validation.errors:
        raise ValueError(f"Agent manifest validation failed: {'; '.join(validation.errors)}")
    yaml_content = config_path.read_text(encoding="utf-8")
    payload = yaml.safe_load(yaml_content)
    if not isinstance(payload, dict):
        raise ValueError("Agent manifest must contain a YAML object.")
    if payload.get("apiVersion") != PUBLIC_API_VERSION or payload.get("kind") != "Agent":
        raise ValueError(
            "Agent manifest must use apiVersion genaug/v1 and kind Agent."
        )
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise ValueError("Agent manifest metadata.name is required.")

    personality = payload.get("personality") if isinstance(payload.get("personality"), dict) else {}
    soul_content = None
    soul_file = personality.get("soul_file") if isinstance(personality, dict) else None
    if isinstance(soul_file, str) and soul_file:
        soul_path = (config_path.parent / soul_file).resolve()
        soul_content = soul_path.read_text(encoding="utf-8")

    skills: list[str] = []
    skills_block = payload.get("skills") if isinstance(payload.get("skills"), dict) else {}
    skills_dir = skills_block.get("directory") if isinstance(skills_block, dict) else None
    if isinstance(skills_dir, str) and skills_dir:
        root = (config_path.parent / skills_dir).resolve()
        if root.exists():
            skills = [
                path.read_text(encoding="utf-8")
                for path in sorted(root.rglob("*.md"))
                if path.name.upper() == "SKILL.MD"
            ]
    return {
        "yaml_content": yaml_content,
        "soul_content": soul_content,
        "skills": skills,
    }


def project_name_from_config(config_path: Path) -> str:
    """Return metadata.name from a local config."""
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        raise ValueError("Agent manifest metadata.name is required.")
    name = payload["metadata"].get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Agent manifest metadata.name is required.")
    return name


def _load_yaml_mapping(config_path: Path, errors: list[str]) -> dict[str, Any] | None:
    """Load a YAML object for local validation."""
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"Invalid YAML: {exc}")
        return None
    except OSError as exc:
        errors.append(f"Could not read manifest: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append("Agent manifest must contain a YAML object.")
        return None
    return payload


def _validate_manifest_identity(payload: dict[str, Any], errors: list[str]) -> None:
    """Validate top-level manifest identity fields."""
    if payload.get("apiVersion") != PUBLIC_API_VERSION or payload.get("kind") != "Agent":
        errors.append("Agent manifest must use apiVersion genaug/v1 and kind Agent.")


def _validate_metadata(metadata: object, errors: list[str]) -> str | None:
    """Validate metadata and return the project name."""
    if not isinstance(metadata, dict):
        errors.append("metadata must be an object.")
        return None
    raw_name = metadata.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        errors.append("metadata.name is required.")
        return None
    if not _slugify(raw_name):
        errors.append("metadata.name must contain at least one alphanumeric character.")
    display_name = metadata.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        errors.append("metadata.display_name must be a string when provided.")
    return raw_name


def _validate_model_routes(model: object, errors: list[str], warnings: list[str]) -> None:
    """Validate local model tier route declarations."""
    if model is None:
        warnings.append("model block is missing; server defaults will apply.")
        return
    if not isinstance(model, dict):
        errors.append("model must be an object with simple, balanced, and complex slots.")
        return
    model_keys = {str(key) for key in model}
    unknown_model_keys = sorted(model_keys - MODEL_KEYS)
    missing_model_keys = sorted(MODEL_KEYS - model_keys)
    if unknown_model_keys:
        errors.append(f"Unknown model slots: {', '.join(unknown_model_keys)}")
    if missing_model_keys:
        errors.append(f"Missing model slots: {', '.join(missing_model_keys)}")
    for slot, model_name in sorted(model.items()):
        if not isinstance(model_name, str) or not _valid_model_name(model_name):
            errors.append(f"Invalid model for {slot}: {model_name}")


def _validate_personality(
    personality: object,
    config_path: Path,
    errors: list[str],
    warnings: list[str],
) -> Path | None:
    """Validate personality references and return a resolved SOUL path when present."""
    if personality is None:
        warnings.append("personality block is missing; hosted defaults will apply.")
        return None
    if not isinstance(personality, dict):
        errors.append("personality must be an object.")
        return None
    raw_soul_file = personality.get("soul_file")
    description = personality.get("description")
    if raw_soul_file is None:
        if not isinstance(description, str) or not description.strip():
            warnings.append("No personality.soul_file or personality.description was provided.")
        return None
    if not isinstance(raw_soul_file, str) or not raw_soul_file.strip():
        errors.append("personality.soul_file must be a non-empty string when provided.")
        return None
    soul_path = (config_path.parent / raw_soul_file).resolve()
    if not soul_path.is_file():
        errors.append(f"personality.soul_file was not found: {raw_soul_file}")
        return soul_path
    return soul_path


def _validate_tools(
    tools: object,
    errors: list[str],
    warnings: list[str],
) -> tuple[list[str], list[str]]:
    """Validate builtin and MCP tool declarations."""
    if tools is None:
        warnings.append("tools block is missing; no tools will be enabled by this manifest.")
        return [], []
    if not isinstance(tools, dict):
        errors.append("tools must be an object.")
        return [], []
    builtin = _string_list(tools.get("builtin"), field_name="tools.builtin", errors=errors)
    duplicates = sorted({tool for tool in builtin if builtin.count(tool) > 1})
    if duplicates:
        warnings.append(f"Duplicate builtin tools should be reviewed: {', '.join(duplicates)}")
    raw_mcp = tools.get("mcp")
    if raw_mcp is None:
        return builtin, []
    if not isinstance(raw_mcp, list):
        errors.append("tools.mcp must be a list.")
        return builtin, []
    server_names: list[str] = []
    for index, server in enumerate(raw_mcp):
        if not isinstance(server, dict):
            errors.append(f"tools.mcp[{index}] must be an object.")
            continue
        server_name = _validate_mcp_server(server, index, errors)
        if server_name:
            server_names.append(server_name)
    return builtin, server_names


def _validate_mcp_server(server: dict[str, Any], index: int, errors: list[str]) -> str | None:
    """Validate one local MCP server declaration."""
    name = server.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"tools.mcp[{index}].name is required.")
        server_name = None
    else:
        server_name = name
    has_url = bool(server.get("url"))
    has_command = bool(server.get("command"))
    if has_url == has_command:
        errors.append(f"tools.mcp[{index}] must define exactly one of url or command.")
    auth = server.get("auth")
    if auth is not None and (
        not isinstance(auth, str) or not _contains_secret_placeholder(auth)
    ):
        errors.append(
            f"tools.mcp[{index}].auth must use a credential placeholder such as "
            "${{ secrets.NAME }} or ${{ credentials.name }}."
        )
    _validate_secret_mapping(server.get("headers"), f"tools.mcp[{index}].headers", errors)
    _validate_secret_mapping(server.get("env"), f"tools.mcp[{index}].env", errors)
    _validate_mcp_tool_filters(server.get("tools"), index, errors)
    return server_name


def _validate_secret_mapping(value: object, field_name: str, errors: list[str]) -> None:
    """Validate sensitive header/env values use placeholders."""
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{field_name} must be an object.")
        return
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not isinstance(raw_value, str):
            errors.append(f"{field_name}.{key} must be a string.")
            continue
        if _is_sensitive_key(key) and raw_value and not _contains_secret_placeholder(raw_value):
            errors.append(
                f"{field_name}.{key} must use a credential placeholder, not a raw secret."
            )


def _validate_mcp_tool_filters(value: object, index: int, errors: list[str]) -> None:
    """Validate optional MCP include/exclude lists."""
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"tools.mcp[{index}].tools must be an object.")
        return
    for filter_name in ("include", "exclude"):
        if filter_name in value:
            _string_list(
                value.get(filter_name),
                field_name=f"tools.mcp[{index}].tools.{filter_name}",
                errors=errors,
            )


def _validate_skills(
    skills: object,
    config_path: Path,
    warnings: list[str],
) -> tuple[Path | None, int]:
    """Validate skills directory and return resolved directory plus SKILL.md count."""
    if skills is None:
        warnings.append("skills block is missing; no local skills will be deployed.")
        return None, 0
    if not isinstance(skills, dict):
        warnings.append("skills block is not an object; no local skills were counted.")
        return None, 0
    raw_directory = skills.get("directory")
    if not isinstance(raw_directory, str) or not raw_directory.strip():
        warnings.append("skills.directory is missing; no local skills will be deployed.")
        return None, 0
    skills_dir = (config_path.parent / raw_directory).resolve()
    if not skills_dir.exists():
        warnings.append(f"skills.directory was not found: {raw_directory}")
        return skills_dir, 0
    if not skills_dir.is_dir():
        warnings.append(f"skills.directory is not a directory: {raw_directory}")
        return skills_dir, 0
    skill_count = sum(1 for path in skills_dir.rglob("SKILL.md") if path.is_file())
    return skills_dir, skill_count


def _validate_behavior(behavior: object, errors: list[str]) -> dict[str, int | str]:
    """Validate behavior controls and return normalized tool discovery."""
    if behavior is None:
        return dict(DEFAULT_TOOL_DISCOVERY)
    if not isinstance(behavior, dict):
        errors.append("behavior must be an object.")
        return dict(DEFAULT_TOOL_DISCOVERY)
    for field_name in (
        "max_tool_calls_per_turn",
        "session_timeout_minutes",
        "messages_per_user_per_minute",
    ):
        if field_name in behavior and not _positive_int_value(behavior.get(field_name)):
            errors.append(f"behavior.{field_name} must be a positive integer.")
    if "daily_token_budget_usd" in behavior:
        budget = behavior.get("daily_token_budget_usd")
        if isinstance(budget, bool) or not isinstance(budget, int | float) or budget < 0:
            errors.append("behavior.daily_token_budget_usd must be a non-negative number.")
    return _validate_tool_discovery(behavior.get("tool_discovery"), errors)


def _validate_tool_discovery(value: object, errors: list[str]) -> dict[str, int | str]:
    """Validate local tool discovery config."""
    if value is None:
        return dict(DEFAULT_TOOL_DISCOVERY)
    if not isinstance(value, dict):
        errors.append("behavior.tool_discovery must be an object.")
        return dict(DEFAULT_TOOL_DISCOVERY)
    mode = str(value.get("mode") or DEFAULT_TOOL_DISCOVERY["mode"]).casefold()
    if mode not in TOOL_DISCOVERY_MODES:
        errors.append("behavior.tool_discovery.mode must be one of: auto, always, direct.")
        mode = str(DEFAULT_TOOL_DISCOVERY["mode"])
    direct_limit = _positive_int_or_default(
        value.get("direct_schema_tool_limit"),
        default=int(DEFAULT_TOOL_DISCOVERY["direct_schema_tool_limit"]),
        field_name="behavior.tool_discovery.direct_schema_tool_limit",
        errors=errors,
    )
    max_results = _positive_int_or_default(
        value.get("max_search_results"),
        default=int(DEFAULT_TOOL_DISCOVERY["max_search_results"]),
        field_name="behavior.tool_discovery.max_search_results",
        errors=errors,
    )
    if max_results > 10:
        errors.append(
            "behavior.tool_discovery.max_search_results must be less than or equal to 10."
        )
        max_results = 10
    return {
        "mode": mode,
        "direct_schema_tool_limit": direct_limit,
        "max_search_results": max_results,
    }


def _validate_channels(channels: object, warnings: list[str]) -> None:
    """Warn when channel config is omitted entirely."""
    if channels is None:
        warnings.append("channels block is missing; channel setup can be added later.")


def _load_spec(spec_source: str) -> dict[str, Any]:
    """Load an OpenAPI document."""
    if spec_source.startswith(("http://", "https://")):
        response = httpx.get(spec_source, timeout=30.0)
        response.raise_for_status()
        text = response.text
    else:
        path = Path(spec_source).expanduser()
        text = path.read_text(encoding="utf-8") if path.exists() else spec_source
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict) or "paths" not in payload:
        raise ValueError("OpenAPI document must contain a paths object.")
    return payload


def _extract_tools(spec: dict[str, Any]) -> list[ToolCandidate]:
    """Extract OpenAPI operations as tool candidates."""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return []
    tools: list[ToolCandidate] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method_lower = str(method).lower()
            if method_lower not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            risk_level, requires_approval = _classify_risk(method_lower)
            tool_id = _sanitize_tool_id(
                str(operation.get("operationId") or ""),
                method_lower,
                str(path),
            )
            tools.append(
                ToolCandidate(
                    tool_id=tool_id,
                    name=str(operation.get("summary") or _display_name(tool_id)),
                    description=str(
                        operation.get("description")
                        or operation.get("summary")
                        or f"{method_lower.upper()} {path}"
                    ),
                    http_method=method_lower.upper(),
                    path=str(path),
                    input_schema=_input_schema(path_item, operation),
                    risk_level=risk_level,
                    requires_approval=requires_approval,
                    enabled=risk_level != "high",
                )
            )
    return tools


def _input_schema(path_item: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON schema for one operation input."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    parameters = [
        *[item for item in path_item.get("parameters", []) if isinstance(item, dict)],
        *[item for item in operation.get("parameters", []) if isinstance(item, dict)],
    ]
    for parameter in parameters:
        name = parameter.get("name")
        if not isinstance(name, str):
            continue
        raw_schema = parameter.get("schema")
        schema: dict[str, Any] = raw_schema if isinstance(raw_schema, dict) else {}
        properties[name] = {
            **schema,
            "description": str(
                parameter.get("description") or f"{parameter.get('in', 'parameter')} parameter"
            ),
        }
        if parameter.get("required") or parameter.get("in") == "path":
            required.append(name)
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        request_body_data: dict[str, Any] = request_body
        raw_content = request_body_data.get("content")
        content = (
            raw_content if isinstance(raw_content, dict) else {}
        )
        content_data: dict[str, Any] = content
        raw_json_body = content_data.get("application/json")
        json_body = (
            raw_json_body if isinstance(raw_json_body, dict) else {}
        )
        json_body_data: dict[str, Any] = json_body
        raw_body_schema = json_body_data.get("schema")
        body_schema: dict[str, Any] = (
            raw_body_schema if isinstance(raw_body_schema, dict) else {}
        )
        properties["body"] = {"type": "object", "description": "JSON request body", **body_schema}
        if request_body_data.get("required"):
            required.append("body")
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(required)),
    }


def _extract_auth_schemes(spec: dict[str, Any]) -> list[str]:
    """Extract readable auth scheme summaries."""
    raw_components = spec.get("components")
    components: dict[str, Any] = raw_components if isinstance(raw_components, dict) else {}
    raw_schemes = components.get("securitySchemes")
    schemes: dict[str, Any] = raw_schemes if isinstance(raw_schemes, dict) else {}
    result = []
    for name, scheme in schemes.items():
        if isinstance(scheme, dict):
            result.append(f"{name}: {scheme.get('type', 'unknown')}")
    return result


def _classify_risk(method: str) -> tuple[str, bool]:
    """Return risk level and approval requirement."""
    if method == "get":
        return "low", False
    if method == "delete":
        return "high", True
    return "medium", True


def _sanitize_tool_id(operation_id: str, method: str, path: str) -> str:
    """Return a valid, stable tool id."""
    source = operation_id or f"{method}_{path.strip('/')}"
    tool_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", source).strip("_-").lower()
    return (tool_id or "tool")[:64]


def _agent_yaml(
    *,
    slug: str,
    display_name: str,
    role: str,
    description: str,
    tools: list[ToolCandidate],
    api_version: str,
    builtin_tools: list[str] | None = None,
) -> str:
    """Create a deployable agent manifest document."""
    enabled_tools = [tool.tool_id for tool in tools if tool.enabled]
    mcp_tools = [
        {
            "name": f"{slug}-api",
            "url": f"http://localhost:9090/{slug}",
            "tools": {"include": enabled_tools},
        }
    ] if enabled_tools else []
    payload = {
        "apiVersion": api_version,
        "kind": "Agent",
        "metadata": {"name": slug, "display_name": display_name, "version": "1.0.0"},
        "personality": {
            "role": role,
            "soul_file": "./SOUL.md",
            "description": description,
            "tone": "concise, proactive, careful",
            "rules": ["Confirm before write actions.", "Explain missing account links plainly."],
        },
        "model": {
            "simple": "google/gemini-2.5-flash-lite",
            "balanced": "google/gemini-2.5-flash",
            "complex": "google/gemini-2.5-pro",
        },
        "tools": {
            "builtin": list(builtin_tools or []),
            "mcp": mcp_tools,
        },
        "skills": {"directory": "./skills/", "learning_enabled": True},
        "channels": {"whatsapp": {}, "sms": {}, "telegram": {}},
        "behavior": {
            "max_tool_calls_per_turn": 10,
            "session_timeout_minutes": 30,
            "daily_token_budget_usd": 50.0,
            "messages_per_user_per_minute": 30,
            "tool_discovery": {
                "mode": "auto",
                "direct_schema_tool_limit": 10,
                "max_search_results": 5,
            },
        },
        "welcome": {"message": f"Hi, I'm {display_name}. How can I help?"},
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _soul_md(*, display_name: str, role: str, description: str) -> str:
    """Create SOUL.md content."""
    return f"""---
name: {display_name}
role: {role}
tone: concise, proactive, careful
---

# {display_name}

{description}

## Rules

- Confirm before changing user data.
- Keep chat responses concise.
- Ask users to link their account when identity is missing.
"""


def _env_example(auth_schemes: list[str]) -> str:
    """Create .env.example content for generated integrations."""
    lines = [
        "GENAUG_ADMIN_API_KEY=",
        "GENAUG_API_BASE_URL=https://api.generalaugment.com",
    ]
    for scheme in auth_schemes:
        key = re.sub(r"[^A-Z0-9]+", "_", scheme.upper()).strip("_")
        lines.append(f"{key or 'API_AUTH'}=")
    return "\n".join(dict.fromkeys(lines)) + "\n"


def _coding_agent_prompt(*, slug: str, display_name: str, description: str) -> str:
    """Create a paste-ready coding agent handoff for app developers."""
    return f"""# Coding Agent Handoff

Paste this into the coding agent that owns your app backend.

```text
You are integrating our app backend with General Augment.

Goal:
- Keep General Augment API keys server-side only.
- Use our app's stable signed-in user id as the Responses API `user` value.
- Call `POST /v1/responses` from the backend, never from browser or mobile code.
- Add app APIs as governed tools only through approved OpenAPI or MCP registration.
- Keep write actions approval-required and destructive actions disabled until reviewed.
- Prove the setup with CLI smoke and verify before production traffic.

Project:
- General Augment project slug: {slug}
- Agent display name: {display_name}
- Agent purpose: {description}

Required environment variables:
- GENAUG_API_BASE_URL=https://api.generalaugment.com
- GENAUG_API_KEY=<project-api-key-from-dashboard-or-cli>

Implementation steps:
1. Install or run the CLI:
   pip install --upgrade general-augment-cli
   genaug --version
   # Private-beta repo fallback if the package is not available yet:
   uv run --project packages/cli genaug --version
   # Use `genaug` below for an installed CLI, or prefix commands with
   # `uv run --project packages/cli` from the repo checkout.
2. Authenticate and diagnose:
   genaug auth login --api-key "$GENAUG_API_KEY" --base-url "$GENAUG_API_BASE_URL"
   genaug doctor --json
   genaug auth whoami
3. Review this scaffold:
   - genaug-agent.yaml
   - SOUL.md
   - skills/
   - tools/
4. Deploy the scaffold:
   genaug deploy ./genaug-agent.yaml
5. Wire the backend helper:
   - POST "$GENAUG_API_BASE_URL/v1/responses"
   - Authorization: Bearer $GENAUG_API_KEY
   - Body includes model, user, input, metadata.feature, and metadata.trace_id.
   - Store returned response id and metadata.general_augment_trace_id in app logs.
6. Add explicit memory only for durable facts:
   - POST /api/v1/agent/memory/store with user_id matching the Responses `user`.
   - Search/profile/delete memory through the server-side project key only.
7. Verify before launch:
   genaug smoke --project {slug} --message "Reply exactly with: ok" --json
   genaug verify --project {slug} --json
   genaug onboarding verify --project {slug} --json

Do not:
- Commit API keys.
- Put General Augment keys in client-side code.
- Send secrets in request metadata, memory facts, SOUL.md, skills, or tool definitions.
- Enable destructive tools until product approval UX exists.

Return a final ready/blocked report with exact commands run, response id, trace id,
dashboard links, CLI/API versions, rate-limit or budget warnings, and any missing auth,
keys, network, provider, memory, trace, or dashboard setup.
```
"""


def _risk_rank(risk_level: str) -> int:
    """Rank tools for auto-curation."""
    return {"low": 0, "medium": 1, "high": 2}.get(risk_level, 3)


def _slugify(value: str) -> str:
    """Create a project slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "agent"


def _normalize_builtin_tools(tools: list[str]) -> list[str]:
    """Return stable, de-duplicated builtin tool ids."""
    normalized: list[str] = []
    for tool in tools:
        value = re.sub(r"[^a-z0-9_-]+", "_", tool.lower()).strip("_-")
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _string_list(value: object, *, field_name: str, errors: list[str]) -> list[str]:
    """Return a string list or append a validation error."""
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{field_name} must be a list.")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field_name}[{index}] must be a non-empty string.")
            continue
        result.append(item)
    return result


def _valid_model_name(value: str) -> bool:
    """Validate model identifiers used by the current public manifest format."""
    return bool(value) and value.startswith(VALID_MODEL_PREFIXES) and " " not in value


def _contains_secret_placeholder(value: str) -> bool:
    """Return whether a value contains an accepted credential placeholder."""
    return bool(SECRET_PLACEHOLDER_RE.search(value))


def _is_sensitive_key(value: str) -> bool:
    """Return whether an env/header key is likely secret-bearing."""
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower())
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def _positive_int_value(value: object) -> bool:
    """Return whether a value is a positive integer."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _positive_int_or_default(
    value: object,
    *,
    default: int,
    field_name: str,
    errors: list[str],
) -> int:
    """Return a positive integer or record an error and return a default."""
    if value is None:
        return default
    if not isinstance(value, bool) and isinstance(value, int) and value >= 1:
        return value
    errors.append(f"{field_name} must be a positive integer.")
    return default


def _display_name(value: str) -> str:
    """Create a display name."""
    return value.replace("-", " ").replace("_", " ").title()
