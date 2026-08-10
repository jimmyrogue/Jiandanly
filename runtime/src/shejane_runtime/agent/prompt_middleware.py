from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from ..middleware.completion_router import completion_repair_instruction
from ..middleware.tool_visibility import delivered_plugin_tool_name
from .context_builder import RuntimeContext, build_default_context


class RuntimePromptMiddleware(AgentMiddleware):
    """Append model-visible instructions from the invocation context."""

    @staticmethod
    def _request_with_context(request: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None)
        if not isinstance(context, RuntimeContext):
            return request
        prompt = build_default_context(context)
        repair_instruction = completion_repair_instruction(
            getattr(request, "state", {}),
            run_id=context.run_id,
        )
        if repair_instruction:
            prompt = f"{prompt}\n\n<runtime-repair>\n{repair_instruction}\n</runtime-repair>"
        artifact_instruction = _plugin_artifact_delivery_instruction(
            getattr(request, "messages", ())
        )
        if artifact_instruction:
            prompt = (
                f"{prompt}\n\n<runtime-artifact-delivery>\n"
                f"{artifact_instruction}\n</runtime-artifact-delivery>"
            )
        system_message = request.system_message
        return request.override(
            system_message=SystemMessage(
                content=[
                    {"type": "text", "text": prompt},
                    *system_message.content_blocks,
                ]
            )
        )

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._request_with_context(request))

    async def awrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await handler(self._request_with_context(request))


def _plugin_artifact_delivery_instruction(messages: Sequence[Any]) -> str | None:
    if delivered_plugin_tool_name(messages) is None:
        return None
    return (
        "The latest plugin Action succeeded. Runtime already persisted its artifacts "
        "and made them available to the user; each artifact_id is a delivered output, "
        "not a host filesystem path. If these artifacts satisfy the request, reply "
        "briefly and stop. Do not read the original attachment, search the filesystem, "
        "call execute or task, or repeat the Action merely to locate or return them. "
        "Call another compatible plugin Action only when the user requested an additional "
        "transformation, passing artifact_id as input_id."
    )
