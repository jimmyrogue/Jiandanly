"""Eval harness — golden-trajectory scoring for the agent.

The agent has strong observability but (per the roadmap) almost no automated
quality signal: changing a prompt / model / middleware gives no way to tell if
trajectory quality moved. This harness closes that gap.

Shape (all pieces are pluggable so the harness logic is unit-testable without
a live agent or a paid LLM):

    cases ──▶ Driver.run(case) ──▶ Trajectory ──▶ judge(case, traj) ──▶ Judgment
                                                                   │
                                                          aggregate ▼
                                                              EvalReport

Two judges ship:
  • heuristic_judge — deterministic, objective checks (answer substrings,
    tools used and step budget). The dependable backbone; no LLM cost.
  • llm_judge — an LLM rates correctness/tool-choice/efficiency against the
    case rubric. Pluggable `complete` fn so tests inject a fake and nightly
    wires the real model.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Protocol


@dataclass
class Expectation:
    """Objective, checkable expectations for a case's trajectory."""

    # Case-insensitive substrings the final answer SHOULD contain.
    answer_contains: list[str] = field(default_factory=list)
    # Substrings the final answer must NOT contain (e.g. a mock-provider tell).
    answer_excludes: list[str] = field(default_factory=list)
    # Tool names expected to appear in the trajectory.
    tools_used: list[str] = field(default_factory=list)
    max_steps: int | None = None
    min_model_calls: int = 0
    min_input_tokens: int = 0
    min_output_tokens: int = 0
    # Relative workspace files whose final content must contain the value.
    files_contain: dict[str, str] = field(default_factory=dict)


@dataclass
class EvalCase:
    id: str
    goal: str
    expect: Expectation = field(default_factory=Expectation)
    model: str = ""
    workspace_path: str | None = None
    settings: dict | None = None
    workspace_files: dict[str, str] = field(default_factory=dict)
    permission_mode: str = "auto"
    permission_decision: str | None = None
    question_answers: list[str] = field(default_factory=list)
    approve_plans: bool = False
    # Free-text guidance for the LLM judge (ignored by the heuristic judge).
    rubric: str = ""


@dataclass
class Trajectory:
    """What one agent run produced — the unit a judge scores."""

    final_text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    steps: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed: bool = False
    error: str = ""
    terminal_status: str = ""
    event_counts: dict[str, int] = field(default_factory=dict)
    workspace_results: dict[str, str] = field(default_factory=dict)


@dataclass
class Judgment:
    correctness: float  # 0..1
    tool_choice: float  # 0..1
    efficiency: float  # 0..1
    passed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def overall(self) -> float:
        return round((self.correctness + self.tool_choice + self.efficiency) / 3, 3)


@dataclass
class CaseResult:
    case_id: str
    trajectory: Trajectory
    judgment: Judgment


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.judgment.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(1 for r in self.results if r.judgment.passed) / len(self.results), 3)

    @property
    def mean_overall(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(r.judgment.overall for r in self.results) / len(self.results), 3)


class Driver(Protocol):
    """Runs a case and returns its trajectory. Real impl hits the runtime;
    tests inject a fake."""

    async def run(self, case: EvalCase) -> Trajectory: ...


Judge = Callable[[EvalCase, Trajectory], Judgment]


def heuristic_judge(case: EvalCase, traj: Trajectory) -> Judgment:
    """Deterministic, objective scoring. No LLM. This is the reliable
    backbone — a case passes only when every objective expectation holds."""
    if traj.failed:
        return Judgment(
            0.0, 0.0, 0.0, passed=False, reasons=[f"run failed: {traj.error or 'unknown'}"]
        )

    reasons: list[str] = []
    text = traj.final_text.lower()

    contains = case.expect.answer_contains
    hits = sum(1 for s in contains if s.lower() in text)
    correctness = (hits / len(contains)) if contains else 1.0
    for s in contains:
        if s.lower() not in text:
            reasons.append(f"missing expected substring: {s!r}")
    for s in case.expect.answer_excludes:
        if s.lower() in text:
            correctness = 0.0
            reasons.append(f"forbidden substring present: {s!r}")

    want = case.expect.tools_used
    used = set(traj.tool_calls)
    thits = sum(1 for t in want if t in used)
    tool_choice = (thits / len(want)) if want else 1.0
    for t in want:
        if t not in used:
            reasons.append(f"expected tool not used: {t}")
    for path, expected in case.expect.files_contain.items():
        actual = traj.workspace_results.get(path)
        if actual is None:
            correctness = 0.0
            reasons.append(f"expected workspace file missing: {path}")
        elif expected not in actual:
            correctness = 0.0
            reasons.append(f"workspace file {path} missing expected content: {expected!r}")

    efficiency = 1.0
    if case.expect.max_steps is not None and traj.steps > case.expect.max_steps:
        efficiency = 0.5
        reasons.append(f"steps {traj.steps} over budget {case.expect.max_steps}")
    for actual, minimum, label in (
        (traj.model_calls, case.expect.min_model_calls, "model calls"),
        (traj.input_tokens, case.expect.min_input_tokens, "input tokens"),
        (traj.output_tokens, case.expect.min_output_tokens, "output tokens"),
    ):
        if actual < minimum:
            correctness = 0.0
            reasons.append(f"{label} {actual} below required {minimum}")
    passed = correctness >= 0.999 and tool_choice >= 0.999 and efficiency >= 0.5
    return Judgment(
        round(correctness, 3), round(tool_choice, 3), round(efficiency, 3), passed, reasons
    )


def build_judge_prompt(case: EvalCase, traj: Trajectory) -> str:
    """The LLM-judge prompt. Asks for strict JSON so parsing is robust."""
    return (
        "You are a strict evaluator of an AI agent's run. Score 0.0–1.0 on each "
        "axis and return ONLY JSON: "
        '{"correctness":0-1,"tool_choice":0-1,"efficiency":0-1,"reasons":["..."]}.\n\n'
        f"TASK GOAL:\n{case.goal}\n\n"
        f"RUBRIC:\n{case.rubric or '(use general judgement)'}\n\n"
        f"AGENT FINAL ANSWER:\n{traj.final_text}\n\n"
        f"TOOLS USED: {', '.join(traj.tool_calls) or '(none)'}\n"
        f"STEPS: {traj.steps}\n"
    )


def parse_judgment(raw: str, pass_threshold: float = 0.7) -> Judgment:
    """Parse an LLM judge's JSON reply into a Judgment. Tolerant of fenced
    code blocks / surrounding prose."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return Judgment(0.0, 0.0, 0.0, passed=False, reasons=["judge returned no JSON"])
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return Judgment(0.0, 0.0, 0.0, passed=False, reasons=["judge JSON parse failed"])

    def clamp(v: object) -> float:
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    correctness = clamp(data.get("correctness"))
    tool_choice = clamp(data.get("tool_choice"))
    efficiency = clamp(data.get("efficiency"))
    reasons = [str(r) for r in data.get("reasons", []) if str(r).strip()]
    overall = (correctness + tool_choice + efficiency) / 3
    return Judgment(
        correctness, tool_choice, efficiency, passed=overall >= pass_threshold, reasons=reasons
    )


def make_llm_judge(complete: Callable[[str], str], pass_threshold: float = 0.7) -> Judge:
    """Build an LLM-backed judge from a completion fn (prompt -> reply text)."""

    def judge(case: EvalCase, traj: Trajectory) -> Judgment:
        if traj.failed:
            return Judgment(
                0.0, 0.0, 0.0, passed=False, reasons=[f"run failed: {traj.error or 'unknown'}"]
            )
        return parse_judgment(complete(build_judge_prompt(case, traj)), pass_threshold)

    return judge


async def evaluate(
    cases: Iterable[EvalCase], driver: Driver, judge: Judge = heuristic_judge
) -> EvalReport:
    """Run every case through the driver and score it. Driver/judge failures
    are captured as a failed case rather than aborting the whole report."""
    results: list[CaseResult] = []
    for case in cases:
        try:
            traj = await driver.run(case)
        except Exception as exc:
            # A driver crash is a failed case, not a harness crash.
            traj = Trajectory(failed=True, error=f"{type(exc).__name__}: {exc}")
        results.append(CaseResult(case.id, traj, judge(case, traj)))
    return results_report(results)


def results_report(results: list[CaseResult]) -> EvalReport:
    return EvalReport(results)


def format_report(report: EvalReport) -> str:
    """A compact text table for CLI / CI logs."""
    lines = ["", "eval results", "─" * 64]
    for r in report.results:
        mark = "PASS" if r.judgment.passed else "FAIL"
        lines.append(
            f"[{mark}] {r.case_id:<24} "
            f"correct={r.judgment.correctness:.2f} tools={r.judgment.tool_choice:.2f} "
            f"eff={r.judgment.efficiency:.2f} overall={r.judgment.overall:.2f}"
        )
        for reason in r.judgment.reasons:
            lines.append(f"        ↳ {reason}")
    lines.append("─" * 64)
    lines.append(
        f"pass_rate={report.pass_rate:.0%}  mean_overall={report.mean_overall:.2f}  ({len(report.results)} cases)"
    )
    return "\n".join(lines)


def report_payload(
    report: EvalReport,
    *,
    runtime_version: str,
    model: str,
    baseline: dict | None = None,
    generated_at: str | None = None,
) -> dict:
    results = [
        {
            "case_id": result.case_id,
            "trajectory": asdict(result.trajectory),
            "judgment": {**asdict(result.judgment), "overall": result.judgment.overall},
        }
        for result in report.results
    ]
    previous_results = {
        str(result.get("case_id")): result
        for result in (baseline or {}).get("results", [])
        if isinstance(result, dict) and result.get("case_id")
    }
    changed = []
    for result in results:
        previous = previous_results.get(result["case_id"])
        if previous is None:
            continue
        previous_passed = bool((previous.get("judgment") or {}).get("passed"))
        passed = bool(result["judgment"]["passed"])
        if previous_passed != passed:
            changed.append(
                {
                    "case_id": result["case_id"],
                    "previous_passed": previous_passed,
                    "passed": passed,
                }
            )
    previous_rate = (baseline or {}).get("summary", {}).get("pass_rate")
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "runtime_version": runtime_version,
        "model": model,
        "summary": {
            "passed": report.passed,
            "pass_rate": report.pass_rate,
            "mean_overall": report.mean_overall,
            "case_count": len(results),
        },
        "comparison": {
            "baseline_pass_rate": previous_rate,
            "pass_rate_delta": (
                round(report.pass_rate - float(previous_rate), 3)
                if isinstance(previous_rate, (int, float))
                else None
            ),
            "changed_cases": changed,
        },
        "results": results,
    }


# Re-exported for the real driver module's type hints without importing httpx here.
AsyncRun = Callable[[EvalCase], Awaitable[Trajectory]]
