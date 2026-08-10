"""Shared PluginRegistry values without importing the facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PluginRegistryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedPluginPackage:
    manifest: dict[str, Any]
    digest: str
    compatibility: str
    signature_status: str
    signer_key_id: str | None
    destination: Path
    created_blob: bool
