"""Tool registration helpers for General Augment integrations."""

from __future__ import annotations

from genaug.client import GeneralAugmentClient


def register_from_openapi(
    spec_url: str,
    *,
    client: GeneralAugmentClient,
    project_id: str,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    target_count: int = 15,
    auto_deploy: bool = True,
) -> dict[str, object]:
    """Generate and register curated MCP tools from an OpenAPI specification."""
    return client.register_openapi_tools(
        project_id,
        spec_url,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
        target_count=target_count,
        auto_deploy=auto_deploy,
    )
