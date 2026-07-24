"""Test-wide pytest fixtures.

We auto-disable MCP on-disk discovery in every test. Without this, any
test that builds the agent picks up the developer's actual Claude
Client / Cursor / Codex MCP server list — which (a) makes the test
flaky (tries to spawn npx / uv subprocesses that may not exist in CI)
and (b) leaks environment-specific config into otherwise hermetic
tests. Tests that DO want to exercise discovery (test_mcp.py) opt
back in explicitly by setting SHEJANE_RUNTIME_MCP_DISCOVERY=on inside
the test body.

We also disable LangSmith/LangChain external tracing. Agent tests create many
LangGraph runs; inheriting a developer shell's LANGSMITH_* credentials turns
unit tests into networked trace ingestion and can produce rate-limit noise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _install_test_streaming_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep streaming integration fixtures hermetic without a live provider."""
    # Production defaults complex runs to incremental execution. Unrelated
    # tests opt out so their scripted providers do not need plan transitions;
    # plan-first tests explicitly enable auto/always at their own boundary.
    monkeypatch.setenv("SHEJANE_PLAN_FIRST", "off")
    from shejane_runtime.agent import builder
    from shejane_runtime.llm.fake import FakeBackendChatModel
    from tests.streaming_model import TestStreamingChatModel

    original_build_chat_model = builder._build_chat_model

    def _build_test_model(
        settings: Any,
        run_id: str,
        mode: str,
        **kwargs: Any,
    ) -> Any:
        model_binding = kwargs.get("model_binding")
        if isinstance(model_binding, dict) and model_binding.get("adapter_id") == "openai_chat":
            return original_build_chat_model(settings, run_id, mode, **kwargs)
        if settings.fake_llm:
            return FakeBackendChatModel(
                profile={
                    "tool_calling": True,
                    "max_input_tokens": settings.unknown_model_max_input_tokens,
                    "max_output_tokens": settings.unknown_model_max_output_tokens,
                }
            )
        return TestStreamingChatModel(
            endpoint_base_url="http://test-provider",
            api_token="test-only",
            mode=mode,
            run_id=run_id,
            request_timeout_s=settings.model_request_timeout_seconds,
            max_output_tokens=settings.unknown_model_max_output_tokens,
            profile={
                "tool_calling": True,
                "max_input_tokens": settings.unknown_model_max_input_tokens,
                "max_output_tokens": settings.unknown_model_max_output_tokens,
            },
        )

    monkeypatch.setattr(builder, "_build_chat_model", _build_test_model)

    from shejane_runtime.runs import RunCoordinator

    original_model_binding = RunCoordinator._model_binding
    original_model_binding_error = RunCoordinator._model_binding_error

    async def _model_binding(
        coordinator: Any,
        principal_id: str,
        mode: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
        if mode in {"auto", "local:test:model"}:
            return (
                {
                    "adapter_id": "test_stream",
                    "model_id": "test-model",
                    "credential_ref": "tests:streaming_model",
                    "requested_model": mode,
                    "profile": {
                        "tool_calling": True,
                        "streaming": True,
                        "max_input_tokens": 128_000,
                        "max_output_tokens": 8_192,
                    },
                    "required_capabilities": ["streaming", "tool_calling"],
                },
                None,
            )
        return await original_model_binding(coordinator, principal_id, mode)

    monkeypatch.setattr(RunCoordinator, "_model_binding", _model_binding)

    original_local_model_binding = RunCoordinator._local_model_binding_locked

    async def _local_model_binding_locked(
        coordinator: Any,
        *,
        principal_id: str,
        connection_id: str,
        model_id: str,
        requested_model: str,
        required_capabilities: tuple[str, ...] = ("streaming", "tool_calling"),
    ) -> tuple[dict[str, Any], Any]:
        if connection_id == "test" and model_id == "model":
            return await _model_binding(coordinator, principal_id, requested_model)
        return await original_local_model_binding(
            coordinator,
            principal_id=principal_id,
            connection_id=connection_id,
            model_id=model_id,
            requested_model=requested_model,
            required_capabilities=required_capabilities,
        )

    monkeypatch.setattr(RunCoordinator, "_local_model_binding_locked", _local_model_binding_locked)

    async def _model_binding_error(
        coordinator: Any,
        principal_id: str,
        settings_snapshot: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        binding = settings_snapshot.get("_model_binding")
        if isinstance(binding, dict) and binding.get("adapter_id") == "test_stream":
            return None, None
        return await original_model_binding_error(coordinator, principal_id, settings_snapshot)

    monkeypatch.setattr(RunCoordinator, "_model_binding_error", _model_binding_error)


@pytest.fixture(autouse=True)
def _isolate_skills_disk_scan_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep agent tests from inheriting the developer's real skill library."""
    monkeypatch.setenv("SHEJANE_RUNTIME_SKILLS_PATH", str(tmp_path / "empty-skills"))


@pytest.fixture(autouse=True)
def _disable_mcp_disk_scan_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force MCP discovery to skip on-disk sources in every test.

    Honored by `shejane_runtime.tools.mcp.discover_servers`: when
    SHEJANE_RUNTIME_MCP_DISCOVERY != "on", only the env var
    SHEJANE_RUNTIME_MCP_SERVERS is consulted.
    """
    monkeypatch.setenv("SHEJANE_RUNTIME_MCP_DISCOVERY", "off")


@pytest.fixture(autouse=True)
def _disable_external_tracing_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pytest hermetic even when the parent shell has LangSmith enabled."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
