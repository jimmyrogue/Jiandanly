"""P10 adapter from frozen plugin Actions to Runtime tools and Artifacts."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.config import get_stream_writer

from ..store.sqlite import LocalStore
from ..tools.runtime import RuntimeToolExecution, current_runtime_tool_execution
from .browser_qa import BrowserQAError
from .catalog import PluginActionDescriptor
from .computer_use import ComputerUseError
from .executor import ActionExecutor, ManagedWorkerActionExecutor, WasiActionExecutor
from .linux_cgroup import LinuxCgroupResources
from .macos_vm import MacOSVMResources
from .managed_worker import WorkerProtocolError
from .platforms import current_managed_worker_platform
from .sandbox_runtime import (
    SandboxRuntimeError,
    configured_srt_launcher,
    managed_worker_release_gate,
)
from .tool_io import (
    PluginActionError as PluginActionError,
)
from .tool_io import (
    _materialize_inputs,
    _persist_artifacts,
    _resolve_inputs,
)
from .wasi import WasiProtocolError, WasiResourceLimitError

_V1_PLATFORM_CAPABILITIES = frozenset(
    {
        "input.read",
        "artifact.write",
        "browser.control",
        "browser.observe",
        "model.vision.invoke",
        "computer.observe",
        "computer.control",
        "computer.setup",
    }
)


def _plugin_error_content(error: ToolException) -> str:
    if not isinstance(error, PluginActionError):
        return str(error)
    return json.dumps(
        {
            "ok": False,
            "error_code": error.code,
            "message": str(error),
            "retryable": False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class PluginToolAdapter:
    def __init__(
        self,
        executor_factory: Callable[[PluginActionDescriptor], ActionExecutor] | None = None,
        vision_invoker: Callable[
            [Mapping[str, Any], dict[str, Any], Path, tuple[dict[str, Any], ...]],
            Awaitable[dict[str, Any]],
        ]
        | None = None,
    ) -> None:
        self._executor_factory = executor_factory or _executor_for_action
        self._vision_invoker = vision_invoker

    async def invoke(
        self,
        action: PluginActionDescriptor,
        arguments: dict[str, Any],
        execution: RuntimeToolExecution,
    ) -> Any:
        context = execution.context
        store = getattr(context, "store", None)
        run_id = str(getattr(context, "run_id", None) or "")
        tool_call_id = execution.tool_call_id
        if not isinstance(store, LocalStore) or not run_id or not tool_call_id:
            raise PluginActionError(
                "invalid_invocation",
                "plugin Action is missing durable Runtime context",
            )
        _validate_json(action.input_schema, arguments, code="invalid_invocation")

        denied = set(action.capabilities) - _V1_PLATFORM_CAPABILITIES
        if denied:
            raise PluginActionError(
                "capability_denied",
                f"plugin Action requests unavailable capabilities: {', '.join(sorted(denied))}",
            )
        uses_vision = "model.vision.invoke" in action.capabilities
        if uses_vision and action.execution_kind != "managed_worker":
            raise PluginActionError(
                "capability_denied",
                "model.vision.invoke requires a Managed Worker Action",
            )
        if uses_vision and (action.model_binding is None or self._vision_invoker is None):
            raise PluginActionError(
                "model_binding_unavailable",
                "Vision Action requires an explicit configured model binding",
            )
        inputs = await _resolve_inputs(
            store=store,
            run_id=run_id,
            action=action,
            arguments=arguments,
            context_inputs=getattr(context, "plugin_inputs", ()),
        )
        if "input.read" in action.capabilities and not inputs:
            raise PluginActionError(
                "invalid_invocation",
                "plugin Action requires a compatible attachment",
            )
        if inputs and "input_id" in arguments:
            arguments = {**arguments, "input_id": inputs[0]["id"]}
        elif inputs and "input_ids" in arguments:
            arguments = {**arguments, "input_ids": [item["id"] for item in inputs]}
        public_inputs = [
            {key: value for key, value in item.items() if key != "source_path"} for item in inputs
        ]
        environment = {
            "locale": getattr(context, "locale", None) or "en-US",
            "timezone": "UTC",
        }
        operation_id = execution.operation_id
        invocation = {
            "schema_version": 1,
            "invocation_id": str(uuid.uuid4()),
            "operation_id": operation_id,
            "action": {
                "plugin_id": action.plugin_id,
                "plugin_version": action.plugin_version,
                "plugin_digest": action.plugin_digest,
                "action_id": action.action_id,
            },
            "arguments": arguments,
            "inputs": public_inputs,
            "grants": {
                "capabilities": sorted(set(action.capabilities) & _V1_PLATFORM_CAPABILITIES)
            },
            "limits": dict(action.limits),
            "environment": environment,
            **(
                {"model_binding_id": str(action.model_binding["id"])}
                if uses_vision and action.model_binding is not None
                else {}
            ),
        }

        staging_root = action.package_root.parent.parent / "executions"
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="action-", dir=staging_root) as temporary:
            root = Path(temporary)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir(mode=0o700)
            output_root.mkdir(mode=0o700)
            await _materialize_inputs(inputs, input_root)
            vision_result: dict[str, Any] | None = None

            async def invoke_vision(params: dict[str, Any]) -> dict[str, Any]:
                nonlocal vision_result
                assert action.model_binding is not None and self._vision_invoker is not None
                if params["model_binding_id"] != action.model_binding["id"]:
                    raise PluginActionError(
                        "model_binding_unavailable",
                        "Vision Worker requested a different model binding",
                    )
                vision_result = await self._vision_invoker(
                    action.model_binding,
                    params,
                    input_root,
                    tuple(public_inputs),
                )
                return vision_result

            try:
                stream_writer = get_stream_writer()
            except RuntimeError:

                def stream_writer(_payload: dict[str, Any]) -> None:
                    return None

            def emit_progress(progress: dict[str, Any]) -> None:
                stream_writer(
                    {
                        "event": "tool.progress",
                        "data": {
                            **progress,
                            "tool_call_id": tool_call_id,
                            "tool": action.tool_name,
                        },
                    }
                )

            executor = self._executor_factory(action)
            if uses_vision and isinstance(executor, ManagedWorkerActionExecutor):
                executor = replace(executor, vision_handler=invoke_vision)
            try:
                result = await executor.invoke(
                    invocation,
                    input_root=input_root,
                    output_root=output_root,
                    on_progress=emit_progress,
                )
            except WasiResourceLimitError as exc:
                raise PluginActionError(exc.code, str(exc)) from exc
            except WasiProtocolError as exc:
                raise PluginActionError("protocol_violation", str(exc)) from exc
            except TimeoutError as exc:
                raise PluginActionError(
                    "resource_exhausted",
                    "Managed Worker execution deadline exceeded",
                ) from exc
            except WorkerProtocolError as exc:
                raise PluginActionError(
                    "protocol_violation",
                    "Managed Worker violated the execution protocol",
                ) from exc
            except BrowserQAError as exc:
                raise PluginActionError("browser_qa_failed", str(exc)) from exc
            except ComputerUseError as exc:
                raise PluginActionError("computer_use_failed", str(exc)) from exc
            _validate_result_identity(result, invocation)
            if result["status"] == "failed":
                error = result.get("error") if isinstance(result.get("error"), dict) else {}
                raise PluginActionError(
                    str(error.get("code") or "plugin_failed"),
                    str(error.get("message") or "plugin Action failed"),
                )
            output = result.get("output", {})
            _validate_json(action.output_schema, output, code="protocol_violation")
            provenance = {
                "plugin": {
                    "id": action.plugin_id,
                    "version": action.plugin_version,
                    "digest": action.plugin_digest,
                },
                "action_id": action.action_id,
                "operation_id": operation_id,
                "inputs": public_inputs,
                "parameters": arguments,
            }
            if action.runtime_assets:
                provenance["runtime_assets"] = [
                    {
                        "id": asset.asset_id,
                        "version": asset.version,
                        "digest": asset.digest,
                        "platform": asset.platform,
                    }
                    for asset in action.runtime_assets
                ]
            if uses_vision:
                assert action.model_binding is not None
                provenance["model"] = {
                    "backend": "cloud",
                    "binding_id": str(action.model_binding["id"]),
                    "connection_id": str(action.model_binding["connection_id"]),
                    "connection_version": int(action.model_binding["connection_version"]),
                    "model_id": str(action.model_binding["model_id"]),
                    **(
                        {"usage": vision_result["usage"]}
                        if isinstance(vision_result, dict)
                        and isinstance(vision_result.get("usage"), dict)
                        else {}
                    ),
                }
            artifacts = await _persist_artifacts(
                store=store,
                run_id=run_id,
                operation_id=operation_id,
                tool_call_id=tool_call_id,
                action=action,
                output_root=output_root,
                candidates=result.get("artifacts", []),
                provenance=provenance,
            )
            response = {
                "status": "succeeded",
                "output": output,
                "artifacts": artifacts,
                "provenance": provenance,
            }
            images = output.get("images") if isinstance(output, dict) else None
            model_images = (
                isinstance(images, list)
                and bool(images)
                and all(
                    isinstance(image, dict)
                    and isinstance(image.get("base64"), str)
                    and isinstance(image.get("mime_type"), str)
                    for image in images
                )
            )
            if action.execution_kind == "builtin" and model_images:
                text_output = dict(output)
                del text_output["images"]
                response["output"] = text_output
                return [
                    {
                        "type": "text",
                        "text": json.dumps(
                            response,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                    *({"type": "image", **image} for image in images),
                ]
            return response


def build_plugin_tool(
    action: PluginActionDescriptor,
    *,
    adapter: PluginToolAdapter | None = None,
    linux_cgroup: LinuxCgroupResources | None = None,
    vm_resources: MacOSVMResources | None = None,
    vision_invoker: Callable[
        [Mapping[str, Any], dict[str, Any], Path, tuple[dict[str, Any], ...]],
        Awaitable[dict[str, Any]],
    ]
    | None = None,
) -> BaseTool:
    active_adapter = adapter or PluginToolAdapter(
        executor_factory=lambda selected: _executor_for_action(
            selected,
            linux_cgroup=linux_cgroup,
            vm_resources=vm_resources,
        ),
        vision_invoker=vision_invoker,
    )

    async def invoke_plugin_action(
        **arguments: Any,
    ) -> Any:
        return await active_adapter.invoke(
            action,
            arguments,
            current_runtime_tool_execution(),
        )

    return StructuredTool.from_function(
        coroutine=invoke_plugin_action,
        name=action.tool_name,
        description=action.description,
        args_schema=_thaw_mapping(action.input_schema),
        handle_tool_error=_plugin_error_content,
    )


def _executor_for_action(
    action: PluginActionDescriptor,
    *,
    linux_cgroup: LinuxCgroupResources | None = None,
    vm_resources: MacOSVMResources | None = None,
) -> ActionExecutor:
    if action.execution_kind == "wasi":
        return WasiActionExecutor(action.entrypoint, action.entrypoint_digest)
    platform = current_managed_worker_platform()
    gate = managed_worker_release_gate(platform or "unsupported")
    if not gate.enabled:
        raise PluginActionError(
            "executor_unavailable",
            "Managed Worker release gate is closed: " + ", ".join(gate.blockers),
        )
    if platform and platform.startswith("darwin/") and vm_resources is None:
        raise PluginActionError(
            "executor_unavailable",
            "macOS Managed Worker execution requires packaged VM resources",
        )
    if vm_resources is not None:
        return ManagedWorkerActionExecutor(
            (str(action.entrypoint),),
            vm_resources=vm_resources,
            package_root=action.package_root,
            runtime_assets=action.runtime_assets,
        )
    if linux_cgroup is not None:
        return ManagedWorkerActionExecutor(
            (str(action.entrypoint),),
            linux_cgroup=linux_cgroup,
            package_root=action.package_root,
            runtime_assets=action.runtime_assets,
        )
    try:
        launcher = configured_srt_launcher()
    except SandboxRuntimeError as exc:
        raise PluginActionError("executor_unavailable", str(exc)) from exc
    if launcher is None:
        raise PluginActionError(
            "executor_unavailable",
            "Managed Worker execution requires an enforced operating-system sandbox",
        )
    return ManagedWorkerActionExecutor(
        (str(action.entrypoint),),
        sandbox_command=launcher,
        package_root=action.package_root,
        runtime_assets=action.runtime_assets,
    )


def _validate_result_identity(result: Any, invocation: dict[str, Any]) -> None:
    if (
        not isinstance(result, dict)
        or result.get("schema_version") != 1
        or result.get("invocation_id") != invocation["invocation_id"]
        or result.get("operation_id") != invocation["operation_id"]
        or result.get("status") not in {"succeeded", "failed"}
    ):
        raise PluginActionError("protocol_violation", "plugin returned an invalid result envelope")


def _validate_json(schema: Mapping[str, Any], value: Any, *, code: str) -> None:
    plain_schema = _thaw_mapping(schema)
    try:
        validator = validator_for(plain_schema)
        validator.check_schema(plain_schema)
        validator(plain_schema).validate(value)
    except Exception as exc:
        raise PluginActionError(code, f"plugin schema validation failed: {exc}") from exc


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw_json(item) for key, item in value.items()}
