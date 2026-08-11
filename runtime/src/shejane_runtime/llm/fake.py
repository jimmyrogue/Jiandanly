"""FakeBackendChatModel — a deterministic, network-free chat model.

Gated by SHEJANE_FAKE_LLM (config.fake_llm). It provides deterministic E2E
scenarios so the real agent, tool, pause/resume and SSE paths can be tested
without a live provider. NEVER enabled in production.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

FAKE_REPLY = "Fake runtime reply for the SSE contract test."
_FAILED_ONCE_SCENARIOS: set[str] = set()


class FakeBackendChatModel(BaseChatModel):
    """Streams deterministic replies and tool calls without network access."""

    @property
    def _llm_type(self) -> str:
        return "shejane-fake"

    def bind_tools(self, tools: Any = None, **kwargs: Any) -> BaseChatModel:
        # Binding is a no-op; deterministic tool calls are produced by
        # _response while the real compiled agent executes the tools.
        return self

    def _pieces(self, text: str) -> list[str]:
        words = text.split(" ")
        return [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]

    def _response(self, messages: list[BaseMessage]) -> AIMessage:
        prompt = "\n".join(str(message.content) for message in messages)
        if "conversation title generator" in prompt:
            return AIMessage(content="State round trip")
        if "P9 final-answer reviewer" in prompt:
            payload = _last_json_object(messages)
            task_goal = str(payload.get("task_goal") or "")
            candidate = str(payload.get("final_candidate") or "")
            needs_repair = (
                "[[e2e:completion-repair]]" in task_goal
                and "E2E_COMPLETION_REPAIRED" not in candidate
            )
            return AIMessage(
                content=json.dumps(
                    {
                        "decision": "repair" if needs_repair else "allow",
                        "reason": (
                            "The deterministic candidate omitted the required exact token."
                            if needs_repair
                            else "The deterministic E2E candidate preserves the requested result."
                        ),
                    }
                )
            )
        if "P9 clarification necessity reviewer" in prompt:
            payload = _last_json_object(messages)
            proposed = payload.get("proposed_questions") if isinstance(payload, dict) else []
            task_goal = str(payload.get("task_goal") or "") if isinstance(payload, dict) else ""
            decision = "repair" if "[[e2e:unnecessary-ask]]" in task_goal else "allow"
            return AIMessage(
                content=json.dumps(
                    {
                        "decisions": [
                            {
                                "tool_call_id": str(item.get("tool_call_id") or ""),
                                "decision": decision,
                                "reason": (
                                    "The requested content is already present in the conversation."
                                    if decision == "repair"
                                    else "The requested input is not present in the conversation."
                                ),
                            }
                            for item in proposed or []
                            if isinstance(item, dict)
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        if "[[e2e:unnecessary-ask]]" in prompt:
            result = _last_tool_result(messages, "user.ask")
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_unnecessary_question",
                            "name": "user.ask",
                            "args": {
                                "question": "What content should I use?",
                                "options": [],
                            },
                        }
                    ],
                )
            return AIMessage(content="E2E unnecessary clarification repaired.")
        if "[[e2e:completion-repair]]" in prompt:
            result = _last_tool_result(
                messages,
                "time.now",
                tool_call_id="call_e2e_completion_review_time",
            )
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_completion_review_time",
                            "name": "time.now",
                            "args": {},
                        }
                    ],
                )
            if "<runtime-repair>" in prompt:
                return AIMessage(content="E2E_COMPLETION_REPAIRED")
            return AIMessage(content="Done.")
        if "[[e2e:skill]]" in prompt:
            skill_token = _e2e_skill_token(prompt)
            if skill_token is not None:
                return AIMessage(content=f"E2E Skill result: {skill_token}")
            skill_path = _e2e_skill_path(prompt)
            if skill_path is None:
                return AIMessage(content="E2E Skill was not exposed to the model.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_e2e_skill_read",
                        "name": "read_file",
                        "args": {"file_path": skill_path},
                    }
                ],
            )
        if "[[e2e:skill-drift]]" in prompt:
            result = _last_tool_result(
                messages,
                "execute",
                tool_call_id="call_e2e_skill_drift_pause",
            )
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_skill_drift_pause",
                            "name": "execute",
                            "args": {"command": "printf skill-drift-pause"},
                        }
                    ],
                )
            return AIMessage(content="E2E Skill drift pause unexpectedly resumed.")
        if "[[e2e:settings-freeze]]" in prompt:
            pause_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_settings_freeze_pause",
            )
            if pause_result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_settings_freeze_pause",
                            "name": "write_file",
                            "args": {
                                "file_path": "settings-freeze.txt",
                                "content": "settings frozen at admission",
                            },
                        }
                    ],
                )
            task_result = _last_tool_result(
                messages,
                "task",
                tool_call_id="call_e2e_settings_freeze_subagent",
            )
            if task_result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_settings_freeze_subagent",
                            "name": "task",
                            "args": {
                                "description": (
                                    "[[e2e:subagent-child]] Return the exact E2E result token."
                                ),
                                "subagent_type": "writer",
                            },
                        }
                    ],
                )
            return AIMessage(content=f"E2E frozen settings retained: {task_result.content}")
        if "[[e2e:subagent-rolling]]" in prompt:
            first_batch = [
                _last_tool_result(
                    messages,
                    "task",
                    tool_call_id=f"call_e2e_rolling_subagent_{index}",
                )
                for index in range(5)
            ]
            if any(result is None for result in first_batch):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"call_e2e_rolling_subagent_{index}",
                            "name": "task",
                            "args": {
                                "description": (
                                    f"[[e2e:rolling-child:{index}]] Return child {index}."
                                ),
                                "subagent_type": "writer",
                            },
                        }
                        for index in range(5)
                    ],
                )
            replacement = _last_tool_result(
                messages,
                "task",
                tool_call_id="call_e2e_rolling_subagent_5",
            )
            if replacement is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_rolling_subagent_5",
                            "name": "task",
                            "args": {
                                "description": (
                                    "[[e2e:rolling-child:5]] Return the rolling replacement."
                                ),
                                "subagent_type": "writer",
                            },
                        }
                    ],
                )
            return AIMessage(content="E2E rolling subagent batches completed: 6/6.")
        if "[[e2e:rolling-child:" in prompt:
            return AIMessage(content="E2E rolling child complete.")
        if "[[e2e:subagent-child]]" in prompt:
            return AIMessage(content="E2E_SUBAGENT_RESULT")
        if "[[e2e:subagent]]" in prompt:
            result = _last_tool_result(
                messages,
                "task",
                tool_call_id="call_e2e_subagent",
            )
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_subagent",
                            "name": "task",
                            "args": {
                                "description": (
                                    "[[e2e:subagent-child]] Return the exact E2E result token."
                                ),
                                "subagent_type": "writer",
                            },
                        }
                    ],
                )
            return AIMessage(content=f"E2E parent received: {result.content}")
        if "[[e2e:write-todos]]" in prompt:
            result = _last_tool_result(
                messages,
                "write_todos",
                tool_call_id="call_e2e_write_todos",
            )
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_write_todos",
                            "name": "write_todos",
                            "args": {
                                "todos": [
                                    {
                                        "content": "E2E_TODO_ACTIVE",
                                        "status": "in_progress",
                                    }
                                ]
                            },
                        }
                    ],
                )
            return AIMessage(content=f"E2E Todo result: {result.content}")
        if "[[e2e:mixed-batch]]" in prompt:
            read_result = _last_tool_result(
                messages,
                "read_file",
                tool_call_id="call_e2e_batch_read",
            )
            write_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_batch_write",
            )
            if read_result is not None and write_result is not None:
                return AIMessage(content="E2E mixed Tool batch completed.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_e2e_batch_read",
                        "name": "read_file",
                        "args": {"file_path": "/source.txt"},
                    },
                    {
                        "id": "call_e2e_batch_write",
                        "name": "write_file",
                        "args": {"file_path": "/batch.txt", "content": "batch output"},
                    },
                ],
            )
        if "[[e2e:multi-write-batch]]" in prompt:
            first_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_multi_write_first",
            )
            second_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_multi_write_second",
            )
            if first_result is not None and second_result is not None:
                return AIMessage(content="E2E multi-permission Tool batch resolved.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_e2e_multi_write_first",
                        "name": "write_file",
                        "args": {"file_path": "/first.txt", "content": "first approved"},
                    },
                    {
                        "id": "call_e2e_multi_write_second",
                        "name": "write_file",
                        "args": {"file_path": "/second.txt", "content": "second denied"},
                    },
                ],
            )
        if "[[e2e:conflicting-write-batch]]" in prompt:
            first_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_conflicting_write_first",
            )
            second_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_conflicting_write_second",
            )
            if first_result is not None and second_result is not None:
                return AIMessage(content="E2E conflicting Tool batch resolved in order.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_e2e_conflicting_write_first",
                        "name": "write_file",
                        "args": {
                            "file_path": "/conflict.txt",
                            "content": "first write wins",
                        },
                    },
                    {
                        "id": "call_e2e_conflicting_write_second",
                        "name": "write_file",
                        "args": {
                            "file_path": "/conflict.txt",
                            "content": "second write must not overwrite",
                        },
                    },
                ],
            )
        if "[[e2e:run-scope-grant]]" in prompt:
            first_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_run_grant_first",
            )
            second_result = _last_tool_result(
                messages,
                "write_file",
                tool_call_id="call_e2e_run_grant_second",
            )
            if first_result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_run_grant_first",
                            "name": "write_file",
                            "args": {"file_path": "/run-grant-a.txt", "content": "a"},
                        }
                    ],
                )
            if second_result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_run_grant_second",
                            "name": "write_file",
                            "args": {"file_path": "/run-grant-b.txt", "content": "b"},
                        }
                    ],
                )
            return AIMessage(content="E2E run-scoped grant reused exactly once.")
        if "Please remember that E2E memory fact." in prompt:
            result = _last_tool_result(messages, "memory.write")
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_memory_write",
                            "name": "memory.write",
                            "args": {"fact": "E2E memory fact."},
                        }
                    ],
                )
            return AIMessage(content=f"E2E memory result: {result.content}")
        if "[[e2e:presentation]]" in prompt:
            results = [
                _last_tool_result(messages, name, tool_call_id=tool_call_id)
                for name, tool_call_id in (
                    ("time.now", "call_e2e_presentation_time_first"),
                    ("time.now", "call_e2e_presentation_time_second"),
                    ("web.fetch", "call_e2e_presentation_web"),
                )
            ]
            if all(result is not None for result in results):
                return AIMessage(content="E2E presentation tools complete.")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_e2e_presentation_time_first",
                        "name": "time.now",
                        "args": {},
                    },
                    {
                        "id": "call_e2e_presentation_time_second",
                        "name": "time.now",
                        "args": {},
                    },
                    {
                        "id": "call_e2e_presentation_web",
                        "name": "web.fetch",
                        "args": {
                            "url": "http://127.0.0.1/private?token=must-not-appear"
                        },
                    },
                ],
            )
        tool_request = _e2e_tool_request(prompt)
        if tool_request is not None:
            name, args = tool_request
            result = _last_tool_result(
                messages,
                name,
                tool_call_id="call_e2e_generic_tool",
            )
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[{"id": "call_e2e_generic_tool", "name": name, "args": args}],
                )
            return AIMessage(content=f"E2E tool result ({name}): {result.content}")
        if "[[e2e:question-write-file]]" in prompt:
            answered = (
                "Choose a recovery option → Option B" in prompt
                or _last_tool_result(messages, "user.ask") is not None
            )
            if not answered:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_recovery_question",
                            "name": "user.ask",
                            "args": {
                                "question": "Choose a recovery option",
                                "options": ["Option A", "Option B"],
                            },
                        }
                    ],
                )
            result = _last_tool_result(messages, "write_file")
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_recovery_write_file",
                            "name": "write_file",
                            "args": {
                                "file_path": "approved.txt",
                                "content": "approved by E2E",
                            },
                        }
                    ],
                )
            return AIMessage(content="E2E approved file written.")
        if "[[e2e:write-file]]" in prompt:
            result = _last_tool_result(messages, "write_file")
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_write_file",
                            "name": "write_file",
                            "args": {
                                "file_path": "approved.txt",
                                "content": "approved by E2E",
                            },
                        }
                    ],
                )
            return AIMessage(content="E2E approved file written.")
        if "[[e2e:ask]]" in prompt:
            result = _last_tool_result(messages, "user.ask")
            if result is None:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_question",
                            "name": "user.ask",
                            "args": {
                                "question": "Choose an E2E option",
                                "options": ["Option A", "Option B"],
                            },
                        }
                    ],
                )
            return AIMessage(content=f"E2E selected: {result.content}")
        if "[[e2e:read-attachment]]" in prompt:
            result = _last_tool_result(messages, "read_file")
            if result is None:
                attachment_path = _attachment_path(prompt)
                if attachment_path is None:
                    return AIMessage(content="E2E attachment path missing from Runtime context.")
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_e2e_read_attachment",
                            "name": "read_file",
                            "args": {"file_path": attachment_path},
                        }
                    ],
                )
            extracted = (
                "E2E rental receipt"
                if "E2E rental receipt" in str(result.content)
                else "attachment text missing"
            )
            return AIMessage(content=f"E2E attachment read: {extracted}")
        if "[[e2e:burst]]" in prompt:
            return AIMessage(
                content=" ".join(f"E2E_BURST_{index:03d}_" + ("x" * 4084) for index in range(256))
            )
        if "[[e2e:external-link]]" in prompt:
            return AIMessage(content="[E2E external link](https://example.test/shejane-e2e)")
        if "[[e2e:pptx-preview]]" in prompt:
            return AIMessage(content="E2E window deck: e2e-window-deck.pptx")
        return AIMessage(content=FAKE_REPLY)

    def _chunks(self, messages: list[BaseMessage]) -> Iterator[ChatGenerationChunk]:
        response = self._response(messages)
        if response.tool_calls:
            for index, call in enumerate(response.tool_calls):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "id": call["id"],
                                "name": call["name"],
                                "args": json.dumps(call["args"]),
                                "index": index,
                            }
                        ],
                    )
                )
            return
        for piece in self._pieces(str(response.content)):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        prompt = "\n".join(str(message.content) for message in messages)
        if "[[e2e:repair]]" in prompt and "- 修复工作流:" not in prompt:
            raise ValueError("validation: E2E repairable model failure")
        fail_once = re.search(r"\[\[e2e:fail-once:([a-zA-Z0-9_-]+)\]\]", prompt)
        if fail_once is not None and fail_once.group(1) not in _FAILED_ONCE_SCENARIOS:
            _FAILED_ONCE_SCENARIOS.add(fail_once.group(1))
            raise TimeoutError("E2E transient model timeout")
        if "[[e2e:post-tool-slow]]" in prompt and any(
            getattr(message, "type", None) == "tool" for message in messages
        ):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content="E2E settling "))
            if run_manager is not None:
                await run_manager.on_llm_new_token("E2E settling ", chunk=chunk)
            yield chunk
            try:
                delay = max(0.0, float(os.environ.get("SHEJANE_E2E_SLOW_SECONDS", "30")))
            except ValueError:
                delay = 30.0
            await asyncio.sleep(delay)
            yield ChatGenerationChunk(message=AIMessageChunk(content="finished"))
            return
        if "[[e2e:reasoning-phase]]" in prompt and (
            "[[e2e:presentation]]" not in prompt
            or any(isinstance(message, ToolMessage) for message in messages)
        ):
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={
                        "reasoning_content": "E2E_PRIVATE_REASONING_MUST_NOT_RENDER"
                    },
                )
            )
            await asyncio.sleep(2)
            final_text = (
                "E2E reasoning presentation complete."
                if "[[e2e:presentation]]" in prompt
                else "E2E reasoning phase complete."
            )
            yield ChatGenerationChunk(message=AIMessageChunk(content=final_text))
            return
        if "[[e2e:slow]]" in prompt:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content="E2E working "))
            if run_manager is not None:
                await run_manager.on_llm_new_token("E2E working ", chunk=chunk)
            yield chunk
            try:
                delay = max(0.0, float(os.environ.get("SHEJANE_E2E_SLOW_SECONDS", "30")))
            except ValueError:
                delay = 30.0
            await asyncio.sleep(delay)
            yield ChatGenerationChunk(message=AIMessageChunk(content="finished"))
            return
        if "[[e2e:scroll]]" in prompt:
            # A long reply streamed token-by-token with a small pacing delay:
            # deterministic mid-stream scroll window (~2.4s) for the
            # scroll-to-bottom E2E, with enough content to overflow the
            # chat viewport (~160 rendered lines).
            lines = " ".join(f"Scroll_{index:03d}_" + ("x" * 56) for index in range(160))
            for piece in self._pieces(lines):
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
                if run_manager is not None and piece:
                    await run_manager.on_llm_new_token(piece, chunk=chunk)
                yield chunk
                await asyncio.sleep(0.015)
            return
        for chunk in self._chunks(messages):
            if run_manager is not None and chunk.message.content:
                await run_manager.on_llm_new_token(str(chunk.message.content), chunk=chunk)
            yield chunk

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield from self._chunks(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._response(messages))])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._response(messages))])


def _last_tool_result(
    messages: list[BaseMessage],
    name: str,
    *,
    tool_call_id: str | None = None,
) -> BaseMessage | None:
    provider_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "tool"
    return next(
        (
            message
            for message in reversed(messages)
            if getattr(message, "type", None) == "tool"
            and (
                getattr(message, "tool_call_id", None) == tool_call_id
                if tool_call_id is not None
                else getattr(message, "name", None) in {name, provider_name}
            )
        ),
        None,
    )


def _last_json_object(messages: list[BaseMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _attachment_path(prompt: str) -> str | None:
    match = re.search(
        r"本次附件（只读）:[^\n]*`(/attachments/[^`\n]+)`",
        prompt,
    )
    return match.group(1) if match else None


def _e2e_skill_path(prompt: str) -> str | None:
    match = re.search(r"Read `([^`\n]*/e2e-active-skill/SKILL\.md)`", prompt)
    return match.group(1) if match else None


def _e2e_skill_token(prompt: str) -> str | None:
    match = re.search(r"E2E_SKILL_(?:ACTIVE|VERSION_(?:ONE|TWO|THREE))", prompt)
    return match.group(0) if match else None


def _e2e_tool_request(prompt: str) -> tuple[str, dict[str, Any]] | None:
    match = re.search(r"\[\[e2e:tool:([A-Za-z0-9_-]+)\]\]", prompt)
    if match is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(match.group(1) + "=="))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        return None
    args = payload.get("args")
    return payload["name"], args if isinstance(args, dict) else {}
