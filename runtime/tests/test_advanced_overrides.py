"""Advanced agent-settings per-run overrides (the client's "Advanced" panel).

`runs._apply_advanced_overrides` folds knobs from the per-run `settings` dict
onto a copy of the base runtime Settings WITHOUT re-validating (model_copy), so
these tests pin the coercion + allow-list behavior that keeps bad client input
from reaching the run loop.
"""

from __future__ import annotations

from shejane_runtime.config import Settings
from shejane_runtime.run_configuration import _apply_advanced_overrides, freeze_run_settings


def _base() -> Settings:
    # Pin the fields under test to known values so assertions don't depend on
    # the developer's .env / shell environment.
    return Settings(
        SHEJANE_RUNTIME_INPUT_GUARD="observe",
        SHEJANE_PLAN_FIRST="off",
    )


def test_empty_overrides_returns_base_unchanged() -> None:
    base = _base()
    # No knobs sent ⇒ the exact same object (no needless copy) ⇒ legacy
    # callers behave identically.
    assert _apply_advanced_overrides(base, {}) is base


def test_all_knobs_applied_with_coercion() -> None:
    base = _base()
    eff = _apply_advanced_overrides(
        base,
        {
            "max_model_calls": "50",  # string → int
            "max_tool_retries": 4,
            "research_search_limit": "6",
            "subagents": False,  # real bool
            "browser_headless": "off",  # falsey string
            "input_guard": "block",
            "plan_first": "auto",
        },
    )
    assert eff.max_model_calls == 50 and isinstance(eff.max_model_calls, int)
    assert eff.max_tool_retries == 4
    assert eff.research_search_limit == 6
    assert eff.enable_subagents is False
    assert eff.browser_headless is False
    assert eff.input_guard_mode == "block"
    assert eff.plan_first_mode == "auto"
    # Copy semantics: the base instance is never mutated.
    assert base.max_model_calls == 100
    assert base.research_search_limit == 10
    assert base.enable_subagents is True


def test_invalid_values_are_ignored() -> None:
    base = _base()
    eff = _apply_advanced_overrides(
        base,
        {
            "max_model_calls": "not-a-number",  # unparseable → ignored
            "plan_first": "sometimes",  # off allow-list → ignored
            "unknown_knob": "whatever",  # unknown key → ignored
        },
    )
    assert eff.max_model_calls == base.max_model_calls
    assert eff.plan_first_mode == base.plan_first_mode


def test_verification_repair_max_env_is_clamped() -> None:
    assert Settings(SHEJANE_RUNTIME_VERIFY_REPAIR_MAX="").verification_repair_max == 1
    assert Settings(SHEJANE_RUNTIME_VERIFY_REPAIR_MAX=0).verification_repair_max == 0
    assert Settings(SHEJANE_RUNTIME_VERIFY_REPAIR_MAX=9).verification_repair_max == 3


def test_run_budget_integer_knobs_are_clamped() -> None:
    assert Settings(max_model_calls=0).max_model_calls == 1
    assert Settings(max_model_calls=999).max_model_calls == 100
    assert Settings(max_tool_retries=-1).max_tool_retries == 0
    assert Settings(max_tool_retries=99).max_tool_retries == 5
    assert Settings(research_search_limit=0).research_search_limit == 1
    assert Settings(research_search_limit=99).research_search_limit == 20

    base = _base()
    eff = _apply_advanced_overrides(
        base,
        {
            "max_model_calls": -5,
            "max_tool_retries": 999,
            "research_search_limit": 0,
        },
    )
    assert eff.max_model_calls == 1
    assert eff.max_tool_retries == 5
    assert eff.research_search_limit == 1


def test_partial_override_leaves_others_at_base() -> None:
    base = _base()
    eff = _apply_advanced_overrides(base, {"plan_first": "always"})
    assert eff.plan_first_mode == "always"
    # Everything the client didn't send stays at the base value.
    assert eff.input_guard_mode == base.input_guard_mode
    assert eff.enable_subagents == base.enable_subagents
    assert eff.max_model_calls == base.max_model_calls


def test_input_guard_override_may_strengthen_but_not_weaken() -> None:
    # Security-posture floor: a per-run client override can RAISE the guard
    # but never lower a machine/env baseline. Strength: off < observe < block.
    strong = Settings(SHEJANE_RUNTIME_INPUT_GUARD="block")
    # Client tries to downgrade block → observe: ignored, stays block.
    assert _apply_advanced_overrides(strong, {"input_guard": "observe"}).input_guard_mode == "block"
    # Client tries to downgrade block → off: ignored (off not even allowed).
    assert _apply_advanced_overrides(strong, {"input_guard": "off"}).input_guard_mode == "block"

    weak = Settings(SHEJANE_RUNTIME_INPUT_GUARD="observe")
    # Client strengthens observe → block: applied.
    assert _apply_advanced_overrides(weak, {"input_guard": "block"}).input_guard_mode == "block"
    # Same level is a no-op (and never copies needlessly).
    assert _apply_advanced_overrides(weak, {"input_guard": "observe"}) is weak


def test_run_settings_snapshot_freezes_effective_defaults() -> None:
    accepted_base = Settings(
        max_model_calls=17,
        SHEJANE_RUNTIME_INPUT_GUARD="observe",
        SHEJANE_RUNTIME_PII_REDACT="email",
    )
    snapshot = freeze_run_settings(accepted_base, {"plan_first": "always"})

    changed_base = Settings(
        max_model_calls=99,
        SHEJANE_RUNTIME_INPUT_GUARD="observe",
        SHEJANE_RUNTIME_PII_REDACT="",
    )
    effective = _apply_advanced_overrides(changed_base, snapshot)

    assert snapshot["_snapshot_version"] == 1
    assert effective.max_model_calls == 17
    assert effective.plan_first_mode == "always"
    assert effective.pii_redact_types == "email"


def test_snapshot_ignores_client_forged_internal_fields() -> None:
    snapshot = freeze_run_settings(
        _base(),
        {
            "_snapshot_version": 1,
            "memory_sources": "forged",
            "_model_binding": {"credential_ref": "raw-secret"},
        },
    )

    assert snapshot["_snapshot_version"] == 1
    assert snapshot["memory_sources"] == _base().memory_sources
    assert "_model_binding" not in snapshot
