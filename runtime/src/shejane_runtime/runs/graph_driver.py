"""P7 LangGraph execution driver for one leased Run attempt."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.types import Command

from ..agent.builder import build_agent
from ..agent.context_builder import RuntimeContext
from ..event_translator import translate
from ..llm.errors import ModelServiceError
from ..llm.runtime import bind_runtime_model
from ..observability import build_callbacks
from ..plugins.identity import plugin_action_tool_version
from ..run_configuration import _apply_advanced_overrides, _execution_policy_snapshot
from ..tools.memory import extract_memory_write_facts
from ..tools.runtime import bind_runtime_tools
from .assistant_projection import (
    _assistant_draft_from_state,
    _assistant_draft_from_update,
    _assistant_round_from_update,
)
from .errors import ExecutionIdentityError, ExecutionSettlementError, RunOutcome
from .failure_projection import (
    _completion_failure_payload,
    _repair_context_from_metadata,
    _repair_context_rejected,
    _repair_rejected_failure_payload,
    _repair_workflow_payload,
    _retry_context_from_metadata,
    _run_failed_payload,
)
from .inputs import _generate_conversation_title, _plugin_input_snapshots
from .interrupts import build_waiting_handoff, handle_run_interrupt
from .stream_state import (
    _checkpoint_id_from_stream,
    _task_interrupts,
    _waiting_status_for_interrupts,
)

log = logging.getLogger("shejane_runtime.runs")


class RunGraphDriverMixin:
    async def _drive_run(
        self,
        *,
        run_id: str,
        principal_id: str,
        resume_payload: dict[str, Any] | None,
        mode: str,
        graph_thread_id: str,
        graph_checkpoint_id: str | None,
        graph_input_kind: str,
        execution_attempt_id: str,
        checkpointer: Any | None = None,
        model_api_key: str | None = None,
        resource_stack: AsyncExitStack | None = None,
    ) -> RunOutcome:
        wakeup = self._wakeups[run_id]
        workspace_path = self._workspaces.get(run_id)
        attachment_bindings = self._attachments.get(run_id, [])
        goal = self._goals.get(run_id, "")
        repair_context: dict[str, Any] | None = None
        retry_context: dict[str, Any] | None = None

        try:
            settings = self.settings
            run_metadata = self._run_metadata.get(run_id) or {}
            run_record = await self.store.get_run(run_id)
            if run_record is None:
                raise ExecutionIdentityError(f"run {run_id} disappeared before execution")
            run_kind = str(run_record.get("run_kind") or "turn")
            root_run_id = str(run_record.get("root_run_id") or run_id)
            agent_definition_id = str(run_record.get("agent_definition_id") or "shejane.default")
            agent_definition_version = str(run_record.get("agent_definition_version") or "1")
            collaboration_depth = int(run_record.get("collaboration_depth") or 0)
            child_definition: dict[str, Any] | None = None
            if run_kind == "child":
                candidate = run_metadata.get("_child_agent_definition")
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("id") != agent_definition_id
                    or candidate.get("version") != agent_definition_version
                    or not isinstance(candidate.get("system_prompt"), str)
                    or not isinstance(candidate.get("allowed_tools"), list)
                    or not all(isinstance(name, str) for name in candidate["allowed_tools"])
                ):
                    raise ExecutionIdentityError(
                        f"child run {run_id} has an incompatible frozen Agent definition"
                    )
                child_definition = candidate
            repair_context = _repair_context_from_metadata(
                run_metadata,
                max_attempts=settings.repair_workflow_max,
            )
            retry_context = _retry_context_from_metadata(run_metadata)
            recovery_context = retry_context or repair_context
            answered_questions = []
            source_run_id = (recovery_context or {}).get("source_run_id")
            if source_run_id:
                answered_questions = await self.store.list_answered_question_choices_for_run(
                    principal_id=principal_id,
                    run_id=str(source_run_id),
                )
            clarification_count = await self.store.count_questions_for_run(run_id)

            # Mark the run as started FIRST — before model resolution and the
            # (slow) agent build. The client treats run.started as "the runtime
            # accepted this run"; emitting it late opened a window where a
            # quick cancel produced a stream with run.canceled but no
            # run.started (flaked test_cancel_midflight on slow CI runners).
            if resume_payload is None:
                await self._enqueue(wakeup, run_id, "run.started", {"goal": goal})

                if repair_context is not None:
                    if _repair_context_rejected(repair_context):
                        await self._enqueue(
                            wakeup,
                            run_id,
                            "repair.workflow",
                            _repair_workflow_payload(
                                repair_context,
                                status="rejected",
                                reason="repair attempt limit exceeded",
                            ),
                        )
                        return RunOutcome(
                            status="failed",
                            event_type="run.failed",
                            payload=_repair_rejected_failure_payload(repair_context),
                        )
                    await self._enqueue(
                        wakeup,
                        run_id,
                        "repair.workflow",
                        _repair_workflow_payload(repair_context, status="started"),
                    )

            resolved_model = mode
            run_settings = self._settings_overrides.get(run_id) or {}
            model_binding = run_settings.get("_model_binding")

            self._modes[run_id] = resolved_model

            # Per-run effective settings = base runtime settings with any
            # "Advanced" knobs the client sent folded on top.
            effective_settings = _apply_advanced_overrides(settings, run_settings)
            execution_policy = _execution_policy_snapshot(goal, run_settings)

            # The ingress schema and 1 MiB request limit are the safety boundary.
            # Context compaction belongs to Deep Agents' token-aware
            # SummarizationMiddleware; do not apply a second message-count policy
            # or manufacture a heuristic summary here.
            history = self._histories.get(run_id, [])
            full_messages: list[dict[str, str]] = [
                {"role": str(item.get("role", "user")), "content": str(item.get("content", ""))}
                for item in history
                if item.get("content")
            ]
            # +1 for the current user goal that gets appended below.
            turn_count = len(full_messages) + 1
            thread_title_seed = (
                await self.store.run_initial_thread_title_seed(run_id)
                if not full_messages
                else None
            )

            # Defaults: memory + skills + mcp all ON. The client's
            # agent settings panel has them enabled by default; legacy
            # callers (curl, tests) that don't send any settings
            # inherit the same default. Only an explicit "off" disables.
            memory_enabled = str(run_settings.get("memory", "on")).lower() != "off"
            skills_enabled = str(run_settings.get("skills", "on")).lower() != "off"
            mcp_enabled = str(run_settings.get("mcp", "on")).lower() != "off"
            # Code execution defaults ON now (since v7 of the client
            # storage, ~2026-05-26). The original opt-in toggle was
            # Per-server opt-out from the MCP tab. The client persists
            # a list of names the user disabled and ships it on every
            # run. Defensive coercion: drop non-strings and dedupe so
            # a buggy renderer can't crash the loop.
            raw_disabled = run_settings.get("mcp_disabled") or []
            mcp_disabled_servers: set[str] = {
                str(name) for name in raw_disabled if isinstance(name, str)
            }

            async def emit_steering_event(event_type: str, payload: dict[str, Any]) -> None:
                await self._enqueue(wakeup, run_id, event_type, payload)

            runtime_context = RuntimeContext(
                run_id=run_id,
                principal_id=principal_id,
                store=self.store,
                steering_emit=emit_steering_event,
                child_run_control=self.child_run_control(),
                agent_mailbox_control=self.agent_mailbox_control(),
                memory_enabled=memory_enabled,
                memory_write_facts=(
                    ()
                    if run_kind == "child"
                    else extract_memory_write_facts(
                        self._user_inputs.get(run_id, goal),
                        history=full_messages,
                    )
                ),
                execution_attempt_id=execution_attempt_id,
                workspace_root=workspace_path,
                attachments=tuple(
                    str(item.get("virtual_path"))
                    for item in attachment_bindings
                    if item.get("virtual_path")
                ),
                task_goal=goal,
                model_call_soft_limit=int(execution_policy["soft_model_call_limit"]),
                model_call_hard_limit=int(execution_policy["max_model_calls"]),
                model_call_final_reserve=int(execution_policy["final_model_call_reserve"]),
                execution_policy=dict(execution_policy),
                agent_role_prompt=(
                    str(child_definition["system_prompt"]) if child_definition is not None else None
                ),
                allowed_tool_names=(
                    tuple(str(name) for name in child_definition["allowed_tools"])
                    if child_definition is not None
                    else ()
                ),
                mode=resolved_model,
                run_kind=run_kind,
                root_run_id=root_run_id,
                agent_definition_id=agent_definition_id,
                agent_definition_version=agent_definition_version,
                collaboration_depth=collaboration_depth,
                permission_mode=str(run_settings.get("permission_mode") or "ask"),
                capability_bindings={
                    str(key): dict(value)
                    for key, value in (
                        run_settings.get("_capability_bindings", {}).items()
                        if isinstance(run_settings.get("_capability_bindings"), dict)
                        else ()
                    )
                    if isinstance(value, dict)
                },
                required_tools=tuple(
                    str(value)
                    for value in (
                        run_settings.get("_required_tools", [])
                        if isinstance(run_settings.get("_required_tools"), list)
                        else []
                    )
                ),
                turn_count=turn_count,
                clarification_count=clarification_count,
                repair_intent=bool(repair_context),
                repair_attempt=(repair_context or {}).get("attempt"),
                repair_max_attempts=(repair_context or {}).get("max_attempts"),
                repair_source_run_id=(repair_context or {}).get("source_run_id"),
                repair_source_message_id=(repair_context or {}).get("source_message_id"),
                repair_failure_category=(repair_context or {}).get("failure_category"),
                repair_failure_action_kind=(repair_context or {}).get("failure_action_kind"),
                retry_intent=bool(retry_context),
                retry_attempt=(retry_context or {}).get("attempt"),
                retry_source_run_id=(retry_context or {}).get("source_run_id"),
                retry_source_message_id=(retry_context or {}).get("source_message_id"),
                retry_failure_category=(retry_context or {}).get("failure_category"),
                retry_failure_action_kind=(retry_context or {}).get("failure_action_kind"),
                recovery_answered_questions=tuple(
                    (
                        str(item["question"]),
                        tuple(str(answer) for answer in item["answers"]),
                    )
                    for item in answered_questions
                ),
            )
            runtime_context.plugin_inputs = await _plugin_input_snapshots(
                self.store,
                run_id,
                attachment_bindings,
            )
            if resource_stack is None:
                raise RuntimeError(
                    "plugin snapshot acquisition requires an execution resource stack"
                )
            plugin_bindings = await self.store.list_run_plugin_bindings(run_id)
            plugin_lease = await resource_stack.enter_async_context(
                self.plugin_catalog.acquire_snapshot(
                    plugin_bindings,
                    execution_context=runtime_context,
                )
            )
            runtime_context.plugin_catalog_hash = plugin_lease.action_catalog_hash
            runtime_context.plugin_lease = plugin_lease
            public_inputs = [
                {key: value for key, value in item.items() if key != "source_path"}
                for item in runtime_context.plugin_inputs
            ]
            for action in plugin_lease.actions:
                action_inputs = [
                    item for item in public_inputs if item["media_type"] in action.consumes
                ]
                invocation_identity = {
                    "action": {
                        "plugin_id": action.plugin_id,
                        "plugin_version": action.plugin_version,
                        "plugin_digest": action.plugin_digest,
                        "action_id": action.action_id,
                    },
                    "inputs": action_inputs,
                    "grants": {
                        "capabilities": sorted(
                            set(action.capabilities)
                            & {
                                "input.read",
                                "artifact.write",
                                "computer.observe",
                                "computer.control",
                                "computer.setup",
                            }
                        )
                    },
                    "limits": dict(action.limits),
                    "environment": {
                        "locale": runtime_context.locale or "en-US",
                        "timezone": "UTC",
                    },
                    "model_binding": action.model_binding,
                }
                runtime_context.plugin_tool_versions[action.tool_name] = plugin_action_tool_version(
                    invocation_identity,
                    action_schema_digest=action.action_schema_digest,
                )
            agent = await build_agent(
                store=self.store,
                checkpointer=checkpointer or self.checkpointer,
                agent_store=self.agent_store,
                workspace_root=workspace_path,
                attachment_bindings=attachment_bindings,
                run_id=run_id,
                mode=resolved_model,
                task_goal=goal,
                turn_count=turn_count,
                memory_enabled=memory_enabled,
                skills_enabled=skills_enabled,
                skill_catalog_hash=(
                    str(run_settings["_skills_fingerprint"])
                    if isinstance(run_settings.get("_skills_fingerprint"), str)
                    else None
                ),
                mcp_enabled=mcp_enabled,
                mcp_disabled_servers=mcp_disabled_servers or None,
                mcp_catalog=self.mcp_catalog,
                plugin_lease=plugin_lease,
                settings=effective_settings,
                model_binding=model_binding if isinstance(model_binding, dict) else None,
                model_api_key=model_api_key,
                resource_stack=resource_stack,
                execution_attempt_id=execution_attempt_id,
                runtime_context=runtime_context,
                definition_cache=self._agent_definitions,
                definition_cache_lock=self._agent_definition_lock,
                repair_context=repair_context,
                retry_context=retry_context,
                steering_emit=emit_steering_event,
            )
            if not runtime_context.graph_definition_id:
                raise RuntimeError("agent definition id is missing")
            await self.store.bind_graph_definition(
                run_id,
                runtime_context.graph_definition_id,
            )
            config = {
                "configurable": {
                    "thread_id": graph_thread_id,
                    "checkpoint_ns": "",
                    "workspace_root": workspace_path or "",
                    "runtime_principal_id": principal_id,
                    "runtime_run_id": run_id,
                    "runtime_attempt_id": execution_attempt_id,
                    **(
                        {"checkpoint_id": graph_checkpoint_id}
                        if graph_checkpoint_id is not None
                        else {}
                    ),
                },
                "callbacks": build_callbacks(),
            }
            if resume_payload is not None:
                input_payload: Any = Command(resume=resume_payload)
                await self._enqueue(wakeup, run_id, "run.resumed", {"payload": resume_payload})
            else:
                if graph_input_kind not in {"new", "fork"}:
                    raise RuntimeError(f"unsupported graph input kind: {graph_input_kind}")
                messages = list(full_messages)
                messages.append(
                    HumanMessage(
                        content=goal,
                        additional_kwargs={
                            "runtime_kind": "task_input",
                            "runtime_run_id": run_id,
                        },
                    )
                )
                input_payload = {"messages": messages}
                # run.started + the "running" status were already emitted at
                # the top of the try block (before resolution/agent build).

            # Auto-approve loop. We may iterate multiple times if the
            # run hits successive HITL gates and every gated tool has
            # an in-run `scope=run` grant. Each iteration drains one
            # astream() cycle; on every paused state we either:
            #   • surface to the user (one-shot approval or a tool the
            #     user hasn't granted run-scope on), OR
            #   • build a synthetic Command(resume={"decisions": [...]})
            #     and loop again — making the pause invisible to the UI.
            current_checkpoint_id = graph_checkpoint_id
            while True:
                latest_checkpoint: dict[str, Any] | None = None
                if runtime_context.model is None:
                    raise RuntimeError("agent model is not bound")
                with (
                    bind_runtime_model(runtime_context.model),  # type: ignore[arg-type]
                    bind_runtime_tools(runtime_context.dynamic_tools),  # type: ignore[arg-type]
                ):
                    active_model_round: tuple[object, object] | None = None
                    active_model_call_id: str | None = None
                    async for part in agent.astream(
                        input_payload,
                        config=config,
                        context=runtime_context,
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
                                    await self._enqueue(
                                        wakeup,
                                        run_id,
                                        "llm.round.started",
                                        {"round_id": active_model_call_id},
                                    )
                                    active_model_round = model_round
                        if kind == "checkpoints":
                            checkpoint_id = _checkpoint_id_from_stream(payload)
                            if checkpoint_id is not None:
                                await self.store.advance_graph_checkpoint(
                                    run_id,
                                    graph_thread_id=graph_thread_id,
                                    expected_checkpoint_id=current_checkpoint_id,
                                    checkpoint_id=checkpoint_id,
                                )
                                current_checkpoint_id = checkpoint_id
                                latest_checkpoint = payload
                            continue
                        if kind == "updates" and not part.get("ns"):
                            draft = _assistant_draft_from_update(payload)
                            if draft is not None:
                                await self.store.update_assistant_draft(
                                    run_id=run_id,
                                    **draft,
                                )
                            assistant_round = _assistant_round_from_update(
                                payload,
                                allow_reasoning_summary=bool(
                                    isinstance(model_binding, dict)
                                    and model_binding.get("display_reasoning_summary") is True
                                ),
                            )
                            if assistant_round is not None:
                                (
                                    round_event,
                                    round_created,
                                ) = await self.store.commit_assistant_round(run_id, assistant_round)
                                if round_created:
                                    self._trace_stream_event(
                                        self._event_stream.stored_event_envelope(round_event)
                                    )
                                committed_item_ids = []
                                if str(assistant_round.get("reasoning_summary") or "").strip():
                                    committed_item_ids.append(
                                        f"round:{assistant_round['round_id']}:reasoning"
                                    )
                                if str(assistant_round.get("text") or "").strip():
                                    committed_item_ids.append(
                                        f"round:{assistant_round['round_id']}:progress"
                                    )
                                await self._enqueue(
                                    wakeup,
                                    run_id,
                                    "llm.round.closed",
                                    {
                                        "round_id": assistant_round["round_id"],
                                        "committed_item_ids": committed_item_ids,
                                    },
                                )
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
                            await self._enqueue(wakeup, run_id, translated["event"], data)

                if current_checkpoint_id is None:
                    raise RuntimeError("graph execution produced no checkpoint")
                if latest_checkpoint is None:
                    raise RuntimeError("graph execution produced no checkpoint payload")
                config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "checkpoint_id": current_checkpoint_id,
                    },
                }
                # v2 checkpoint parts are emitted before pending interrupt
                # writes are folded into tasks. The public state read at this
                # exact branch head includes them; v3 lifecycle streams can
                # replace this once that API is stable.
                snapshot = await agent.aget_state(config)
                next_nodes = list(snapshot.next)
                if not next_nodes:
                    completion_failure = _completion_failure_payload(
                        snapshot.values,
                        current_run_id=run_id,
                    )
                    if completion_failure is not None:
                        if repair_context is not None:
                            await self._enqueue(
                                wakeup,
                                run_id,
                                "repair.workflow",
                                _repair_workflow_payload(
                                    repair_context,
                                    status="failed",
                                    reason=str(completion_failure["error"]),
                                ),
                            )
                        return RunOutcome(
                            status="failed",
                            event_type="run.failed",
                            payload=completion_failure,
                        )
                    final_draft = _assistant_draft_from_state(snapshot.values, run_id=run_id)
                    if final_draft is not None:
                        await self.store.update_assistant_draft(run_id=run_id, **final_draft)
                    draft = await self.store.get_assistant_draft(run_id)
                    if draft is None:
                        raise ExecutionSettlementError("final assistant draft is missing")
                    if repair_context is not None:
                        await self._enqueue(
                            wakeup,
                            run_id,
                            "repair.workflow",
                            _repair_workflow_payload(repair_context, status="completed"),
                        )
                    result_payload: dict[str, Any] = {}
                    if thread_title_seed:
                        try:
                            generated_title = await _generate_conversation_title(
                                getattr(runtime_context, "title_model", None),
                                user_input=self._user_inputs.get(run_id, goal),
                                assistant_answer=str(draft.get("content") or ""),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning(
                                "run %s title generation failed: %s",
                                run_id,
                                type(exc).__name__,
                            )
                        else:
                            if generated_title:
                                result_payload = {
                                    "thread_title": generated_title,
                                    "thread_title_seed": thread_title_seed,
                                }
                    return RunOutcome(
                        status="completed",
                        event_type="run.completed",
                        payload=result_payload,
                    )

                # Gather interrupts from BOTH places LangGraph stores them:
                #   • snapshot.interrupts — aggregated top-level list
                #     (LangGraph 1.x). Reliable when present.
                #   • snapshot.tasks[*].interrupts — per-task lists. With
                #     parallel tool calls (e.g. ToolNode dispatches 3
                #     web.search + 1 user.ask in one step), each tool
                #     gets its own task; the user.ask interrupt lands in
                #     whichever task index ran it, NOT necessarily
                #     tasks[0]. Earlier code only checked tasks[0] and
                #     missed the interrupt → run stalled with empty
                #     interrupts and `next=["tools"]`.
                # We prefer the top-level list and fall back to scanning
                # every task. Dedupe by interrupt id so neither source
                # double-counts.
                interrupts_top = list(getattr(snapshot, "interrupts", ()) or ())
                interrupts_per_task = [
                    intr for task in (snapshot.tasks or ()) for intr in _task_interrupts(task)
                ]
                seen_ids: set[Any] = set()
                interrupts: list[Any] = []
                for intr in interrupts_top + interrupts_per_task:
                    key = getattr(intr, "id", None)
                    if key is None:
                        interrupts.append(intr)
                        continue
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    interrupts.append(intr)
                interrupt_ids = [
                    str(getattr(interrupt, "id", None) or f"anonymous-{index}")
                    for index, interrupt in enumerate(interrupts)
                ]
                wait_cycle_id = (
                    "wait_"
                    + hashlib.sha256(
                        f"{run_id}\0{current_checkpoint_id}\0".encode()
                        + "\0".join(interrupt_ids).encode()
                    ).hexdigest()[:32]
                )
                # Surface to user.
                for snap_interrupt in interrupts:
                    await handle_run_interrupt(
                        self.store,
                        self._enqueue,
                        wakeup,
                        run_id,
                        snap_interrupt,
                        wait_cycle_id=wait_cycle_id,
                    )
                return RunOutcome(
                    status=_waiting_status_for_interrupts(interrupts),
                    event_type="run.waiting",
                    payload={
                        "next": next_nodes,
                        "wait_cycle_id": wait_cycle_id,
                        "interrupts": [
                            {"value": getattr(i, "value", None), "id": getattr(i, "id", None)}
                            for i in interrupts
                        ],
                        "handoff": await build_waiting_handoff(self.store, run_id),
                    },
                )

        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if self._shutting_down or current_task in self._lost_leases:
                raise
            if repair_context is not None:
                await self._enqueue(
                    wakeup,
                    run_id,
                    "repair.workflow",
                    _repair_workflow_payload(repair_context, status="canceled"),
                )
            return RunOutcome(
                status="canceled",
                event_type="run.canceled",
                payload={},
            )
        except Exception as exc:
            failure_payload = _run_failed_payload(
                exc,
                secrets=(model_api_key,) if model_api_key else (),
            )
            if model_api_key:
                log.error(
                    "run %s failed type=%s error=%s",
                    run_id,
                    type(exc).__name__,
                    failure_payload.get("error", "model service request failed"),
                )
            else:
                log.exception("run %s failed", run_id)
            if isinstance(exc, ModelServiceError):
                await self._enqueue(wakeup, run_id, "llm.error", failure_payload)
            if repair_context is not None:
                await self._enqueue(
                    wakeup,
                    run_id,
                    "repair.workflow",
                    _repair_workflow_payload(
                        repair_context,
                        status="failed",
                        reason=str(failure_payload.get("error") or exc),
                    ),
                )
            return RunOutcome(
                status="failed",
                event_type="run.failed",
                payload=failure_payload,
            )
