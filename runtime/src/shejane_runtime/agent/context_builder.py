"""Context Engineering — assembles the Runtime-owned prompt stack that
`create_deep_agent(instructions=...)` consumes.

Prompt Stack Overview
---------------------
Tool definitions (Layer 20 wire form) come from LangChain's bind_tools
path automatically. Conversation history (Layer 60) and current user
message (Layer 70) flow through `agent.astream(messages)` and are not
prompt-string concerns.

```
Priority   Layer                          Owner
─────────────────────────────────────────────────────
0          Identity and safety baseline   Runtime — identity.md
20         Tool definitions               LangChain (auto)
30         Developer instructions         Runtime — developer.md zones
32         Durable child role             Runtime — frozen Agent definition
35         Current task                   Runtime — this file (per-run)
40         Active skills hint             Runtime — this file
45         Memory (AGENTS.md cascade)     Runtime — deepagents MemoryMiddleware
50         Run state                      Runtime — this file (per-run)
55         Runtime context                Runtime — this file (dynamic)
60         Conversation history           Runtime (passed through, last-N capped)
70         Current user message           User
```

Layers 35 and 50 are new in P0 of the Context Engineering rollout —
they materialize the "Task" and "State" zones from the article's
recommended skeleton ([Role & Policies] [Task] [State] [Evidence]
[Context] [Output]). They depend on per-run inputs and so live on
RuntimeContext, not in static markdown.

The output of `ContextBuilder.build()` is a single string passed as
`instructions=` to `create_deep_agent`. Deep Agents turns it into the
same leading SystemMessage for every configured BYOK model.

Budget Management
-----------------
Each layer has a per-layer token cap; total cap defaults to 8 KiB
(~2000 tokens, conservative). When a layer would exceed its cap, we
truncate from the *end* of that layer and append a marker. Layer 30
(developer instructions) is treated as load-bearing and never truncated
— if the cap is too small, the build raises rather than silently
producing a broken agent.

Reload semantics
----------------
The developer-instructions markdown file is read once at module import
and cached. To pick up edits during dev, restart the runtime (or call
`reset_prompt_cache_for_tests()`). This is intentional — we don't want
file I/O on every run.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import locale as _locale
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("shejane_runtime.agent.context_builder")

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_IDENTITY_PROMPT_PATH = _PROMPTS_DIR / "identity.md"
_DEVELOPER_PROMPT_PATH = _PROMPTS_DIR / "developer.md"

# Conservative defaults. Rough rule of thumb: 1 token ≈ 4 chars for English, ~2 chars
# for Chinese. 8 KiB → ~2k–4k tokens worth of system context.
_DEFAULT_TOTAL_BUDGET_CHARS = 8 * 1024
_TRUNCATION_MARKER = "\n…[内容过长已截断]"


class AsyncToolExecutionGate:
    """Fair shared/exclusive gate plus deterministic ordering within a batch."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._batch_next: dict[str, int] = {}

    @asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def write(self):
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writer and self._readers == 0)
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()

    @asynccontextmanager
    async def ordered(self, batch_key: str, position: int, completed_prefix: int = 0):
        async with self._condition:
            current = self._batch_next.get(batch_key, 0)
            if completed_prefix > current:
                self._batch_next[batch_key] = completed_prefix
            await self._condition.wait_for(lambda: self._batch_next.get(batch_key, 0) == position)
        completed = False
        try:
            yield
            completed = True
        finally:
            if completed:
                async with self._condition:
                    self._batch_next[batch_key] = position + 1
                    self._condition.notify_all()


@dataclass(frozen=True)
class ContextLayer:
    """One slot in the assembled prompt.

    `name` is for logs/snapshots, `priority` controls assembly order
    (lower = closer to the top), `content` is the rendered text, and
    `max_chars` is the per-layer budget. Layers below a priority of 50
    are considered "structural" (identity-adjacent, developer-controlled)
    and are never truncated — if their content blows the budget, the
    build fails loudly so we notice during dev rather than silently
    shipping a broken agent.
    """

    name: str
    priority: int
    content: str
    max_chars: int = 4096
    truncatable: bool = True


@dataclass
class RuntimeContext:
    """Per-run inputs that feed Layer 35 (task), Layer 50 (state) and
    Layer 55 (runtime context).

    All fields are optional; missing fields just omit the corresponding
    rendered line instead of guessing a default. The dataclass-as-bag
    shape is intentional — callers from many layers should not need to
    learn a long positional signature.
    """

    # Harness-only dependencies. They are available to middleware and tools,
    # but ContextBuilder never renders them into the model prompt.
    run_id: str | None = None
    principal_id: str | None = None
    store: object | None = None
    steering_emit: object | None = None
    backend: object | None = None
    model: object | None = None
    approval_model: object | None = None
    clarification_model: object | None = None
    completion_model: object | None = None
    title_model: object | None = None
    dynamic_tools: dict[str, object] = field(default_factory=dict)
    tool_registry: dict[str, object] = field(default_factory=dict)
    capability_bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    required_tools: tuple[str, ...] = ()
    memory_enabled: bool = True
    subagents_enabled: bool = True
    child_run_control: object | None = None
    child_agent_definitions: dict[str, dict[str, object]] = field(default_factory=dict)
    agent_mailbox_control: object | None = None
    # Trusted ingress capability derived from the real top-level user input.
    # Tools may inspect it, but it is never rendered into the model prompt.
    memory_write_facts: tuple[str, ...] = ()
    graph_definition_id: str | None = None
    plugin_catalog_hash: str | None = None
    plugin_lease: object | None = None
    plugin_inputs: tuple[dict[str, object], ...] = ()
    plugin_tool_versions: dict[str, str] = field(default_factory=dict)
    execution_attempt_id: str | None = None
    permission_mode: str = "ask"
    # Shared by parent + subagents. Consequential tool calls acquire this
    # lock so LangChain's parallel ToolNode cannot race workspace/external
    # mutations. Read-only calls and subagent orchestration remain parallel.
    tool_mutation_lock: object = field(default_factory=AsyncToolExecutionGate)
    outbound_is_external: bool = False
    outbound_pii_types: tuple[str, ...] = ()
    outbound_secrets: tuple[str, ...] = ()

    # Layer 55 — model-visible runtime context
    workspace_root: str | None = None
    attachments: tuple[str, ...] = ()
    locale: str | None = None
    enabled_skills: list[str] = field(default_factory=list)
    now: _dt.datetime | None = None  # injected for tests; defaults to UTC now

    # Layer 35 — current task. The goal the user sent for THIS run.
    # Echoed in the system context so it survives long histories and
    # tool-call chains even if the user's original message scrolls out
    # of the model's attention window.
    task_goal: str | None = None
    agent_role_prompt: str | None = None
    allowed_tool_names: tuple[str, ...] = ()

    # Layer 50 — run state. These are the things the model can't see
    # but needs to know about the current run.
    mode: str | None = None  # Runtime model selection stored with the run
    run_kind: str = "turn"
    root_run_id: str | None = None
    agent_definition_id: str = "shejane.default"
    agent_definition_version: str = "1"
    collaboration_depth: int = 0
    turn_count: int | None = None  # message count incl. current turn
    clarification_count: int = 0
    repair_intent: bool = False
    repair_attempt: int | None = None
    repair_max_attempts: int | None = None
    repair_source_run_id: str | None = None
    repair_source_message_id: str | None = None
    repair_failure_category: str | None = None
    repair_failure_action_kind: str | None = None
    retry_intent: bool = False
    retry_attempt: int | None = None
    retry_source_run_id: str | None = None
    retry_source_message_id: str | None = None
    retry_failure_category: str | None = None
    retry_failure_action_kind: str | None = None
    recovery_answered_questions: tuple[tuple[str, tuple[str, ...]], ...] = ()


class ContextBuilder:
    """Assembles the complete Runtime-owned prompt stack.

    Stateless: a single instance can build many prompts. Tests
    construct with overridden paths/budgets.
    """

    def __init__(
        self,
        *,
        identity_prompt_path: Path = _IDENTITY_PROMPT_PATH,
        developer_prompt_path: Path = _DEVELOPER_PROMPT_PATH,
        total_budget_chars: int = _DEFAULT_TOTAL_BUDGET_CHARS,
    ) -> None:
        self._identity_prompt_path = identity_prompt_path
        self._developer_prompt_path = developer_prompt_path
        self._total_budget_chars = total_budget_chars
        self._identity_cache: str | None = None
        self._developer_cache: str | None = None

    # ---- public API ----------------------------------------------------

    def build(self, *, runtime: RuntimeContext) -> str:
        """Return the assembled instructions string for create_deep_agent.

        Layers are concatenated in priority order with `\\n\\n` between
        them. The result is what deepagents sees as `instructions=`.
        """
        layers = self._all_layers(runtime)
        layers = [layer for layer in layers if layer.content.strip()]
        return self._assemble(layers)

    def render_snapshot(self, *, runtime: RuntimeContext) -> dict[str, str]:
        """Return a {layer_name: content} mapping for tests and debug
        dumps. Useful for assertion-by-layer rather than substring
        search on the assembled blob."""
        return {layer.name: layer.content for layer in self._all_layers(runtime)}

    def _all_layers(self, runtime: RuntimeContext) -> list[ContextLayer]:
        """Construct every layer for this run, in declaration order.
        Sorting by priority happens in `_assemble`; this just lists them."""
        return [
            self._layer_identity_safety(),
            self._layer_developer(),
            self._layer_agent_role(runtime.agent_role_prompt),
            self._layer_task(runtime.task_goal),
            self._layer_skills(runtime.enabled_skills),
            self._layer_state(runtime),
            self._layer_runtime_context(runtime),
        ]

    # ---- layer builders ------------------------------------------------

    def _layer_identity_safety(self) -> ContextLayer:
        """Layer 0 — provider-independent identity and safety policy."""
        if self._identity_cache is None:
            try:
                self._identity_cache = self._identity_prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"identity and safety prompt is unavailable: {self._identity_prompt_path}"
                ) from exc
        return ContextLayer(
            name="identity_safety",
            priority=0,
            content=self._identity_cache,
            max_chars=2048,
            truncatable=False,
        )

    def _layer_developer(self) -> ContextLayer:
        """Layer 30 — agent behavior guidance (file-loaded, non-truncatable).

        Loaded once and cached. To refresh during dev, restart the runtime.
        """
        if self._developer_cache is None:
            try:
                self._developer_cache = self._developer_prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.error(
                    "developer prompt file unreadable at %s: %s",
                    self._developer_prompt_path,
                    exc,
                )
                self._developer_cache = ""
        return ContextLayer(
            name="developer",
            priority=30,
            content=self._developer_cache,
            max_chars=8 * 1024,
            truncatable=False,
        )

    def _layer_task(self, task_goal: str | None) -> ContextLayer:
        """Layer 35 — the current user task, echoed into the system
        context.

        Why echo: in long, tool-heavy runs the user's original message
        ends up far up the conversation. Restating it here gives the
        model a stable anchor regardless of how much intermediate
        tool-call chatter has piled up. We cap the goal at 800 chars
        so a pasted log dump in the prompt doesn't blow the budget.
        """
        if not task_goal or not task_goal.strip():
            return ContextLayer(name="task", priority=35, content="")
        goal = task_goal.strip()
        if len(goal) > 800:
            goal = goal[:800] + "…[已截断]"
        content = "<task>\n" + goal + "\n</task>"
        return ContextLayer(
            name="task",
            priority=35,
            content=content,
            max_chars=1024,
        )

    def _layer_agent_role(self, role_prompt: str | None) -> ContextLayer:
        if not role_prompt or not role_prompt.strip():
            return ContextLayer(name="agent_role", priority=32, content="")
        return ContextLayer(
            name="agent_role",
            priority=32,
            content="<agent-role>\n" + role_prompt.strip() + "\n</agent-role>",
            max_chars=8 * 1024,
            truncatable=False,
        )

    def _layer_state(self, runtime: RuntimeContext) -> ContextLayer:
        """Layer 50 — current run state. Things the model can't observe
        but should know: which model tier the user picked and how many
        turns we're into the conversation.

        Distinct from runtime_context (Layer 55): state describes the
        RUN, runtime_context describes the ENVIRONMENT.
        """
        bullets: list[str] = []
        if runtime.mode:
            bullets.append(f"- 当前模式: {runtime.mode}")
        if runtime.run_kind == "child":
            bullets.append(
                "- 当前身份: Runtime 托管的 durable child Run "
                f"({runtime.agent_definition_id}@{runtime.agent_definition_version})"
            )
            if runtime.root_run_id:
                bullets.append(f"- 协作 root Run: {runtime.root_run_id}")
        if runtime.turn_count is not None:
            bullets.append(f"- 对话轮次: 第 {runtime.turn_count} 轮")
        if runtime.required_tools:
            bullets.append(
                "- 本次必须调用工具: "
                + ", ".join(runtime.required_tools)
                + "。如果工具失败或被拒绝，必须如实说明，不得声称成功。"
            )
        if runtime.repair_intent:
            attempt = runtime.repair_attempt if runtime.repair_attempt is not None else 1
            max_attempts = runtime.repair_max_attempts if runtime.repair_max_attempts else "?"
            bullets.append(f"- 修复工作流: 第 {attempt}/{max_attempts} 次修复尝试")
            if runtime.repair_source_run_id:
                bullets.append(f"- 来源 run: {runtime.repair_source_run_id}")
            if runtime.repair_source_message_id:
                bullets.append(f"- 来源消息: {runtime.repair_source_message_id}")
            if runtime.repair_failure_category or runtime.repair_failure_action_kind:
                category = runtime.repair_failure_category or "unknown"
                action = runtime.repair_failure_action_kind or "unknown"
                bullets.append(f"- 原失败分类: {category} / {action}")
            bullets.append(
                "- 修复要求: 不要机械重复上一次失败路径；先定位根因，修复后必须验证。"
                "如果无法安全修复，说明明确阻塞和需要用户提供的信息。"
            )
        if runtime.retry_intent:
            attempt = runtime.retry_attempt if runtime.retry_attempt is not None else 1
            bullets.append(f"- 恢复重试: 第 {attempt} 次重试")
            if runtime.retry_source_run_id:
                bullets.append(f"- 来源 run: {runtime.retry_source_run_id}")
            if runtime.retry_source_message_id:
                bullets.append(f"- 来源消息: {runtime.retry_source_message_id}")
            if runtime.retry_failure_category or runtime.retry_failure_action_kind:
                category = runtime.retry_failure_category or "unknown"
                action = runtime.retry_failure_action_kind or "unknown"
                bullets.append(f"- 原失败分类: {category} / {action}")
            bullets.append(
                "- 重试要求: 先利用原失败分类调整策略，避免盲目重复上一次失败路径。"
                "如果同类失败再次出现，说明明确阻塞和下一步需要用户或运营处理的信息。"
            )
        if runtime.recovery_answered_questions:
            bullets.append("- 已确认的用户选择（必须沿用，不得再次询问）:")
            bullets.extend(
                f"  - {question} → {', '.join(answers)}"
                for question, answers in runtime.recovery_answered_questions
            )
        if not bullets:
            return ContextLayer(name="state", priority=50, content="")
        content = "<state>\n" + "\n".join(bullets) + "\n</state>"
        return ContextLayer(
            name="state",
            priority=50,
            content=content,
            max_chars=1024,
        )

    def _layer_skills(self, enabled_skills: list[str]) -> ContextLayer:
        """Layer 40 — names of skills the user has enabled for this run.

        We don't dump the skill body here; deepagents' SkillsMiddleware
        does that on activation. This layer just primes the model that
        the skills are available so it picks them up earlier in the
        decision loop.
        """
        if not enabled_skills:
            return ContextLayer(name="skills", priority=40, content="")
        lines = ["# 用户启用的 Skill", ""]
        for name in enabled_skills:
            lines.append(f"- {name}")
        lines.append("")
        lines.append("如果用户的请求与某个 skill 明显相关，优先使用该 skill 的能力。")
        return ContextLayer(
            name="skills",
            priority=40,
            content="\n".join(lines),
            max_chars=1024,
        )

    def _layer_runtime_context(self, runtime: RuntimeContext) -> ContextLayer:
        """Layer 55 — implicit context the model needs but the user
        didn't say out loud.

        Crucial design point: this is APPENDED to the prompt stack, NOT
        merged into the user message. The user's original words remain
        the user's words. The model sees this block as system-level
        runtime info, separate from the conversational turn.
        """
        now = runtime.now or _dt.datetime.now(_dt.UTC)
        # Convert to local for readability. Falls back to UTC if locale
        # detection is unavailable (CI, tests).
        try:
            local_now = now.astimezone()
        except Exception:
            local_now = now

        lines = ["# 运行时上下文", ""]
        lines.append(f"- 当前时间: {local_now.strftime('%Y-%m-%d %H:%M %Z')}")
        if runtime.workspace_root:
            lines.append(f"- 工作区根目录: `{runtime.workspace_root}`")
        else:
            lines.append("- 工作区: 未授权（仅虚拟文件系统，不能读写真实磁盘文件）")
        if runtime.attachments:
            lines.append(
                "- 本次附件（只读）: " + "、".join(f"`{path}`" for path in runtime.attachments)
            )

        loc = runtime.locale or _detect_locale()
        if loc:
            lines.append(f"- 用户语言偏好: {loc}")
        return ContextLayer(
            name="runtime_context",
            priority=55,
            content="\n".join(lines),
            max_chars=2048,
        )

    # ---- assembly ------------------------------------------------------

    def _assemble(self, layers: list[ContextLayer]) -> str:
        """Concat layers in priority order with per-layer + total budget
        enforcement.

        Truncation rules:
          - Non-truncatable layers exceeding their cap raise ValueError
            (loud failure during dev).
          - Truncatable layers exceeding their cap get cut at max_chars
            with a marker appended.
          - If the total assembled length exceeds total_budget_chars,
            truncate trailing truncatable layers until we fit.
        """
        ordered = sorted(layers, key=lambda layer: layer.priority)

        # Per-layer enforcement first.
        rendered: list[tuple[ContextLayer, str]] = []
        for layer in ordered:
            content = layer.content
            if len(content) > layer.max_chars:
                if not layer.truncatable:
                    raise ValueError(
                        f"context layer {layer.name!r} ({len(content)} chars) exceeds "
                        f"its non-truncatable cap of {layer.max_chars}; "
                        f"shrink the source content or raise the cap."
                    )
                content = content[: layer.max_chars - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
                log.warning(
                    "context layer %s truncated from %d to %d chars",
                    layer.name,
                    len(layer.content),
                    len(content),
                )
            rendered.append((layer, content))

        # Total enforcement — walk backwards through truncatable layers
        # and shave until we fit.
        joined = "\n\n".join(content for _, content in rendered)
        if len(joined) > self._total_budget_chars:
            log.warning(
                "assembled context %d chars exceeds total budget %d; trimming",
                len(joined),
                self._total_budget_chars,
            )
            # Greedy from-the-tail strategy: shorten the lowest-priority
            # truncatable layer first, then the next, etc.
            for i in range(len(rendered) - 1, -1, -1):
                layer, content = rendered[i]
                if not layer.truncatable:
                    continue
                overflow = len(joined) - self._total_budget_chars
                if overflow <= 0:
                    break
                new_len = max(0, len(content) - overflow - len(_TRUNCATION_MARKER))
                new_content = content[:new_len] + _TRUNCATION_MARKER if new_len > 0 else ""
                rendered[i] = (layer, new_content)
                joined = "\n\n".join(c for _, c in rendered if c)
            if len(joined) > self._total_budget_chars:
                raise ValueError("non-truncatable context layers exceed the total context budget")

        # Telemetry — per-layer + total char counts. Goes to the runtime
        # log so `make logs-runtime` can answer "why is my prompt
        # 12K chars?" without a debugger. Char counts (not tokens) are
        # cheap to compute; rough conversion in Chinese is ~2 chars per
        # token.
        layer_summary = ", ".join(
            f"{layer.name}={len(content)}" for layer, content in rendered if content
        )
        log.info(
            "context assembled: total=%d chars, layers=[%s]",
            len(joined),
            layer_summary,
        )
        return joined


# Module-level singleton for the common case. Tests construct their own
# instance with overridden paths.
_default_builder = ContextBuilder()


def build_default_context(runtime: RuntimeContext) -> str:
    """Convenience wrapper around the module-level builder."""
    return _default_builder.build(runtime=runtime)


def identity_safety_prompt() -> str:
    """Return the same provider-independent baseline used by the main Agent."""
    return _default_builder._layer_identity_safety().content


def reset_prompt_cache_for_tests() -> None:
    """Drop cached Runtime prompt files so a subsequent build re-reads them.

    Production code should NEVER call this — the cache is intentional
    to avoid file I/O per run."""
    _default_builder._identity_cache = None
    _default_builder._developer_cache = None


def _indent(value: str, prefix: str) -> str:
    return "\n".join(prefix + line if line.strip() else line for line in value.splitlines())


def _detect_locale() -> str | None:
    """Best-effort locale string for the runtime context block.

    Returns 'zh'/'en'/etc. from the system locale. Returns None when
    detection fails (CI without LANG, etc.) so the layer just omits
    the line instead of asserting a wrong default."""
    try:
        loc = _locale.getlocale()[0]
    except Exception:
        return None
    if not loc:
        return None
    return loc.split("_")[0].lower() if "_" in loc else loc.lower()
