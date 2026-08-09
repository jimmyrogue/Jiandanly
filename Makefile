# ============================================================================
# SheJane / 石间 — developer & ops command surface.
#
#   make            → grouped help (this is the default goal)
#   make ci         → run everything CI runs, locally
#   make dev / restart-runtime → run the full stack / restart Runtime only
#   make release COMPONENT=client VERSION=X.Y.Z → push client-vX.Y.Z
#
# Targets are grouped with `##@ Section` headers and self-document via the
# `## description` after each name — `make help` parses those. Keep new
# targets annotated so they show up.
# ============================================================================

.DEFAULT_GOAL := help

.PHONY: help \
	dev dev-client dev-runtime restart-runtime doctor \
	test test-client test-runtime test-runtime-sdk test-scripts test-contract test-fixed-plugins-e2e test-e2e test-e2e-real test-packaged \
	ci build build-client build-runtime build-runtime-sdk package-runtime \
	lint schemas setup-hooks \
	release eval eval-gate \
	logs logs-client logs-runtime

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next} \
		/^[a-zA-Z0-9_.-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)
	@echo ""

##@ Dev & restart
dev: ## Start Client + Runtime with a clean local restart
	./scripts/dev.sh start

dev-client: ## Start Client only using SHEJANE_RUNTIME_URL and SHEJANE_RUNTIME_TOKEN
	./scripts/dev.sh start-client

dev-runtime: ## Start Runtime only on SHEJANE_RUNTIME_PORT
	./scripts/dev.sh start-runtime

restart-runtime: ## Hard-restart Runtime only after a Python edit
	./scripts/dev.sh restart

doctor: ## One-shot diagnostic: "why isn't dev working?"
	@./scripts/dev.sh doctor

##@ Test
test: test-client test-runtime-sdk test-runtime test-scripts ## Fast unit suites by fault domain

test-client: ## Client unit tests
	pnpm --filter @shejane/client test --run

test-runtime: ## Runtime unit tests
	cd runtime && uv run python -m pytest

test-runtime-sdk: ## Runtime SDK unit tests
	pnpm --filter @shejane/runtime-sdk test

test-scripts: ## Node script contract tests
	node --test scripts/tests/*.test.mjs

test-contract: ## Real Runtime HTTP/SSE ↔ Runtime SDK contract tests, without Electron
	SHEJANE_CONTRACT_ONLY=1 ./scripts/test-contract.sh

test-fixed-plugins-e2e: ## Browser QA, Computer Use, and OCR execution paths
	./scripts/test-fixed-plugins-e2e.sh

test-e2e: test-fixed-plugins-e2e ## Fixed plugins + Runtime recovery + contract + Electron Client paths
	./scripts/test-contract.sh

test-e2e-real: export SHEJANE_EVAL_MODEL := $(MODEL)
test-e2e-real: ## Normal Agent, every Tool, and Client flows through a real BYOK LLM
	@test -n "$$SHEJANE_EVAL_MODEL" || { echo "❌ MODEL is required, for example: make test-e2e-real MODEL=local:deepseek:deepseek-v4-flash" >&2; exit 2; }
	./scripts/test-e2e-real.sh

test-packaged: ## Verify a packaged Client + bundled Runtime (APP=/path/to/app)
	@test -n "$(APP)" || { echo "❌ APP is required" >&2; exit 2; }
	node scripts/test-packaged-client-runtime.mjs "$(APP)"

ci: lint test build test-e2e ## Run EVERYTHING CI runs, locally (before pushing a PR)

##@ Build
build: build-runtime-sdk build-client build-runtime ## Build both modules from source

build-client: ## Build the Client
	pnpm --filter @shejane/client build

build-runtime-sdk: ## Build the Runtime SDK
	pnpm --filter @shejane/runtime-sdk build

build-runtime: ## Sync the Python Runtime environment
	cd runtime && uv sync --frozen

package-runtime: ## Freeze Runtime into runtime/dist/shejane-runtime/
	./scripts/build-computer-use-builtin.sh
	./scripts/build-browser-qa-builtin.sh
	./scripts/build-ocr-builtin.sh
	./scripts/build-linux-managed-worker-launcher.sh
	cd runtime && uv run --isolated --no-dev --group package python -m PyInstaller shejane-runtime.spec --noconfirm --clean
	@echo "✅ Runtime frozen → runtime/dist/shejane-runtime/ (run it on THIS OS/arch only)"

##@ Lint & schemas
lint: ## Run the same lint checks CI runs
	@echo "→ ruff (Python)"
	@cd runtime && uv run ruff check src tests && uv run ruff format --check src tests
	@echo "→ project guards"
	@./scripts/check.sh
	@echo "✅ all lints pass"

schemas: ## Regenerate openapi.json + generated.ts from the runtime's pydantic models
	@./scripts/export-runtime-openapi.sh
	@pnpm --filter @shejane/runtime-sdk generate
	@echo "✅ schemas regenerated. Commit openapi.json + generated.ts."

setup-hooks: ## Install lefthook + wire pre-commit hooks (run once per clone)
	@if ! command -v lefthook >/dev/null 2>&1; then \
		echo "lefthook not found. Installing via brew (macOS)…"; \
		if command -v brew >/dev/null 2>&1; then \
			brew install lefthook; \
		else \
			echo "❌ brew not available. Install lefthook manually:" >&2; \
			echo "    https://github.com/evilmartians/lefthook#install" >&2; \
			exit 1; \
		fi; \
	fi
	@lefthook install
	@echo "✅ Pre-commit hooks wired. Bypass once with: LEFTHOOK=0 git commit"

##@ Release
release: ## Cut a published release: COMPONENT=client|runtime-sdk VERSION=X.Y.Z
	@case "$(COMPONENT)" in client|runtime-sdk) ;; *) echo "❌ COMPONENT must be client or runtime-sdk" >&2; exit 1 ;; esac
	@printf '%s\n' "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "❌ VERSION must look like X.Y.Z" >&2; exit 1; }
	@if [ -n "$$(git status --porcelain)" ]; then echo "❌ Working tree not clean — commit or stash first." >&2; exit 1; fi
	@branch=$$(git rev-parse --abbrev-ref HEAD); if [ "$$branch" != "main" ]; then echo "❌ Releases must be cut from main (currently on '$$branch')." >&2; exit 1; fi
	git tag -a "$(COMPONENT)-v$(VERSION)" -m "$(COMPONENT) v$(VERSION)"
	git push origin "$(COMPONENT)-v$(VERSION)"
	@echo "✅ Pushed $(COMPONENT)-v$(VERSION). Only that module's release workflow will run."
##@ Eval
eval: ## Run the agent eval suite against a Runtime with a real provider
	cd runtime && uv run python -m shejane_runtime.eval

eval-gate: ## Deterministic core Agent outcome gate for CI and releases
	@mkdir -p .tmp
	cd runtime && uv run python -m pytest -q \
		tests/test_eval_harness.py \
		tests/test_e2e_capabilities.py::test_permission_mode_auto_executes_workspace_writes_without_prompt \
		tests/test_e2e_capabilities.py::test_read_file_without_pagination_reads_a_normal_text_file_in_one_call \
		tests/test_e2e_capabilities.py::test_capability_1b_permission_approve_resumes_the_run \
		tests/test_e2e_capabilities.py::test_capability_2_subagent_task_surfaces_spawned_event \
		tests/test_e2e_capabilities.py::test_capability_6_memory_middleware_injects_agents_md \
		tests/test_e2e_capabilities.py::test_capability_7_todolist_middleware_exposes_write_todos_tool \
		tests/test_e2e_capabilities.py::test_capability_9_memory_search_tool_in_agent \
		tests/test_e2e_capabilities.py::test_capability_10_plan_first_is_a_runtime_gate_not_prompt_text \
		tests/test_e2e_capabilities.py::test_capability_8_happy_path_run_completes \
		tests/test_run_recovery.py::test_recover_orphans_fails_dead_runs_and_keeps_waiting \
		--junitxml=../.tmp/eval-gate.xml

##@ Logs
logs: ## Snapshot both Client and Runtime logs
	./scripts/dev.sh logs all

logs-client: ## Tail the client Vite log
	./scripts/dev.sh logs client

logs-runtime: ## Tail the Runtime log
	./scripts/dev.sh logs runtime
