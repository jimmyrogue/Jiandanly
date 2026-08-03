"""Run the pinned official A2A TCK against the SheJane gateway adapter."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_TCK_COMMIT = "5996b79f9cefa6fc390980e383e358a66fb9e49e"


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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"A2A TCK SUT exited with status {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("A2A TCK SUT did not become ready")


def _must_failures(report: dict[str, Any]) -> list[str]:
    requirements = report.get("per_requirement")
    if not isinstance(requirements, dict):
        raise RuntimeError("A2A TCK report has no requirement results")
    return sorted(
        requirement_id
        for requirement_id, result in requirements.items()
        if isinstance(result, dict)
        and result.get("level") == "MUST"
        and result.get("status") == "FAIL"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tck-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    tck_root = args.tck_root.resolve()
    if not (tck_root / "run_tck.py").is_file():
        raise SystemExit(f"not an A2A TCK checkout: {tck_root}")
    commit = _git_commit(tck_root)
    if commit != _TCK_COMMIT:
        raise SystemExit(f"A2A TCK commit must be {_TCK_COMMIT}, got {commit}")
    if _git_dirty(tck_root):
        raise SystemExit("A2A TCK checkout has modified tracked files")

    runtime_root = Path(__file__).resolve().parents[1]
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    report_path = tck_root / "reports/compatibility.json"
    started_at = time.time_ns()

    with tempfile.TemporaryDirectory(prefix="shejane-a2a-tck-") as temporary:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("a2a_tck_sut.py")),
                "--db",
                str(Path(temporary) / "gateway.db"),
                "--port",
                str(port),
            ],
            cwd=runtime_root,
            text=True,
        )
        try:
            _wait_ready(f"{base_url}/.well-known/agent-card.json", process)
            completed = subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    str(tck_root),
                    "--frozen",
                    "python",
                    str(tck_root / "run_tck.py"),
                    "--sut-host",
                    base_url,
                    "--transport",
                    "jsonrpc",
                ],
                cwd=tck_root,
                check=False,
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    if not report_path.is_file() or report_path.stat().st_mtime_ns < started_at:
        raise RuntimeError("A2A TCK did not produce a fresh compatibility report")
    report = json.loads(report_path.read_text())
    failures = _must_failures(report)
    summary = {
        "tck_commit": commit,
        "must_failures": failures,
        **report.get("summary", {}),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report_path, args.output)
    return 0 if completed.returncode == 0 and not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
