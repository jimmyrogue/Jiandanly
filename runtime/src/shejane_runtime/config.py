"""Runtime boot configuration and internal defaults."""

from __future__ import annotations

import ipaddress
import platform
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RUN_BUDGET_LIMITS: dict[str, tuple[int, int]] = {
    "max_model_calls": (1, 100),
    "max_tool_retries": (0, 5),
    "research_search_limit": (1, 20),
}

DEFAULT_RUNTIME_DATA_DIR = Path.home() / ".shejane" / "runtime"
LEGACY_RUNTIME_DATA_DIR = Path.home() / ".shejane" / "local-host"
LEGACY_RUNTIME_DB_FILENAME = "local-host.db"


def default_computer_use_package() -> Path | None:
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return None
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        return None
    name = "computer-use-0.2.2-darwin-arm64.shejane-plugin"
    package = Path(frozen_root) / "builtin-plugins" / name
    return package if package.is_file() else None


def default_browser_qa_package() -> Path | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        target = "darwin-arm64"
    elif sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        target = "windows-amd64"
    else:
        return None
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        return None
    package = Path(frozen_root) / "builtin-plugins" / f"browser-qa-0.1.3-{target}.shejane-plugin"
    return package if package.is_file() else None


def default_ocr_package() -> Path | None:
    machine = platform.machine().lower()
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        target = "darwin-arm64"
    elif sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        target = "windows-amd64"
    else:
        return None
    frozen_root = getattr(sys, "_MEIPASS", None)
    if not frozen_root:
        return None
    package = Path(frozen_root) / "builtin-plugins" / f"ocr-0.1.4-{target}.shejane-plugin"
    return package if package.is_file() else None


def clamp_run_budget(field: str, value: int) -> int:
    lower, upper = RUN_BUDGET_LIMITS[field]
    return max(lower, min(upper, value))


def _empty_string_default(value: Any, default: int) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return default
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHEJANE_RUNTIME_",
        validate_by_alias=True,
        validate_by_name=True,
        extra="ignore",
    )

    # HTTP server
    host: str = Field(default="127.0.0.1", alias="SHEJANE_RUNTIME_HOST")
    port: int = Field(default=17371, alias="SHEJANE_RUNTIME_PORT")
    pairing_token: str = Field(default="", alias="SHEJANE_RUNTIME_TOKEN")

    @field_validator("host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        host = value.strip()
        if host.lower() == "localhost":
            return "localhost"
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Runtime listener must use an explicit loopback host") from exc
        if not address.is_loopback:
            raise ValueError("Runtime listener must use an explicit loopback host")
        return str(address)

    # Storage
    data_dir: Path = Field(default=DEFAULT_RUNTIME_DATA_DIR)
    checkpoint_db_filename: str = "agent.db"
    store_db_filename: str = "store.db"
    runtime_db_filename: str = "runtime.db"
    managed_worker_vm_assets: Path | None = None
    managed_worker_linux_assets: Path | None = None
    computer_use_package: Path | None = Field(default_factory=default_computer_use_package)
    browser_qa_package: Path | None = Field(default_factory=default_browser_qa_package)
    browser_qa_runtime_asset: Path | None = None
    ocr_package: Path | None = Field(default_factory=default_ocr_package)
    ocr_runtime_asset: Path | None = None
    fixed_runtime_asset_base_url: str | None = None

    @field_validator(
        "managed_worker_vm_assets",
        "managed_worker_linux_assets",
        "computer_use_package",
        "browser_qa_package",
        "browser_qa_runtime_asset",
        "ocr_package",
        "ocr_runtime_asset",
    )
    @classmethod
    def require_absolute_vm_assets(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Runtime asset paths must be absolute")
        return value

    @field_validator("fixed_runtime_asset_base_url")
    @classmethod
    def require_https_runtime_asset_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("fixed Runtime Asset base URL must be credential-free HTTPS")
        return normalized

    # When set, the agent uses a deterministic in-process fake LLM instead of
    # any model service — no network, no key. Used by the SSE contract test to
    # exercise the real run/stream pipeline (event names + envelope) without a
    # live upstream. NEVER enable in production.
    fake_llm: bool = Field(default=False, alias="SHEJANE_FAKE_LLM")

    # Agent runtime knobs
    max_model_calls: int = 100
    max_tool_retries: int = 2
    research_search_limit: int = 10
    unknown_model_max_input_tokens: int = Field(
        default=32_768,
        alias="SHEJANE_RUNTIME_UNKNOWN_MODEL_MAX_INPUT_TOKENS",
        ge=8_192,
        le=10_000_000,
    )
    unknown_model_max_output_tokens: int = Field(
        default=8_192,
        alias="SHEJANE_RUNTIME_UNKNOWN_MODEL_MAX_OUTPUT_TOKENS",
        ge=128,
        le=1_000_000,
    )
    model_request_timeout_seconds: float = Field(
        default=120.0,
        alias="SHEJANE_RUNTIME_MODEL_REQUEST_TIMEOUT_SECONDS",
        ge=5.0,
        le=900.0,
    )

    # Browser
    browser_headless: bool = True

    # Subagents (Phase 6'+ — deepagents SubAgentMiddleware)
    enable_subagents: bool = True

    # Middleware modes with explicit (non-prefixed) aliases. These are also
    # read directly by their middleware as an env fallback; builder.py now
    # passes them explicitly so a per-run override (the client's Advanced
    # agent-settings panel) can win over the env default.
    #   input_guard_mode: observe | block
    #   plan_first_mode:  off | auto | always
    input_guard_mode: str = Field(default="observe", alias="SHEJANE_RUNTIME_INPUT_GUARD")
    plan_first_mode: str = Field(default="off", alias="SHEJANE_PLAN_FIRST")

    # Compatibility deployment policy. Applied only to the outbound request
    # copy for external providers; it never rewrites LangGraph state.
    pii_redact_types: str = Field(default="", alias="SHEJANE_RUNTIME_PII_REDACT")

    # Comma-separated AGENTS.md paths (or directories containing them) that
    # deepagents' MemoryMiddleware should load into the system prompt at
    # run start. Empty ⇒ no memory pre-load. Example:
    #   "~/.shejane/AGENTS.md,/path/to/project/AGENTS.md"
    memory_sources: str = Field(
        default="",
        alias="SHEJANE_RUNTIME_MEMORY_PATHS",
    )

    # Bounded verification repair. When task.verify returns ok=false and the
    # model tries to finalize, jump back to the model with a repair instruction
    # this many times. 0 disables the loop.
    verification_repair_max: int = Field(
        default=1,
        alias="SHEJANE_RUNTIME_VERIFY_REPAIR_MAX",
    )
    repair_workflow_max: int = Field(
        default=3,
        alias="SHEJANE_RUNTIME_REPAIR_WORKFLOW_MAX",
    )

    @field_validator("verification_repair_max", mode="before")
    @classmethod
    def _coerce_verification_repair_max(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return 1
        return value

    @field_validator("verification_repair_max")
    @classmethod
    def _clamp_verification_repair_max(cls, value: int) -> int:
        return max(0, min(3, value))

    @field_validator("repair_workflow_max", mode="before")
    @classmethod
    def _coerce_repair_workflow_max(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return 3
        return value

    @field_validator("repair_workflow_max")
    @classmethod
    def _clamp_repair_workflow_max(cls, value: int) -> int:
        return max(0, min(5, value))

    @field_validator("max_model_calls", mode="before")
    @classmethod
    def _coerce_max_model_calls(cls, value: Any) -> Any:
        return _empty_string_default(value, 100)

    @field_validator("max_model_calls")
    @classmethod
    def _clamp_max_model_calls(cls, value: int) -> int:
        return clamp_run_budget("max_model_calls", value)

    @field_validator("max_tool_retries", mode="before")
    @classmethod
    def _coerce_max_tool_retries(cls, value: Any) -> Any:
        return _empty_string_default(value, 2)

    @field_validator("max_tool_retries")
    @classmethod
    def _clamp_max_tool_retries(cls, value: int) -> int:
        return clamp_run_budget("max_tool_retries", value)

    @field_validator("research_search_limit", mode="before")
    @classmethod
    def _coerce_research_search_limit(cls, value: Any) -> Any:
        return _empty_string_default(value, 10)

    @field_validator("research_search_limit")
    @classmethod
    def _clamp_research_search_limit(cls, value: int) -> int:
        return clamp_run_budget("research_search_limit", value)

    def ensure_data_dir(self) -> Path:
        # Preserve existing installations across the one-time module rename.
        # This is data migration only; legacy names are not accepted as active
        # configuration, commands, imports, or protocol aliases.
        if self.data_dir == DEFAULT_RUNTIME_DATA_DIR and LEGACY_RUNTIME_DATA_DIR.exists():
            if not self.data_dir.exists():
                LEGACY_RUNTIME_DATA_DIR.replace(self.data_dir)
            else:
                for source in LEGACY_RUNTIME_DATA_DIR.iterdir():
                    destination = self.data_dir / source.name
                    if not destination.exists():
                        source.replace(destination)
                try:
                    LEGACY_RUNTIME_DATA_DIR.rmdir()
                except OSError:
                    pass
        self.data_dir.mkdir(parents=True, exist_ok=True)
        legacy_runtime_db = self.data_dir / LEGACY_RUNTIME_DB_FILENAME
        if legacy_runtime_db.exists() and not self.runtime_db_path.exists():
            legacy_runtime_db.replace(self.runtime_db_path)
        return self.data_dir

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / self.checkpoint_db_filename

    @property
    def store_db_path(self) -> Path:
        return self.data_dir / self.store_db_filename

    @property
    def runtime_db_path(self) -> Path:
        return self.data_dir / self.runtime_db_filename


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests(**overrides: object) -> Settings:
    """Replace the cached settings with a fresh instance — tests only."""
    global _settings
    _settings = Settings(**overrides)  # type: ignore[arg-type]
    return _settings
