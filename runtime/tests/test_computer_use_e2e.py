from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from shejane_runtime.agent.context_builder import RuntimeContext
from shejane_runtime.auth import LOCAL_OWNER_PRINCIPAL_ID
from shejane_runtime.plugins.catalog import PluginActionDescriptor
from shejane_runtime.plugins.computer_use import (
    COMPUTER_USE_PLUGIN_VERSION,
    ComputerUseActionExecutor,
    ComputerUseService,
)
from shejane_runtime.plugins.tools import PluginToolAdapter
from shejane_runtime.store.sqlite import LocalStore
from shejane_runtime.tools.runtime import RuntimeToolExecution

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "runtime" / "plugins" / "computer-use"
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def action_descriptor(package: Path, action_id: str) -> PluginActionDescriptor:
    template = json.loads(
        (PLUGIN_ROOT / ".shejane-plugin" / "plugin.template.json").read_text(encoding="utf-8")
    )
    action = next(item for item in template["contributions"]["actions"] if item["id"] == action_id)
    return PluginActionDescriptor(
        plugin_id="org.shejane.computer-use",
        plugin_version=COMPUTER_USE_PLUGIN_VERSION,
        plugin_digest="sha256:" + "b" * 64,
        action_id=action_id,
        tool_name=f"plugin.org.shejane.computer-use.{action_id}",
        title=action["title"],
        description=action["description"],
        action_schema_digest="sha256:" + "c" * 64,
        input_schema=json.loads((PLUGIN_ROOT / action["input_schema"]).read_text(encoding="utf-8")),
        output_schema=json.loads(
            (PLUGIN_ROOT / action["output_schema"]).read_text(encoding="utf-8")
        ),
        consumes=tuple(action["consumes"]),
        produces=tuple(action["produces"]),
        effects=tuple(action["effects"]),
        determinism=action["determinism"],
        capabilities=tuple(action["capabilities"]),
        limits=action["limits"],
        package_root=package,
        entrypoint=package / "payload" / "bridge-server.mjs",
        entrypoint_digest="sha256:" + "d" * 64,
        execution_kind="builtin",
        execution_handler="computer_use",
        runtime_assets=(),
        model_binding=None,
    )


def write_bridge(package: Path) -> None:
    bridge = package / "payload" / "bridge-server.mjs"
    bridge.parent.mkdir(parents=True)
    bridge.write_text(
        f"""
import readline from 'node:readline';
const lines = readline.createInterface({{ input: process.stdin, crlfDelay: Infinity }});
for await (const line of lines) {{
  const request = JSON.parse(line);
  let result;
  if (request.action === 'observe_ui') {{
    result = {{
      text: '@r1 window Fixture\\n  @e1 button Save',
      details: {{ stateId: 'state-1', pid: process.pid }},
      images: [{{ base64: '{PNG_1X1}', mime_type: 'image/png' }}],
    }};
  }} else if (
    request.action === 'act_ui' &&
    request.arguments.stateId === 'state-1' &&
    request.arguments.actions[0]?.ref === '@e1'
  ) {{
    result = {{
      text: 'clicked Save',
      details: {{ stateId: 'state-2', pid: process.pid }},
    }};
  }} else {{
    process.stdout.write(JSON.stringify({{
      id: request.id,
      error: {{ message: 'unexpected action or stale state' }},
    }}) + '\\n');
    continue;
  }}
  process.stdout.write(JSON.stringify({{ id: request.id, result }}) + '\\n');
}}
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_computer_use_bridge_observe_act_and_cleanup_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("SHEJANE_REQUIRE_FIXED_PLUGIN_E2E") == "1":
            pytest.fail("Node.js is required")
        pytest.skip("Node.js is unavailable")
    pid: int | None = None
    package = tmp_path / "package"
    write_bridge(package)
    monkeypatch.setenv("SHEJANE_RUNTIME_NODE_PATH", node)
    store = await LocalStore.open(tmp_path / "runtime.db")
    run = await store.create_run(
        principal_id=LOCAL_OWNER_PRINCIPAL_ID,
        goal="verify the desktop fixture",
        workspace_path=None,
    )
    service = ComputerUseService(package, workspace_root=tmp_path)
    adapter = PluginToolAdapter(
        executor_factory=lambda action: ComputerUseActionExecutor(service, action.action_id)
    )
    context = RuntimeContext(store=store, run_id=str(run["id"]), plugin_inputs=())

    async def invoke(action_id: str, arguments: dict[str, object]) -> object:
        return await adapter.invoke(
            action_descriptor(package, action_id),
            arguments,
            RuntimeToolExecution(
                context=context,
                operation_id=f"toolop_computer_{action_id}",
                tool_call_id=f"call_computer_{action_id}",
            ),
        )

    try:
        observed = await invoke("observe_ui", {"mode": "fused"})
        assert isinstance(observed, list)
        assert observed[1] == {
            "type": "image",
            "base64": PNG_1X1,
            "mime_type": "image/png",
        }
        observed_payload = json.loads(observed[0]["text"])
        assert "@e1 button Save" in observed_payload["output"]["text"]
        assert observed_payload["output"]["details"]["stateId"] == "state-1"
        pid = int(observed_payload["output"]["details"]["pid"])

        acted = await invoke(
            "act_ui",
            {
                "stateId": "state-1",
                "actions": [{"action": "click", "ref": "@e1"}],
            },
        )
        assert isinstance(acted, dict)
        assert acted["output"]["text"] == "clicked Save"
        assert acted["output"]["details"] == {"stateId": "state-2", "pid": pid}
        assert acted["provenance"]["plugin"]["id"] == "org.shejane.computer-use"
    finally:
        await service.aclose()
        await store.close()

    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
