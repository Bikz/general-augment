from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from platform_cli.commands.certification import _probe_runtime_management_denial
from platform_cli.config import CLIConfig
from platform_cli.runtime import Runtime


def test_runtime_denial_probe_returns_only_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            observed["timeout"] = timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            observed.update({"url": url, "headers": headers})
            return httpx.Response(403)

    monkeypatch.setattr("platform_cli.commands.certification.httpx.Client", FakeClient)
    runtime = Runtime(
        config=CLIConfig(
            base_url="https://api.example.test",
            runtime_api_key="ga_runtime_synthetic_never_print",
        ),
        config_path=tmp_path / "config.yaml",
        loaded_config_path=tmp_path / "config.yaml",
    )
    evidence = _probe_runtime_management_denial(runtime)

    assert evidence.payload == {
        "schema_version": "general-augment-runtime-management-denial/v1",
        "checked_at": evidence.checked_at,
        "status": 403,
    }
    assert "ga_runtime_synthetic_never_print" not in json.dumps(evidence.payload)
    assert observed["headers"] == {"X-Admin-Key": "ga_runtime_synthetic_never_print"}
