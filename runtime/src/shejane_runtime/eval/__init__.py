"""Agent eval harness — golden trajectories + heuristic / LLM-judge scoring.

See harness.py for the design. `python -m shejane_runtime.eval` runs the seed cases
against a live runtime (heuristic judge); the harness pieces are unit-tested
hermetically in tests/test_eval_harness.py.
"""

from .harness import (
    CaseResult,
    Driver,
    EvalCase,
    EvalReport,
    Expectation,
    Judge,
    Judgment,
    Trajectory,
    evaluate,
    format_report,
    heuristic_judge,
    make_llm_judge,
    parse_judgment,
    report_payload,
)

__all__ = [
    "CaseResult",
    "Driver",
    "EvalCase",
    "EvalReport",
    "Expectation",
    "Judge",
    "Judgment",
    "Trajectory",
    "evaluate",
    "format_report",
    "heuristic_judge",
    "make_llm_judge",
    "parse_judgment",
    "report_payload",
]
