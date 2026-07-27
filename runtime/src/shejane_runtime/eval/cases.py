"""Seed golden-trajectory cases.

Intentionally small + objective so the heuristic judge gives a dependable
signal. Grow this list as regressions are found — each fixed bug should leave
a case behind. Expectations are deliberately loose where a real LLM's exact
wording varies (substring checks, not equality).
"""

from __future__ import annotations

from .harness import EvalCase, Expectation

CASES: list[EvalCase] = [
    EvalCase(
        id="direct-math",
        goal="请只用一句中文回答:2+2 等于几?",
        expect=Expectation(
            answer_contains=["4"],
            # A real provider must answer — never the mock fallback string.
            answer_excludes=["Mock SheJane"],
            max_steps=3,
            min_model_calls=1,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="A correct, concise one-sentence answer stating the sum is 4.",
    ),
    EvalCase(
        id="direct-factual",
        goal="Answer in one short English sentence: what is the capital of Japan?",
        expect=Expectation(
            answer_contains=["Tokyo"],
            answer_excludes=["Mock SheJane"],
            max_steps=3,
            min_model_calls=1,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Names Tokyo as the capital of Japan.",
    ),
    EvalCase(
        id="workspace-read-tool",
        goal=(
            "必须调用 read_file 工具读取工作区 README.md，然后回答其中的项目名称；不要凭记忆回答。"
        ),
        workspace_files={"README.md": "Project: SheJane\nPurpose: local agentic chat runtime.\n"},
        expect=Expectation(
            answer_contains=["SheJane"],
            answer_excludes=["Mock SheJane"],
            tools_used=["read_file"],
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Reads README.md with the tool, then identifies SheJane and its purpose.",
    ),
    EvalCase(
        id="workspace-write-result",
        goal="调用 write_file 在工作区创建 result.txt，内容必须是 FILE_RESULT_OK，然后确认完成。",
        workspace_files={"source.txt": "eval seed\n"},
        expect=Expectation(
            answer_contains=["完成"],
            answer_excludes=["Mock SheJane"],
            tools_used=["write_file"],
            files_contain={"result.txt": "FILE_RESULT_OK"},
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Creates result.txt with the exact requested content and confirms completion.",
    ),
    EvalCase(
        id="planning-tool",
        goal="先调用 write_todos 记录一个待办，再用一句话回答 PLAN_RESULT_OK。",
        expect=Expectation(
            answer_contains=["PLAN_RESULT_OK"],
            answer_excludes=["Mock SheJane"],
            tools_used=["write_todos"],
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Uses the planning tool before returning PLAN_RESULT_OK.",
    ),
    EvalCase(
        id="agents-instruction",
        goal="用一句话确认你已读取工作区指令。",
        workspace_files={
            "AGENTS.md": "For every response, include the exact token AGENT_RULE_OK.\n"
        },
        expect=Expectation(
            answer_contains=["AGENT_RULE_OK"],
            answer_excludes=["Mock SheJane"],
            max_steps=3,
            min_model_calls=1,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Obeys the Runtime-injected AGENTS.md instruction.",
    ),
    EvalCase(
        id="explicit-memory-write",
        goal="请记住这个事实：eval-memory-2718。必须调用 memory.write，然后确认该事实。",
        expect=Expectation(
            answer_contains=["eval-memory-2718"],
            answer_excludes=["Mock SheJane"],
            tools_used=["memory.write"],
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Writes only the explicitly authorized fact to memory and confirms it.",
    ),
    EvalCase(
        id="subagent-delegation",
        goal=(
            "必须调用 task 工具委派给 writer 子 Agent，让它返回 SUBAGENT_RESULT_OK；"
            "收到结果后原样回答该标记。"
        ),
        expect=Expectation(
            answer_contains=["SUBAGENT_RESULT_OK"],
            answer_excludes=["Mock SheJane"],
            tools_used=["task"],
            max_steps=5,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Delegates once to a writer subagent and returns its exact result token.",
    ),
    EvalCase(
        id="permission-wait-resume",
        goal="调用 write_file 创建 approved.txt，内容是 PERMISSION_RESULT_OK，然后确认完成。",
        workspace_files={"source.txt": "permission seed\n"},
        permission_mode="ask",
        permission_decision="approve",
        expect=Expectation(
            answer_contains=["完成"],
            answer_excludes=["Mock SheJane"],
            tools_used=["write_file"],
            files_contain={"approved.txt": "PERMISSION_RESULT_OK"},
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Waits for approval, resumes, writes the approved file, and finishes.",
    ),
    EvalCase(
        id="question-wait-resume",
        goal=(
            "必须调用 user.ask 询问选择 Option A 或 Option B；收到回答后，"
            "用 QUESTION_RESULT_OK 和所选项回答。"
        ),
        question_answers=["Option B"],
        expect=Expectation(
            answer_contains=["QUESTION_RESULT_OK", "Option B"],
            answer_excludes=["Mock SheJane"],
            tools_used=["user.ask"],
            max_steps=4,
            min_model_calls=2,
            min_input_tokens=1,
            min_output_tokens=1,
        ),
        rubric="Asks the user, resumes from the durable answer, and reports Option B.",
    ),
]
