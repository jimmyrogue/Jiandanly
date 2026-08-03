"""Tests for the Runtime-owned prompt construction stack."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from shejane_runtime.agent.context_builder import (
    ContextBuilder,
    ContextLayer,
    RuntimeContext,
    reset_prompt_cache_for_tests,
)

# ---- _layer_developer ------------------------------------------------


def test_runtime_identity_and_safety_precede_developer_instructions(tmp_path: Path) -> None:
    identity_file = tmp_path / "identity.md"
    identity_file.write_text("IDENTITY_AND_SAFETY_MARKER", encoding="utf-8")
    developer_file = tmp_path / "developer.md"
    developer_file.write_text("DEVELOPER_MARKER", encoding="utf-8")

    builder = ContextBuilder(
        identity_prompt_path=identity_file,
        developer_prompt_path=developer_file,
    )
    result = builder.build(runtime=RuntimeContext())

    assert result.index("IDENTITY_AND_SAFETY_MARKER") < result.index("DEVELOPER_MARKER")


def test_developer_layer_loaded_from_disk(tmp_path: Path) -> None:
    """The developer layer reads its content from a markdown file so
    we can edit prompt copy without touching code."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("# Developer\n\n你必须始终使用工具。", encoding="utf-8")

    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext())

    assert "你必须始终使用工具" in result
    assert "# Developer" in result


def test_developer_layer_missing_file_does_not_crash(tmp_path: Path) -> None:
    """Missing developer.md should log a warning but produce a non-empty
    prompt (the runtime context still flows through)."""
    builder = ContextBuilder(developer_prompt_path=tmp_path / "does-not-exist.md")
    result = builder.build(
        runtime=RuntimeContext(workspace_root="/tmp/ws"),
    )
    assert "运行时上下文" in result  # runtime layer still rendered


def test_developer_layer_cached_until_reset(tmp_path: Path) -> None:
    """The cache is intentional — we don't want file I/O per run.
    `reset_prompt_cache_for_tests()` only affects the module-level
    default builder, not custom-constructed ones; verify the
    per-instance cache too."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("first version", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    builder.build(runtime=RuntimeContext())

    # Edit on disk; the cache should hold the first version until
    # explicitly cleared.
    prompt_file.write_text("second version", encoding="utf-8")
    cached_result = builder.build(runtime=RuntimeContext())
    assert "first version" in cached_result
    assert "second version" not in cached_result


# ---- _layer_skills ---------------------------------------------------


def test_skills_layer_renders_when_skills_enabled(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(enabled_skills=["write", "debug"]),
    )

    assert "用户启用的 Skill" in result
    assert "- write" in result
    assert "- debug" in result


def test_skills_layer_absent_when_no_skills(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext(enabled_skills=[]))

    assert "用户启用的 Skill" not in result


# ---- _layer_runtime_context ------------------------------------------


def test_runtime_context_includes_workspace_when_set(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(workspace_root="/Users/me/projects/app"),
    )

    assert "/Users/me/projects/app" in result
    assert "工作区根目录" in result


def test_runtime_context_marks_unauthorized_when_no_workspace(tmp_path: Path) -> None:
    """No workspace → the model should know it can't touch real files.
    This is load-bearing safety info; loudly visible in the prompt."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext(workspace_root=None))

    assert "未授权" in result
    assert "虚拟文件系统" in result


def test_runtime_context_renders_time(tmp_path: Path) -> None:
    """Injected `now` flows into the rendered timestamp so the model
    knows what 'now' means without us depending on system clock. We
    assert on year+month rather than the exact day because the render
    converts to the host machine's local TZ (a UTC datetime at 19:30
    becomes the next day in UTC+8) — that conversion is the intended
    behavior so the user sees their wall-clock time."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    fixed_now = _dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=_dt.UTC)

    result = builder.build(
        runtime=RuntimeContext(workspace_root="/ws", now=fixed_now),
    )

    assert "2026-05" in result, f"expected 2026-05 in rendered context, got: {result}"


def test_runtime_context_locale_passed_through(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext(locale="zh"))

    assert "用户语言偏好: zh" in result


def test_state_layer_renders_repair_workflow_context(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(
            repair_intent=True,
            repair_attempt=2,
            repair_max_attempts=3,
            repair_source_run_id="run_original",
            repair_source_message_id="msg_original",
            repair_failure_category="validation",
            repair_failure_action_kind="repair",
        ),
    )

    assert "修复工作流" in result
    assert "第 2/3 次" in result
    assert "run_original" in result
    assert "msg_original" in result
    assert "validation / repair" in result
    assert "修复后必须验证" in result


def test_state_layer_renders_retry_workflow_context(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(
            retry_intent=True,
            retry_attempt=2,
            retry_source_run_id="run_failed",
            retry_source_message_id="msg_failed",
            retry_failure_category="auth",
            retry_failure_action_kind="user_action",
        ),
    )

    assert "恢复重试" in result
    assert "第 2 次" in result
    assert "run_failed" in result
    assert "msg_failed" in result
    assert "auth / user_action" in result
    assert "避免盲目重复" in result


# ---- assembly + priority ordering ------------------------------------


def test_layers_are_ordered_developer_before_skills_before_runtime(
    tmp_path: Path,
) -> None:
    """Priority stack must be respected: developer (30) < skills (40)
    < runtime (55). Whoever's earlier in the text wins precedence in
    the model's attention."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("DEVELOPER_MARKER", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(
            workspace_root="/ws",
            enabled_skills=["skill_a"],
        ),
    )

    dev_pos = result.index("DEVELOPER_MARKER")
    skill_pos = result.index("用户启用的 Skill")
    runtime_pos = result.index("运行时上下文")
    assert dev_pos < skill_pos < runtime_pos


# ---- budget enforcement ----------------------------------------------


def test_non_truncatable_layer_overflow_raises(tmp_path: Path) -> None:
    """Developer instructions are load-bearing — if they exceed their
    cap, fail loudly so dev notices rather than shipping a silently
    truncated agent."""
    huge = "X" * (32 * 1024)
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text(huge, encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)

    with pytest.raises(ValueError, match="non-truncatable"):
        builder.build(runtime=RuntimeContext())


def test_per_layer_truncation_marker(tmp_path: Path) -> None:
    """A truncatable layer over its cap gets cut with a marker."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    # Force the skills layer over its 1024-char cap.
    huge_skills = [f"skill_{i:04d}" for i in range(200)]
    result = builder.build(
        runtime=RuntimeContext(enabled_skills=huge_skills),
    )

    assert "[内容过长已截断]" in result


def test_total_budget_trims_trailing_layers(tmp_path: Path) -> None:
    """If the assembled total exceeds the total budget, trailing
    truncatable layers shrink first; the developer layer stays whole."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("DEV-CONTENT-MARKER\n" + ("PAD\n" * 100), encoding="utf-8")
    identity_file = tmp_path / "identity.md"
    identity_file.write_text("IDENTITY", encoding="utf-8")
    builder = ContextBuilder(
        identity_prompt_path=identity_file,
        developer_prompt_path=prompt_file,
        total_budget_chars=600,  # very tight
    )
    result = builder.build(
        runtime=RuntimeContext(
            workspace_root="/ws",
            enabled_skills=["a", "b", "c"],
        ),
    )

    # Developer marker is preserved (non-truncatable).
    assert "DEV-CONTENT-MARKER" in result
    # Total fits within budget.
    assert len(result) <= 600 + len("\n…[内容过长已截断]") * 2


# ---- render_snapshot -------------------------------------------------


def test_render_snapshot_returns_per_layer_dict(tmp_path: Path) -> None:
    """Snapshot API for tests + debug — keys are layer names, values
    are the rendered content for that layer."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    snap = builder.render_snapshot(
        runtime=RuntimeContext(workspace_root="/ws", enabled_skills=["x"]),
    )

    # All five layers always show up in the snapshot (some may be empty
    # when their inputs are not provided — the snapshot exposes that
    # too, which is useful for debugging "why is my <task> empty?").
    assert set(snap.keys()) == {
        "identity_safety",
        "developer",
        "agent_role",
        "task",
        "skills",
        "state",
        "runtime_context",
    }
    assert "dev" in snap["developer"]
    assert "- x" in snap["skills"]
    assert "/ws" in snap["runtime_context"]


# ---- task layer ------------------------------------------------------


def test_task_layer_renders_goal(tmp_path: Path) -> None:
    """Layer 35 echoes the user's goal so the model has a stable anchor
    in long tool-call chains where the original user message is far
    above in the conversation."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(task_goal="重构这个登录模块"),
    )

    assert "<task>" in result
    assert "重构这个登录模块" in result
    assert "</task>" in result


def test_task_layer_absent_when_goal_empty(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext(task_goal=""))
    assert "<task>" not in result


def test_task_layer_caps_long_goal(tmp_path: Path) -> None:
    """A pasted log dump in the goal would blow the prompt budget;
    cap at 800 chars and mark the truncation visibly."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    long_goal = "X" * 2000
    result = builder.build(runtime=RuntimeContext(task_goal=long_goal))

    assert "[已截断]" in result
    # Goal portion + a few chars for tags + marker, well under 2000.
    assert result.count("X") <= 800


# ---- state layer -----------------------------------------------------


def test_state_layer_renders_mode_and_turn_count(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(mode="deep", turn_count=5, required_tools=("image.generate",)),
    )

    assert "<state>" in result
    assert "当前模式: deep" in result
    assert "第 5 轮" in result
    assert "必须调用工具: image.generate" in result


def test_state_layer_absent_when_no_state_fields(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(runtime=RuntimeContext())
    assert "<state>" not in result


def test_durable_child_role_and_lineage_are_model_visible(tmp_path: Path) -> None:
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("dev", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(
            run_kind="child",
            root_run_id="root-1",
            agent_definition_id="subagent:researcher",
            agent_definition_version="sha256:v1",
            agent_role_prompt="Only collect primary evidence.",
        )
    )

    assert "<agent-role>\nOnly collect primary evidence.\n</agent-role>" in result
    assert "subagent:researcher@sha256:v1" in result
    assert "协作 root Run: root-1" in result


# ---- priority ordering with new layers -------------------------------


def test_full_layer_order_developer_task_skills_state_runtime(tmp_path: Path) -> None:
    """All five layers in priority order. If someone reshuffles
    priorities without updating intent, this test catches it."""
    prompt_file = tmp_path / "dev.md"
    prompt_file.write_text("DEVELOPER_MARKER", encoding="utf-8")
    builder = ContextBuilder(developer_prompt_path=prompt_file)
    result = builder.build(
        runtime=RuntimeContext(
            workspace_root="/ws",
            enabled_skills=["skill_a"],
            task_goal="TASK_MARKER",
            mode="fast",
            turn_count=3,
        ),
    )

    dev_pos = result.index("DEVELOPER_MARKER")
    task_pos = result.index("TASK_MARKER")
    skills_pos = result.index("用户启用的 Skill")
    state_pos = result.index("<state>")
    runtime_pos = result.index("运行时上下文")
    assert dev_pos < task_pos < skills_pos < state_pos < runtime_pos


# ---- module-level cache reset hook -----------------------------------


def test_reset_prompt_cache_for_tests_is_callable() -> None:
    """Smoke check the test hook exists and is callable — used by other
    test modules that swap prompt files mid-suite."""
    reset_prompt_cache_for_tests()


# ---- ContextLayer dataclass invariants -------------------------------


def test_context_layer_immutable() -> None:
    """ContextLayer is frozen so layer authors can't mutate priority or
    truncation flags after construction (would break assembly order)."""
    from dataclasses import FrozenInstanceError

    layer = ContextLayer(name="x", priority=10, content="hi")
    with pytest.raises(FrozenInstanceError):
        layer.priority = 20  # type: ignore[misc]
