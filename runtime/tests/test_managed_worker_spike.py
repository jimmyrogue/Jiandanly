from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from docx import Document

from shejane_runtime.plugins.executor import ManagedWorkerActionExecutor
from shejane_runtime.plugins.managed_worker import WorkerProtocolError, _vision_request

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = (
    REPO_ROOT / "runtime" / "plugins" / "fixtures" / "worker-documents" / "payload" / "worker.py"
)


def test_vision_host_call_rejects_unimplemented_structured_task() -> None:
    invocation = {
        "grants": {"capabilities": ["model.vision.invoke"]},
        "model_binding_id": "vision-default",
        "inputs": [{"id": "image"}],
    }
    frame = {
        "jsonrpc": "2.0",
        "id": "worker:vision:1",
        "method": "model/vision/invoke",
        "params": {
            "model_binding_id": "vision-default",
            "input_ids": ["image"],
            "task": "structured",
            "prompt": "Return JSON.",
            "max_output_tokens": 64,
        },
    }

    with pytest.raises(WorkerProtocolError, match="task is invalid"):
        _vision_request(frame, invocation)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_binding_id", "vision-forged", "model binding changed"),
        ("input_ids", ["unbound-image"], "inputs are invalid"),
    ],
)
def test_vision_host_call_cannot_change_frozen_scope(
    field: str,
    value: object,
    message: str,
) -> None:
    invocation = {
        "grants": {"capabilities": ["model.vision.invoke"]},
        "model_binding_id": "vision-default",
        "inputs": [{"id": "image"}],
    }
    params = {
        "model_binding_id": "vision-default",
        "input_ids": ["image"],
        "task": "describe",
        "prompt": "Describe the image.",
        "max_output_tokens": 64,
    }
    params[field] = value

    with pytest.raises(WorkerProtocolError, match=message):
        _vision_request(
            {
                "jsonrpc": "2.0",
                "id": "worker:vision:1",
                "method": "model/vision/invoke",
                "params": params,
            },
            invocation,
        )


@pytest.mark.asyncio
async def test_managed_worker_renders_authorized_document_to_staging(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    document_dir = input_root / "document"
    document_dir.mkdir(parents=True)
    output_root.mkdir()
    source = document_dir / "document.docx"
    document = Document()
    document.add_paragraph("SheJane worker fixture")
    document.save(str(source))
    source_bytes = source.read_bytes()

    executor = ManagedWorkerActionExecutor((sys.executable, str(WORKER)))
    result = await executor.invoke(
        {
            "schema_version": 1,
            "invocation_id": "223e4567-e89b-42d3-a456-426614174001",
            "operation_id": "run_01:document.render:001",
            "action": {
                "plugin_id": "dev.shejane.fixture.documents",
                "plugin_version": "0.1.0",
                "plugin_digest": "sha256:" + "c" * 64,
                "action_id": "document.render",
            },
            "arguments": {"input_id": "document"},
            "inputs": [
                {
                    "id": "document",
                    "path": "/input/document/document.docx",
                    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "size_bytes": len(source_bytes),
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                }
            ],
            "grants": {"capabilities": ["input.read", "artifact.write"]},
            "limits": {"timeout_ms": 5_000, "memory_mb": 512, "output_mb": 8},
            "environment": {"locale": "en-US", "timezone": "UTC"},
        },
        input_root=input_root,
        output_root=output_root,
    )

    assert result["status"] == "succeeded"
    assert result["output"] == {"page_count": 1}
    assert result["artifacts"] == [
        {
            "path": "/output/document.pdf",
            "media_type": "application/pdf",
            "name": "document.pdf",
        }
    ]
    assert (output_root / "document.pdf").read_bytes().startswith(b"%PDF-1.4")


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group spike")
async def test_managed_worker_timeout_kills_descendant_processes(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = """
import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"], "child.pid").write_text(str(child.pid))
time.sleep(60)
"""

    with pytest.raises(TimeoutError):
        executor = ManagedWorkerActionExecutor((sys.executable, "-c", script))
        await executor.invoke(
            {
                "invocation_id": "323e4567-e89b-42d3-a456-426614174002",
                "operation_id": "run_01:document.render:timeout",
                "action": {
                    "plugin_id": "dev.shejane.fixture.documents",
                    "plugin_digest": "sha256:" + "c" * 64,
                    "action_id": "document.render",
                },
                "grants": {"capabilities": []},
                "limits": {"timeout_ms": 250, "memory_mb": 64, "output_mb": 1},
            },
            input_root=input_root,
            output_root=output_root,
        )

    child_pid = int((output_root / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_managed_worker_timeout_requests_cooperative_cancel_first(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import os
import sys
from pathlib import Path

initialize = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}}), flush=True)
invoke = json.loads(sys.stdin.readline())
cancel = json.loads(sys.stdin.readline())
Path(os.environ["SHEJANE_PLUGIN_OUTPUT_ROOT"], "cancel.json").write_text(json.dumps(cancel["params"], sort_keys=True))
"""
    executor = ManagedWorkerActionExecutor((sys.executable, "-c", script))
    invocation = {
        "invocation_id": "323e4567-e89b-42d3-a456-426614174099",
        "operation_id": "run_01:document.render:cooperative-timeout",
        "action": {
            "plugin_id": "dev.shejane.fixture.documents",
            "plugin_digest": "sha256:" + "c" * 64,
            "action_id": "document.render",
        },
        "grants": {"capabilities": []},
        "limits": {"timeout_ms": 100, "memory_mb": 64, "output_mb": 1},
    }

    with pytest.raises(TimeoutError):
        await executor.invoke(
            invocation,
            input_root=input_root,
            output_root=output_root,
        )

    assert json.loads((output_root / "cancel.json").read_text()) == {
        "operation_id": invocation["operation_id"],
        "reason": "timeout",
    }


@pytest.mark.asyncio
async def test_managed_worker_rejects_oversized_protocol_frame(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    executor = ManagedWorkerActionExecutor(
        (sys.executable, "-c", 'print("x" * (1024 * 1024 + 1), flush=True)')
    )

    with pytest.raises(WorkerProtocolError, match="frame limit"):
        await executor.invoke(
            {
                "invocation_id": "423e4567-e89b-42d3-a456-426614174003",
                "operation_id": "run_01:document.render:oversized",
                "action": {
                    "plugin_id": "dev.shejane.fixture.documents",
                    "plugin_digest": "sha256:" + "c" * 64,
                    "action_id": "document.render",
                },
                "grants": {"capabilities": []},
                "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            },
            input_root=input_root,
            output_root=output_root,
        )


@pytest.mark.asyncio
async def test_managed_worker_streams_ordered_progress_before_result(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import sys

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

initialize = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}})
invoke = json.loads(sys.stdin.readline())
params = invoke["params"]
for sequence, completed in ((1, 1), (2, 2), (3, 3)):
    send({"jsonrpc":"2.0","method":"notifications/progress","params":{
        "schema_version":1,
        "invocation_id":params["invocation_id"],
        "operation_id":params["operation_id"],
        "sequence":sequence,
        "phase":"decode",
        "message":"Decoding media",
        "completed":completed,
        "total":3,
        "unit":"frames"
    }})
send({"jsonrpc":"2.0","id":invoke["id"],"result":{
    "schema_version":1,
    "invocation_id":params["invocation_id"],
    "operation_id":params["operation_id"],
    "status":"succeeded",
    "output":{},
    "artifacts":[]
}})
shutdown = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":shutdown["id"],"result":{}})
"""
    progress: list[dict[str, object]] = []
    executor = ManagedWorkerActionExecutor((sys.executable, "-c", script))
    result = await executor.invoke(
        {
            "schema_version": 1,
            "invocation_id": "523e4567-e89b-42d3-a456-426614174004",
            "operation_id": "run_01:media.inspect:progress",
            "action": {
                "plugin_id": "dev.shejane.fixture.media",
                "plugin_version": "0.1.0",
                "plugin_digest": "sha256:" + "d" * 64,
                "action_id": "media.inspect",
            },
            "arguments": {},
            "inputs": [],
            "grants": {"capabilities": []},
            "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            "environment": {"locale": "en-US", "timezone": "UTC"},
        },
        input_root=input_root,
        output_root=output_root,
        on_progress=progress.append,
    )

    assert [item["sequence"] for item in progress] == [1, 3]
    assert progress[-1]["completed"] == progress[-1]["total"] == 3
    assert result["status"] == "succeeded"


@pytest.mark.asyncio
async def test_managed_worker_rejects_out_of_order_progress(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import sys

initialize = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}}), flush=True)
invoke = json.loads(sys.stdin.readline())
params = invoke["params"]
print(json.dumps({"jsonrpc":"2.0","method":"notifications/progress","params":{"schema_version":1,"invocation_id":params["invocation_id"],"operation_id":params["operation_id"],"sequence":2,"phase":"decode"}}), flush=True)
"""
    executor = ManagedWorkerActionExecutor((sys.executable, "-c", script))

    with pytest.raises(WorkerProtocolError, match="progress sequence"):
        await executor.invoke(
            {
                "invocation_id": "623e4567-e89b-42d3-a456-426614174005",
                "operation_id": "run_01:media.inspect:bad-progress",
                "action": {
                    "plugin_id": "dev.shejane.fixture.media",
                    "plugin_digest": "sha256:" + "d" * 64,
                    "action_id": "media.inspect",
                },
                "grants": {"capabilities": []},
                "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            },
            input_root=input_root,
            output_root=output_root,
        )


@pytest.mark.asyncio
async def test_managed_worker_can_request_one_granted_vision_host_call(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import sys

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

initialize = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}})
invoke = json.loads(sys.stdin.readline())
params = invoke["params"]
send({"jsonrpc":"2.0","id":"worker:vision:1","method":"model/vision/invoke","params":{
    "model_binding_id":"vision-default",
    "input_ids":["image"],
    "task":"describe",
    "prompt":"Describe the image.",
    "max_output_tokens":64
}})
host = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":invoke["id"],"result":{
    "schema_version":1,
    "invocation_id":params["invocation_id"],
    "operation_id":params["operation_id"],
    "status":"succeeded",
    "output":{"text":host["result"]["text"]},
    "artifacts":[]
}})
shutdown = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":shutdown["id"],"result":{}})
"""
    requests: list[dict[str, object]] = []

    async def invoke_vision(params: dict[str, object]) -> dict[str, object]:
        requests.append(params)
        return {"text": "A paper lantern."}

    executor = ManagedWorkerActionExecutor(
        (sys.executable, "-c", script),
        vision_handler=invoke_vision,
    )
    result = await executor.invoke(
        {
            "schema_version": 1,
            "invocation_id": "723e4567-e89b-42d3-a456-426614174006",
            "operation_id": "run_01:vision.analyze:host-call",
            "action": {
                "plugin_id": "dev.shejane.fixture.vision",
                "plugin_version": "0.1.0",
                "plugin_digest": "sha256:" + "e" * 64,
                "action_id": "vision.analyze",
            },
            "arguments": {},
            "inputs": [
                {
                    "id": "image",
                    "path": "/input/image.png",
                    "media_type": "image/png",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                }
            ],
            "grants": {"capabilities": ["input.read", "model.vision.invoke"]},
            "model_binding_id": "vision-default",
            "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            "environment": {"locale": "en-US", "timezone": "UTC"},
        },
        input_root=input_root,
        output_root=output_root,
    )

    assert requests == [
        {
            "model_binding_id": "vision-default",
            "input_ids": ["image"],
            "task": "describe",
            "prompt": "Describe the image.",
            "max_output_tokens": 64,
        }
    ]
    assert result["output"] == {"text": "A paper lantern."}


@pytest.mark.asyncio
async def test_managed_worker_rejects_ungranted_vision_host_call(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import sys

initialize = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}}), flush=True)
invoke = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0","id":"worker:vision:1","method":"model/vision/invoke","params":{"model_binding_id":"vision-default","input_ids":["image"],"task":"describe","prompt":"Describe.","max_output_tokens":64}}), flush=True)
"""

    async def invoke_vision(_params: dict[str, object]) -> dict[str, object]:
        return {"text": "must not run"}

    executor = ManagedWorkerActionExecutor(
        (sys.executable, "-c", script),
        vision_handler=invoke_vision,
    )
    with pytest.raises(WorkerProtocolError, match="not granted"):
        await executor.invoke(
            {
                "invocation_id": "823e4567-e89b-42d3-a456-426614174007",
                "operation_id": "run_01:vision.analyze:denied",
                "action": {
                    "plugin_id": "dev.shejane.fixture.vision",
                    "plugin_digest": "sha256:" + "e" * 64,
                    "action_id": "vision.analyze",
                },
                "inputs": [],
                "grants": {"capabilities": []},
                "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            },
            input_root=input_root,
            output_root=output_root,
        )


@pytest.mark.asyncio
async def test_managed_worker_allows_only_one_vision_host_call(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    script = r"""
import json
import sys

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

initialize = json.loads(sys.stdin.readline())
send({"jsonrpc":"2.0","id":initialize["id"],"result":{"protocol_version":1,"process_isolated":True,"access_isolated":False,"resource_isolated":False,"sandboxed":False}})
json.loads(sys.stdin.readline())
request = {"jsonrpc":"2.0","method":"model/vision/invoke","params":{"model_binding_id":"vision-default","input_ids":["image"],"task":"describe","prompt":"Describe.","max_output_tokens":64}}
send({"id":"worker:vision:1", **request})
json.loads(sys.stdin.readline())
send({"id":"worker:vision:2", **request})
"""
    calls = 0

    async def invoke_vision(_params: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"text": "A paper lantern."}

    executor = ManagedWorkerActionExecutor(
        (sys.executable, "-c", script),
        vision_handler=invoke_vision,
    )
    with pytest.raises(WorkerProtocolError, match="host-call limit"):
        await executor.invoke(
            {
                "invocation_id": "923e4567-e89b-42d3-a456-426614174008",
                "operation_id": "run_01:vision.analyze:second-call",
                "action": {
                    "plugin_id": "dev.shejane.fixture.vision",
                    "plugin_digest": "sha256:" + "e" * 64,
                    "action_id": "vision.analyze",
                },
                "inputs": [{"id": "image"}],
                "grants": {"capabilities": ["model.vision.invoke"]},
                "model_binding_id": "vision-default",
                "limits": {"timeout_ms": 2_000, "memory_mb": 64, "output_mb": 1},
            },
            input_root=input_root,
            output_root=output_root,
        )

    assert calls == 1
