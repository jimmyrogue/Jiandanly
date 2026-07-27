"""Hermetic tests for the eval harness — no live agent, no paid LLM.

Covers the scoring + aggregation + gating logic with a fake driver and a fake
LLM completion fn, so the harness itself is regression-protected even though
the real eval (`python -m shejane_runtime.eval`) needs a running runtime.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from shejane_runtime.eval import (
    EvalCase,
    Expectation,
    Trajectory,
    evaluate,
    format_report,
    heuristic_judge,
    make_llm_judge,
    parse_judgment,
    report_payload,
)
from shejane_runtime.eval.__main__ import _required_model
from shejane_runtime.eval.driver import HttpRuntimeDriver


def _case(**kw) -> EvalCase:
    kw.setdefault("id", "c")
    kw.setdefault("goal", "g")
    return EvalCase(**kw)


def test_heuristic_passes_when_all_expectations_hold() -> None:
    case = _case(
        expect=Expectation(answer_contains=["Tokyo"], tools_used=["web.search"], max_steps=5)
    )
    traj = Trajectory(final_text="The capital is Tokyo.", tool_calls=["web.search"], steps=2)
    j = heuristic_judge(case, traj)
    assert j.passed
    assert j.correctness == 1.0 and j.tool_choice == 1.0 and j.efficiency == 1.0


def test_heuristic_fails_on_missing_substring() -> None:
    case = _case(expect=Expectation(answer_contains=["Tokyo"]))
    j = heuristic_judge(case, Trajectory(final_text="It's Osaka."))
    assert not j.passed
    assert any("Tokyo" in r for r in j.reasons)


def test_heuristic_zero_correctness_on_forbidden_substring() -> None:
    case = _case(expect=Expectation(answer_contains=["4"], answer_excludes=["Mock SheJane"]))
    j = heuristic_judge(case, Trajectory(final_text="Mock SheJane response: 4"))
    assert j.correctness == 0.0
    assert not j.passed


def test_heuristic_flags_missing_tool() -> None:
    case = _case(expect=Expectation(tools_used=["web.search"]))
    j = heuristic_judge(case, Trajectory(final_text="done", tool_calls=["web.fetch"]))
    assert j.tool_choice == 0.0
    assert not j.passed


def test_heuristic_penalizes_over_budget_steps() -> None:
    case = _case(expect=Expectation(max_steps=2))
    j = heuristic_judge(case, Trajectory(final_text="done", steps=9))
    assert j.efficiency == 0.5
    assert any("over budget" in r for r in j.reasons)


def test_heuristic_fails_a_failed_run() -> None:
    j = heuristic_judge(_case(), Trajectory(failed=True, error="boom"))
    assert not j.passed
    assert j.overall == 0.0


def test_heuristic_requires_metered_real_model_calls() -> None:
    case = _case(expect=Expectation(min_model_calls=1, min_input_tokens=1, min_output_tokens=1))
    assert heuristic_judge(
        case,
        Trajectory(model_calls=2, input_tokens=100, output_tokens=10),
    ).passed
    judgment = heuristic_judge(case, Trajectory())
    assert not judgment.passed
    assert any("model calls" in reason for reason in judgment.reasons)


def test_heuristic_checks_final_workspace_results() -> None:
    case = _case(expect=Expectation(files_contain={"result.txt": "READY"}))

    assert heuristic_judge(
        case,
        Trajectory(workspace_results={"result.txt": "STATUS=READY"}),
    ).passed
    assert not heuristic_judge(case, Trajectory()).passed


def test_evaluate_aggregates_pass_rate() -> None:
    cases = [
        _case(id="a", expect=Expectation(answer_contains=["x"])),
        _case(id="b", expect=Expectation(answer_contains=["y"])),
    ]
    trajs = {"a": Trajectory(final_text="has x"), "b": Trajectory(final_text="missing")}

    class FakeDriver:
        async def run(self, case: EvalCase) -> Trajectory:
            return trajs[case.id]

    report = asyncio.run(evaluate(cases, FakeDriver(), heuristic_judge))
    assert report.pass_rate == 0.5
    assert not report.passed
    assert {r.case_id for r in report.results} == {"a", "b"}


def test_evaluate_captures_driver_crash_as_failed_case() -> None:
    class BoomDriver:
        async def run(self, case: EvalCase) -> Trajectory:
            raise RuntimeError("runtime down")

    report = asyncio.run(evaluate([_case(id="z")], BoomDriver(), heuristic_judge))
    assert not report.passed
    assert report.results[0].trajectory.failed
    assert "runtime down" in report.results[0].trajectory.error


def test_http_driver_sends_the_strict_run_command(monkeypatch) -> None:
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests.append((request.url.path, json.loads(request.content)))
            if request.url.path.endswith("/workspaces"):
                return httpx.Response(200, json={"id": "workspace_eval"})
            return httpx.Response(200, json={"id": "run_eval"})
        return httpx.Response(
            200,
            content=b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("shejane_runtime.eval.driver.httpx.AsyncClient", PatchedClient)

    asyncio.run(
        HttpRuntimeDriver("http://runtime", "tok").run(
            _case(model="local:provider:model", workspace_path="/tmp/eval-workspace")
        )
    )

    assert requests[0] == (
        "/v1/workspaces",
        {"path": "/tmp/eval-workspace", "label": "eval:c"},
    )
    run_request = requests[1][1]
    assert run_request["model"] == "local:provider:model"
    assert run_request["workspace_path"] == "/tmp/eval-workspace"
    assert run_request["command_id"].startswith("cmd_eval_")
    assert run_request["client_message_id"].startswith("msg_eval_")
    assert "mode" not in run_request


def test_http_driver_resolves_waits_and_captures_workspace_results(monkeypatch) -> None:
    commands: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workspaces"):
            return httpx.Response(200, json={"id": "workspace_eval"})
        if request.url.path.endswith("/runs"):
            workspace = json.loads(request.content)["workspace_path"]
            with open(f"{workspace}/result.txt", "w", encoding="utf-8") as output:
                output.write("READY")
            return httpx.Response(200, json={"id": "run_eval"})
        if request.url.path.endswith("/commands"):
            commands.append(json.loads(request.content))
            return httpx.Response(200, json={"resolved": True, "resumed": True})
        events = [
            {
                "event_type": "permission.required",
                "payload": {"request_id": "perm_eval"},
            },
            {"event_type": "run.completed", "payload": {"final_text": "done"}},
        ]
        content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200,
            content=f"{content}data: [DONE]\n\n".encode(),
            headers={"content-type": "text/event-stream"},
        )

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("shejane_runtime.eval.driver.httpx.AsyncClient", PatchedClient)
    trajectory = asyncio.run(
        HttpRuntimeDriver("http://runtime", "tok").run(
            _case(
                workspace_files={"source.txt": "seed"},
                permission_mode="ask",
                permission_decision="approve",
                expect=Expectation(files_contain={"result.txt": "READY"}),
            )
        )
    )

    assert commands[0]["type"] == "permission.resolve"
    assert commands[0]["permission_id"] == "perm_eval"
    assert trajectory.workspace_results == {"result.txt": "READY"}
    assert trajectory.terminal_status == "completed"


def test_required_model_accepts_a_concrete_runtime_model(monkeypatch) -> None:
    monkeypatch.setenv("SHEJANE_EVAL_MODEL", "local:provider:model")
    assert _required_model() == "local:provider:model"


@pytest.mark.parametrize("value", [None, "fast", "local::model"])
def test_required_model_rejects_missing_or_legacy_alias(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("SHEJANE_EVAL_MODEL", raising=False)
    else:
        monkeypatch.setenv("SHEJANE_EVAL_MODEL", value)
    with pytest.raises(ValueError, match="SHEJANE_EVAL_MODEL"):
        _required_model()


def test_parse_judgment_handles_fenced_json() -> None:
    raw = '```json\n{"correctness":0.9,"tool_choice":0.8,"efficiency":1.0,"reasons":["ok"]}\n```'
    j = parse_judgment(raw)
    assert j.correctness == 0.9 and j.tool_choice == 0.8 and j.efficiency == 1.0
    assert j.passed  # overall 0.9 >= 0.7


def test_parse_judgment_handles_garbage() -> None:
    j = parse_judgment("the model rambled with no json")
    assert not j.passed
    assert j.correctness == 0.0


def test_llm_judge_uses_injected_completion() -> None:
    def fake_complete(prompt: str) -> str:
        assert "TASK GOAL" in prompt  # the rubric prompt was built
        return '{"correctness":1,"tool_choice":1,"efficiency":1,"reasons":[]}'

    judge = make_llm_judge(fake_complete)
    j = judge(_case(), Trajectory(final_text="anything"))
    assert j.passed and j.overall == 1.0


def test_format_report_renders_pass_and_fail() -> None:
    cases = [_case(id="ok", expect=Expectation(answer_contains=["x"]))]

    class D:
        async def run(self, case: EvalCase) -> Trajectory:
            return Trajectory(final_text="x")

    report = asyncio.run(evaluate(cases, D(), heuristic_judge))
    out = format_report(report)
    assert "PASS" in out and "pass_rate=100%" in out


def test_report_payload_records_trajectory_and_baseline_regressions() -> None:
    case = _case(id="regressed", expect=Expectation(answer_contains=["expected"]))

    class D:
        async def run(self, _case: EvalCase) -> Trajectory:
            return Trajectory(
                final_text="missing",
                terminal_status="completed",
                event_counts={"run.completed": 1},
                workspace_results={"result.txt": "actual"},
            )

    report = asyncio.run(evaluate([case], D(), heuristic_judge))
    payload = report_payload(
        report,
        runtime_version="1.2.3",
        model="local:provider:model",
        baseline={
            "summary": {"pass_rate": 1.0},
            "results": [{"case_id": "regressed", "judgment": {"passed": True}}],
        },
        generated_at="2026-07-26T00:00:00+00:00",
    )

    assert payload["runtime_version"] == "1.2.3"
    assert payload["comparison"]["pass_rate_delta"] == -1.0
    assert payload["comparison"]["changed_cases"] == [
        {"case_id": "regressed", "previous_passed": True, "passed": False}
    ]
    assert payload["results"][0]["trajectory"]["event_counts"] == {"run.completed": 1}
