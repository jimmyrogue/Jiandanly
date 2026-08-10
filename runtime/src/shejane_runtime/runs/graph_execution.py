"""Durable LangGraph stream cycle and terminal snapshot projection."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk

from ..agent.context_builder import RuntimeContext
from ..event_translator import translate
from ..llm.runtime import bind_runtime_model
from ..tools.runtime import bind_runtime_tools
from .assistant_projection import (
    _assistant_draft_from_state,
    _assistant_draft_from_update,
    _assistant_round_from_update,
)
from .errors import ExecutionSettlementError, RunOutcome
from .failure_projection import _completion_failure_payload, _repair_workflow_payload
from .inputs import _generate_conversation_title
from .interrupts import build_waiting_handoff, handle_run_interrupt
from .stream_state import (
    _checkpoint_id_from_stream,
    _task_interrupts,
    _waiting_status_for_interrupts,
)

log = logging.getLogger("shejane_runtime.runs")


@dataclass(slots=True)
class GraphStreamExecution:
    coordinator: Any
    agent: Any
    runtime_context: RuntimeContext
    config: dict[str, Any]
    input_payload: Any
    run_id: str
    graph_thread_id: str
    checkpoint_id: str | None
    model_binding: Any
    repair_context: dict[str, Any] | None
    thread_title_seed: str | None
    goal: str
    wakeup: asyncio.Event

    async def run(self) -> RunOutcome:
        latest_checkpoint = await self._consume_stream()
        if self.checkpoint_id is None:
            raise RuntimeError("graph execution produced no checkpoint")
        if latest_checkpoint is None:
            raise RuntimeError("graph execution produced no checkpoint payload")
        self.config = {
            **self.config,
            "configurable": {
                **self.config["configurable"],
                "checkpoint_id": self.checkpoint_id,
            },
        }
        # v2 checkpoint parts precede pending interrupt writes. Reading the
        # public state at this exact branch head folds those writes into tasks.
        snapshot = await self.agent.aget_state(self.config)
        next_nodes = list(snapshot.next)
        if not next_nodes:
            return await self._completed_outcome(snapshot)
        return await self._waiting_outcome(snapshot, next_nodes)

    async def _consume_stream(self) -> dict[str, Any] | None:
        coordinator = self.coordinator
        latest_checkpoint: dict[str, Any] | None = None
        if self.runtime_context.model is None:
            raise RuntimeError("agent model is not bound")
        with (
            bind_runtime_model(self.runtime_context.model),  # type: ignore[arg-type]
            bind_runtime_tools(self.runtime_context.dynamic_tools),  # type: ignore[arg-type]
        ):
            active_model_round: tuple[object, object] | None = None
            active_model_call_id: str | None = None
            async for part in self.agent.astream(
                self.input_payload,
                config=self.config,
                context=self.runtime_context,
                stream_mode=["updates", "messages", "custom", "checkpoints"],
                durability="sync",
                version="v2",
            ):
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                payload = part.get("data")
                if not isinstance(kind, str):
                    continue
                if kind == "messages" and isinstance(payload, tuple) and len(payload) == 2:
                    chunk, metadata = payload
                    if isinstance(chunk, AIMessageChunk):
                        if (
                            not isinstance(metadata, dict)
                            or metadata.get("langgraph_node") != "model"
                            or part.get("ns")
                        ):
                            continue
                        chunk_round_id = str(
                            chunk.additional_kwargs.get("runtime_model_call_id") or ""
                        )
                        if chunk_round_id:
                            active_model_call_id = chunk_round_id
                        model_round = (
                            metadata.get("langgraph_checkpoint_ns"),
                            metadata.get("langgraph_step"),
                        )
                        if model_round != active_model_round:
                            await coordinator._enqueue(
                                self.wakeup,
                                self.run_id,
                                "llm.round.started",
                                {"round_id": active_model_call_id},
                            )
                            active_model_round = model_round
                if kind == "checkpoints":
                    checkpoint_id = _checkpoint_id_from_stream(payload)
                    if checkpoint_id is not None:
                        await coordinator.store.advance_graph_checkpoint(
                            self.run_id,
                            graph_thread_id=self.graph_thread_id,
                            expected_checkpoint_id=self.checkpoint_id,
                            checkpoint_id=checkpoint_id,
                        )
                        self.checkpoint_id = checkpoint_id
                        latest_checkpoint = payload
                    continue
                if kind == "updates" and not part.get("ns"):
                    await self._project_assistant_update(payload)
                if part.get("ns"):
                    continue
                for translated in translate(kind, payload):
                    data = (
                        translated["data"]
                        if isinstance(translated["data"], dict)
                        else {"value": translated["data"]}
                    )
                    if translated["event"].startswith("llm.") and active_model_call_id:
                        data.setdefault("round_id", active_model_call_id)
                    await coordinator._enqueue(
                        self.wakeup,
                        self.run_id,
                        translated["event"],
                        data,
                    )
        return latest_checkpoint

    async def _project_assistant_update(self, payload: Any) -> None:
        coordinator = self.coordinator
        draft = _assistant_draft_from_update(payload)
        if draft is not None:
            await coordinator.store.update_assistant_draft(run_id=self.run_id, **draft)
        assistant_round = _assistant_round_from_update(
            payload,
            allow_reasoning_summary=bool(
                isinstance(self.model_binding, dict)
                and self.model_binding.get("display_reasoning_summary") is True
            ),
        )
        if assistant_round is None:
            return
        round_event, round_created = await coordinator.store.commit_assistant_round(
            self.run_id,
            assistant_round,
        )
        if round_created:
            coordinator._trace_stream_event(
                coordinator._event_stream.stored_event_envelope(round_event)
            )
        committed_item_ids = []
        if str(assistant_round.get("reasoning_summary") or "").strip():
            committed_item_ids.append(f"round:{assistant_round['round_id']}:reasoning")
        if str(assistant_round.get("text") or "").strip():
            committed_item_ids.append(f"round:{assistant_round['round_id']}:progress")
        await coordinator._enqueue(
            self.wakeup,
            self.run_id,
            "llm.round.closed",
            {
                "round_id": assistant_round["round_id"],
                "committed_item_ids": committed_item_ids,
            },
        )

    async def _completed_outcome(self, snapshot: Any) -> RunOutcome:
        coordinator = self.coordinator
        completion_failure = _completion_failure_payload(
            snapshot.values,
            current_run_id=self.run_id,
        )
        if completion_failure is not None:
            if self.repair_context is not None:
                await coordinator._enqueue(
                    self.wakeup,
                    self.run_id,
                    "repair.workflow",
                    _repair_workflow_payload(
                        self.repair_context,
                        status="failed",
                        reason=str(completion_failure["error"]),
                    ),
                )
            return RunOutcome("failed", "run.failed", completion_failure)

        final_draft = _assistant_draft_from_state(snapshot.values, run_id=self.run_id)
        if final_draft is not None:
            await coordinator.store.update_assistant_draft(run_id=self.run_id, **final_draft)
        draft = await coordinator.store.get_assistant_draft(self.run_id)
        if draft is None:
            raise ExecutionSettlementError("final assistant draft is missing")
        if self.repair_context is not None:
            await coordinator._enqueue(
                self.wakeup,
                self.run_id,
                "repair.workflow",
                _repair_workflow_payload(self.repair_context, status="completed"),
            )
        result_payload: dict[str, Any] = {}
        if self.thread_title_seed:
            try:
                generated_title = await _generate_conversation_title(
                    getattr(self.runtime_context, "title_model", None),
                    user_input=coordinator._user_inputs.get(self.run_id, self.goal),
                    assistant_answer=str(draft.get("content") or ""),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "run %s title generation failed: %s",
                    self.run_id,
                    type(exc).__name__,
                )
            else:
                if generated_title:
                    result_payload = {
                        "thread_title": generated_title,
                        "thread_title_seed": self.thread_title_seed,
                    }
        return RunOutcome("completed", "run.completed", result_payload)

    async def _waiting_outcome(self, snapshot: Any, next_nodes: list[str]) -> RunOutcome:
        coordinator = self.coordinator
        interrupts_top = list(getattr(snapshot, "interrupts", ()) or ())
        interrupts_per_task = [
            interrupt for task in (snapshot.tasks or ()) for interrupt in _task_interrupts(task)
        ]
        seen_ids: set[Any] = set()
        interrupts: list[Any] = []
        for interrupt in interrupts_top + interrupts_per_task:
            key = getattr(interrupt, "id", None)
            if key is None:
                interrupts.append(interrupt)
            elif key not in seen_ids:
                seen_ids.add(key)
                interrupts.append(interrupt)
        interrupt_ids = [
            str(getattr(interrupt, "id", None) or f"anonymous-{index}")
            for index, interrupt in enumerate(interrupts)
        ]
        wait_cycle_id = (
            "wait_"
            + hashlib.sha256(
                f"{self.run_id}\0{self.checkpoint_id}\0".encode()
                + "\0".join(interrupt_ids).encode()
            ).hexdigest()[:32]
        )
        for interrupt in interrupts:
            await handle_run_interrupt(
                coordinator.store,
                coordinator._enqueue,
                self.wakeup,
                self.run_id,
                interrupt,
                wait_cycle_id=wait_cycle_id,
            )
        return RunOutcome(
            status=_waiting_status_for_interrupts(interrupts),
            event_type="run.waiting",
            payload={
                "next": next_nodes,
                "wait_cycle_id": wait_cycle_id,
                "interrupts": [
                    {"value": getattr(item, "value", None), "id": getattr(item, "id", None)}
                    for item in interrupts
                ],
                "handoff": await build_waiting_handoff(coordinator.store, self.run_id),
            },
        )
