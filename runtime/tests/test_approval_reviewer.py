from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage


class FakeApprovalModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[Any] = []
        self.kwargs: dict[str, Any] = {}

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self.messages = messages
        self.kwargs = kwargs
        return AIMessage(content=self.content)


@pytest.mark.asyncio
async def test_reviewer_returns_one_bounded_decision_per_operation() -> None:
    from shejane_runtime.middleware.approval_reviewer import review_approval_batch

    model = FakeApprovalModel(
        '{"decisions":[{"operation_id":"op-1","decision":"allow","reason":"requested test"}]}'
    )

    decisions = await review_approval_batch(
        model=model,
        task_goal="Run the test command",
        actions=[
            {
                "operation_id": "op-1",
                "tool_name": "execute",
                "risk": "external_or_unknown",
                "arguments": {"command": "make test", "api_key": "secret-value"},
            }
        ],
        timeout_seconds=1,
    )

    assert decisions == {"op-1": {"decision": "allow", "reason": "requested test"}}
    assert "secret-value" not in str(model.messages)
    reviewer_prompt = str(model.messages[0].content)
    assert "goal-relevant read-only" in reviewer_prompt
    assert "Do not ask merely because an action accesses the public internet" in reviewer_prompt


@pytest.mark.asyncio
async def test_reviewer_requests_strict_json_schema_when_provider_supports_it() -> None:
    from shejane_runtime.middleware.approval_reviewer import review_approval_batch

    model = FakeApprovalModel(
        '{"decisions":[{"operation_id":"op-1","decision":"allow","reason":"safe"}]}'
    )
    model.supports_json_schema_output = True

    await review_approval_batch(
        model=model,
        task_goal="Inspect the file",
        actions=[{"operation_id": "op-1", "tool_name": "read", "arguments": {}}],
        timeout_seconds=1,
    )

    response_format = model.kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_reviewer_rejects_incomplete_or_unsupported_decisions() -> None:
    from shejane_runtime.middleware.approval_reviewer import (
        ApprovalReviewUnavailable,
        review_approval_batch,
    )

    model = FakeApprovalModel(
        '{"decisions":[{"operation_id":"other","decision":"deny","reason":"no"}]}'
    )

    with pytest.raises(ApprovalReviewUnavailable):
        await review_approval_batch(
            model=model,
            task_goal="Run tests",
            actions=[
                {
                    "operation_id": "op-1",
                    "tool_name": "execute",
                    "risk": "external_or_unknown",
                    "arguments": {"command": "make test"},
                }
            ],
            timeout_seconds=1,
        )
