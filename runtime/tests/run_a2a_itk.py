"""Run the pinned official A2A ITK against the SheJane gateway adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

_ITK_COMMIT = "486e7add944daaf1a6e247a433782fa0824039ac"

_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "name": "shejane-python-standard",
        "sdks": ["current", "python_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
    },
    {
        "name": "shejane-python-streaming",
        "sdks": ["current", "python_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
        "streaming": True,
    },
    {
        "name": "shejane-python-push",
        "sdks": ["current", "python_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "push_notification",
    },
    {
        "name": "shejane-python-resubscribe-cancel",
        "sdks": ["current", "python_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "resubscribe",
        "streaming": True,
    },
    {
        "name": "shejane-go-standard",
        "sdks": ["current", "go_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
    },
    {
        "name": "shejane-go-streaming",
        "sdks": ["current", "go_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
        "streaming": True,
    },
    {
        "name": "shejane-go-push",
        "sdks": ["current", "go_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "push_notification",
    },
    {
        "name": "shejane-go-resubscribe-cancel",
        "sdks": ["current", "go_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "resubscribe",
        "streaming": True,
    },
    {
        "name": "shejane-typescript-standard",
        "sdks": ["current", "ts_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
    },
    {
        "name": "shejane-typescript-streaming",
        "sdks": ["current", "ts_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "send_message",
        "streaming": True,
    },
    {
        "name": "shejane-typescript-push",
        "sdks": ["current", "ts_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "push_notification",
    },
    {
        "name": "shejane-typescript-resubscribe-cancel",
        "sdks": ["current", "ts_v10"],
        "edges": ["0->1", "1->0"],
        "behavior": "resubscribe",
        "streaming": True,
    },
    {
        "name": "shejane-python-go-typescript-multihop",
        "sdks": ["current", "python_v10", "go_v10", "ts_v10"],
        "edges": ["0->1", "1->0", "0->2", "2->0", "0->3", "3->0"],
        "behavior": "send_message",
    },
    {
        "name": "shejane-python-go-typescript-multihop-streaming",
        "sdks": ["current", "python_v10", "go_v10", "ts_v10"],
        "edges": ["0->1", "1->0", "0->2", "2->0", "0->3", "3->0"],
        "behavior": "send_message",
        "streaming": True,
    },
)


def _git_commit(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_dirty(root: Path) -> bool:
    return bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


async def _run(args: argparse.Namespace) -> int:
    itk_root = args.itk_root.resolve()
    if not (itk_root / "testlib.py").is_file():
        raise SystemExit(f"not an A2A ITK checkout: {itk_root}")
    commit = _git_commit(itk_root)
    if commit != _ITK_COMMIT:
        raise SystemExit(f"A2A ITK commit must be {_ITK_COMMIT}, got {commit}")
    if _git_dirty(itk_root):
        raise SystemExit("A2A ITK checkout has modified tracked files")
    sys.path.insert(0, str(itk_root))
    import test_suite  # type: ignore[import-not-found]
    import testlib  # type: ignore[import-not-found]

    selected = [
        scenario
        for scenario in _SCENARIOS
        if not args.scenario or scenario["name"] in args.scenario
    ]
    unknown = set(args.scenario or ()) - {scenario["name"] for scenario in _SCENARIOS}
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(sorted(unknown))}")
    required = sorted({sdk for scenario in selected for sdk in scenario["sdks"]})

    runtime_root = Path(__file__).resolve().parents[1]
    sut = Path(__file__).with_name("a2a_itk_sut.py")
    with tempfile.TemporaryDirectory(prefix="shejane-a2a-itk-") as temporary:
        database = Path(temporary) / "gateway.db"

        def launch_current(http_port: int, grpc_port: int) -> subprocess.Popen[str]:
            return subprocess.Popen(
                [
                    sys.executable,
                    str(sut),
                    "--db",
                    str(database),
                    "--itk-root",
                    str(itk_root),
                    "--httpPort",
                    str(http_port),
                    "--grpcPort",
                    str(grpc_port),
                ],
                cwd=runtime_root,
                text=True,
            )

        test_suite.get_agent_def("current")["launcher"] = launch_current

        def launch_python_v10(http_port: int, grpc_port: int) -> subprocess.Popen[str]:
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            return subprocess.Popen(
                [
                    "uv",
                    "run",
                    "--project",
                    str(runtime_root),
                    "--with",
                    "grpcio",
                    "python",
                    str(itk_root / "agents/python/v10/main.py"),
                    "--httpPort",
                    str(http_port),
                    "--grpcPort",
                    str(grpc_port),
                ],
                cwd=itk_root / "agents/python/v10",
                env=environment,
                text=True,
            )

        test_suite.get_agent_def("python_v10")["launcher"] = launch_python_v10

        async def start_notification_server(port: int, _test_name: str) -> subprocess.Popen[str]:
            testlib._clean_ports(port)
            process = subprocess.Popen(  # noqa: ASYNC220 - ITK API requires Popen
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "notifications_app:create_notifications_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=itk_root,
                text=True,
            )
            async with httpx.AsyncClient(timeout=2) as client:
                for _ in range(20):
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/health")
                        if response.status_code == 200:
                            return process
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.25)
            process.terminate()
            raise RuntimeError("ITK notification server failed to start")

        testlib.start_notification_server = start_notification_server
        read_push_notifications = testlib.read_push_notifications

        async def read_push_after_outbox(notification_server_url: str) -> list[str]:
            await asyncio.sleep(1.5)
            return await read_push_notifications(notification_server_url)

        testlib.read_push_notifications = read_push_after_outbox

        if "go_v10" in required:
            go_agent = Path(temporary) / "go-v10"
            shutil.copytree(itk_root / "agents/go/v10", go_agent)
            go_mod = go_agent / "go.mod"
            go_manifest = go_mod.read_text()
            old_go = "github.com/a2aproject/a2a-go/v2 v2.3.1"
            if old_go not in go_manifest:
                raise RuntimeError("pinned ITK Go SDK version changed; refresh the matrix")
            go_mod.write_text(go_manifest.replace(old_go, "github.com/a2aproject/a2a-go/v2 v2.4.0"))
            go_main = go_agent / "main.go"
            go_source = go_main.read_text()
            old_sender = "push.NewHTTPPushSender(nil)"
            if old_sender not in go_source:
                raise RuntimeError("pinned ITK Go push sender changed; refresh the fixture")
            go_main.write_text(
                go_source.replace(
                    old_sender,
                    "push.NewHTTPPushSender(&push.HTTPSenderConfig{AllowPrivateNetworks: true})",
                    1,
                )
            )

            def launch_go_v10(http_port: int, grpc_port: int) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [
                        "go",
                        "run",
                        "-mod=mod",
                        "main.go",
                        "--httpPort",
                        str(http_port),
                        "--grpcPort",
                        str(grpc_port),
                    ],
                    cwd=go_agent,
                    text=True,
                )

            test_suite.get_agent_def("go_v10")["launcher"] = launch_go_v10

        if "ts_v10" in required:
            typescript_agent = Path(temporary) / "ts-v10"
            shutil.copytree(
                itk_root / "agents/ts/v10",
                typescript_agent,
                ignore=shutil.ignore_patterns("node_modules"),
            )
            package_json = typescript_agent / "package.json"
            manifest = json.loads(package_json.read_text())
            manifest["dependencies"]["@a2a-js/sdk"] = "1.0.1"
            package_json.write_text(json.dumps(manifest, indent=2) + "\n")

            def launch_typescript_v10(http_port: int, grpc_port: int) -> subprocess.Popen[str]:
                if not (typescript_agent / "node_modules").is_dir():
                    subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund", "--silent"],
                        cwd=typescript_agent,
                        check=True,
                    )
                return subprocess.Popen(
                    [
                        str(typescript_agent / "node_modules/.bin/tsx"),
                        "main.ts",
                        "--httpPort",
                        str(http_port),
                        "--grpcPort",
                        str(grpc_port),
                    ],
                    cwd=typescript_agent,
                    text=True,
                )

            test_suite.get_agent_def("ts_v10")["launcher"] = launch_typescript_v10

        processes, _uris, ports = await testlib.start_itk_cluster(required)
        results: dict[str, Any] = {}
        try:
            for scenario in selected:
                outcome = await testlib.execute_itk_test(
                    sdks=scenario["sdks"],
                    behavior=scenario["behavior"],
                    edges=scenario["edges"],
                    scenario_name=scenario["name"],
                    protocols=["jsonrpc"],
                    streaming=bool(scenario.get("streaming")),
                )
                results.update(outcome)
        finally:
            testlib.stop_itk_cluster(processes, ports)

    passed = all(bool(item.get("passed")) for item in results.values())
    report = {
        "all_passed": passed,
        "itk_commit": commit,
        "sdk_matrix": {
            "a2a-python": "1.1.2",
            "a2a-go": "2.4.0",
            "@a2a-js/sdk": "1.0.1",
        },
        "scenario_count": len(results),
        "results": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    return 0 if passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--itk-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario", action="append")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
