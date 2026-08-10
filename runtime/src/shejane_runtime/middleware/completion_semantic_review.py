"""Bounded semantic review for P9 clarification and final candidates."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import ToolMessage

from .clarification_reviewer import (
    ClarificationReviewUnavailable,
    review_clarification_batch,
)
from .completion_evidence import (
    _assistant_text,
    _current_run_id,
    _has_current_tool_evidence,
    _latest_memory_write_failed,
    _terminal_route,
)
from .completion_reviewer import (
    CompletionReviewUnavailable,
    review_completion_candidate,
)


async def review_clarification_calls(
    state: Any,
    runtime: Any,
    *,
    max_repairs: int,
) -> dict[str, Any] | None:
    messages = list(state.get("messages") or [])
    if not messages:
        return None
    last = messages[-1]
    if getattr(last, "type", None) != "ai":
        return None
    ask_calls = [
        call
        for call in (getattr(last, "tool_calls", None) or [])
        if str(call.get("name") or "") == "user.ask"
    ]
    if not ask_calls:
        return None

    run_id = _current_run_id(runtime, messages)
    previous = state.get("clarification_review_state")
    review_state = previous if isinstance(previous, dict) else {}
    previous_decisions = (
        dict(review_state.get("decisions") or {}) if review_state.get("run_id") == run_id else {}
    )
    call_ids = [str(call.get("id") or "") for call in ask_calls]
    if all(previous_decisions.get(call_id) == "allow" for call_id in call_ids):
        return None

    context = getattr(runtime, "context", None)
    questions = [
        {
            "tool_call_id": str(call.get("id") or ""),
            "question": str((call.get("args") or {}).get("question") or ""),
            "options": list((call.get("args") or {}).get("options") or []),
        }
        for call in ask_calls
    ]
    try:
        reviewed = await review_clarification_batch(
            model=getattr(context, "clarification_model", None),
            task_goal=str(getattr(context, "task_goal", None) or ""),
            messages=messages,
            questions=questions,
            runtime_facts={
                "workspace_configured": bool(getattr(context, "workspace_root", None)),
                "attachments": list(getattr(context, "attachments", ()) or ()),
            },
        )
        source = "llm"
    except ClarificationReviewUnavailable:
        # The question UI is the safe fallback. A reviewer outage must not
        # turn optional semantic checking into a new deadlock.
        reviewed = {
            call_id: {
                "decision": "allow",
                "reason": "Reviewer unavailable; the question UI remains available.",
            }
            for call_id in call_ids
        }
        source = "fallback"

    merged_decisions = {
        **previous_decisions,
        **{call_id: value["decision"] for call_id, value in reviewed.items()},
    }
    repairs = int(review_state.get("repairs") or 0) if review_state.get("run_id") == run_id else 0
    rejected = [call_id for call_id, value in reviewed.items() if value["decision"] == "repair"]
    if not rejected or repairs >= max_repairs:
        # Bounded repair: after the one corrective loop, fail open to the
        # visible question card instead of cycling invisibly forever.
        if rejected:
            merged_decisions.update({call_id: "allow" for call_id in rejected})
        return {
            "clarification_review_state": {
                "run_id": run_id,
                "decisions": merged_decisions,
                "repairs": repairs,
                "source": source,
            }
        }

    tool_messages: list[ToolMessage] = []
    for call in getattr(last, "tool_calls", None) or []:
        call_id = str(call.get("id") or "")
        if str(call.get("name") or "") == "user.ask":
            content = (
                "Runtime P9 review found that this question is already answered by the "
                "conversation or runtime evidence. Use that evidence and continue."
            )
        else:
            content = (
                "Not executed because a sibling clarification was rejected. Reissue this "
                "tool call only if it is still needed after using the existing evidence."
            )
        tool_messages.append(
            ToolMessage(
                content=content,
                name=str(call.get("name") or "unknown"),
                tool_call_id=call_id,
                status="error",
            )
        )
    return {
        "messages": tool_messages,
        "completion_route": {
            "decision": "repair_requested",
            "reason": "unnecessary_clarification",
            "message": "The proposed question is already answered by available evidence.",
            "recoverable": True,
            "attempts": repairs + 1,
            "max_attempts": max_repairs,
            "run_id": run_id,
            "instruction": (
                "Use the existing conversation evidence to continue the current task. "
                "Do not ask the rejected question again. If a different fact is genuinely "
                "blocking, call user.ask with only that missing fact."
            ),
        },
        "clarification_review_state": {
            "run_id": run_id,
            "decisions": merged_decisions,
            "repairs": repairs + 1,
            "source": source,
            "reasons": {call_id: reviewed[call_id]["reason"] for call_id in rejected},
        },
        "jump_to": "model",
    }


async def review_final_candidate(
    state: Any,
    runtime: Any,
    deterministic: dict[str, Any],
    *,
    max_repairs: int,
) -> dict[str, Any]:
    messages = list(state.get("messages") or [])
    run_id = _current_run_id(runtime, messages)
    if _latest_memory_write_failed(messages, run_id):
        context = getattr(runtime, "context", None)
        task_goal = str(getattr(context, "task_goal", None) or "")
        final_candidate = _assistant_text(getattr(messages[-1], "content", None))
        if re.search(r"[\u3400-\u9fff]", task_goal + final_candidate):
            corrected = "这次没有保存到长期记忆。请明确告诉我要记录的完整内容。"
        else:
            corrected = (
                "This was not saved to long-term memory. "
                "Please tell me the complete fact you want me to record."
            )
        return {
            **deterministic,
            "messages": [messages[-1].model_copy(update={"content": corrected})],
            "completion_review_state": {
                "run_id": run_id,
                "attempts": 0,
                "decision": "allow",
                "source": "memory_write_receipt",
            },
        }
    if not _has_current_tool_evidence(messages, run_id):
        return deterministic

    context = getattr(runtime, "context", None)
    try:
        reviewed = await review_completion_candidate(
            model=getattr(context, "completion_model", None),
            task_goal=str(getattr(context, "task_goal", None) or ""),
            messages=messages,
            final_candidate=_assistant_text(getattr(messages[-1], "content", None)),
        )
        source = "llm"
    except CompletionReviewUnavailable:
        # Semantic review is defense in depth. Provider or parser failure
        # must not deadlock an otherwise deterministically valid run.
        return {
            **deterministic,
            "completion_review_state": {
                "run_id": run_id,
                "attempts": 0,
                "decision": "allow",
                "source": "fallback",
            },
        }

    previous = state.get("completion_review_state")
    review_state = previous if isinstance(previous, dict) else {}
    attempts = int(review_state.get("attempts") or 0) if review_state.get("run_id") == run_id else 0
    if reviewed["decision"] == "allow":
        return {
            **deterministic,
            "completion_review_state": {
                "run_id": run_id,
                "attempts": attempts,
                "decision": "allow",
                "source": source,
                "reason": reviewed["reason"],
            },
        }

    if attempts >= max_repairs:
        blocked = _terminal_route(
            "blocked",
            "completion_review_failed",
            "The final answer still omitted or contradicted required task evidence.",
            recoverable=True,
            run_id=run_id,
        )
        blocked["completion_review_state"] = {
            "run_id": run_id,
            "attempts": attempts,
            "decision": "repair",
            "source": source,
            "reason": reviewed["reason"],
        }
        return blocked

    attempt = attempts + 1
    return {
        "completion_route": {
            "decision": "repair_requested",
            "reason": "completion_review_failed",
            "message": "The final candidate did not preserve required task evidence.",
            "recoverable": True,
            "attempts": attempt,
            "max_attempts": max_repairs,
            "run_id": run_id,
            "instruction": (
                "Re-read the current task goal and the latest completed ToolMessages. "
                "Produce one corrected final answer that includes every explicitly "
                "requested result, exact value, and selected user answer. Do not repeat "
                "successful tools unless required evidence is genuinely absent."
            ),
        },
        "completion_review_state": {
            "run_id": run_id,
            "attempts": attempt,
            "decision": "repair",
            "source": source,
            "reason": reviewed["reason"],
        },
        "jump_to": "model",
    }
