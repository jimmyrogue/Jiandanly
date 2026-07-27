"""CLI: run the seed eval cases against a RUNNING local runtime.

    SHEJANE_EVAL_RUNTIME_URL  (default http://127.0.0.1:17371)
    SHEJANE_EVAL_TOKEN       (or SHEJANE_RUNTIME_TOKEN) — runtime bearer token
    SHEJANE_EVAL_MODEL       concrete local:<connection>:<model> selection

Exits non-zero if any case fails, so it gates CI / `make eval`. Uses the
heuristic judge (objective, no extra LLM cost). For a meaningful score the
Runtime must run against a real configured provider — otherwise the
mock-provider tell trips the `answer_excludes` check and cases fail loudly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

from shejane_runtime.api_schemas import RUNTIME_MODEL_PATTERN

from .cases import CASES
from .driver import HttpRuntimeDriver
from .harness import evaluate, format_report, heuristic_judge, report_payload


def _required_model() -> str:
    model = os.environ.get("SHEJANE_EVAL_MODEL", "").strip()
    if len(model) > 128 or re.fullmatch(RUNTIME_MODEL_PATTERN, model) is None:
        raise ValueError(
            "set SHEJANE_EVAL_MODEL to a concrete local:<connection>:<model> selection"
        )
    return model


def main() -> int:
    base_url = os.environ.get("SHEJANE_EVAL_RUNTIME_URL", "http://127.0.0.1:17371")
    token = os.environ.get("SHEJANE_EVAL_TOKEN") or os.environ.get("SHEJANE_RUNTIME_TOKEN", "")
    if not token:
        print(
            "error: set SHEJANE_EVAL_TOKEN (or SHEJANE_RUNTIME_TOKEN) to the runtime bearer token",
            file=sys.stderr,
        )
        return 2

    try:
        model = _required_model()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    driver = HttpRuntimeDriver(base_url, token)
    cases = [replace(case, model=model) for case in CASES]

    async def run_eval():
        return await driver.runtime_version(), await evaluate(cases, driver, heuristic_judge)

    runtime_version, report = asyncio.run(run_eval())
    print(format_report(report))
    report_path = Path(os.environ.get("SHEJANE_EVAL_REPORT", "../.tmp/eval-report.json"))
    baseline_path = os.environ.get("SHEJANE_EVAL_BASELINE")
    baseline = None
    if baseline_path and Path(baseline_path).is_file():
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    payload = report_payload(
        report,
        runtime_version=runtime_version,
        model=model,
        baseline=baseline,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report={report_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
