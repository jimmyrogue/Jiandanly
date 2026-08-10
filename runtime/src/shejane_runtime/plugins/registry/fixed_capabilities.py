"""Runtime-owned fixed capability installation and setup flows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...store.sqlite import LocalStore, PluginStateError, PluginVersionConflictError
from ..browser_qa import BROWSER_QA_PLUGIN_ID
from ..computer_use import (
    COMPUTER_USE_PLUGIN_ID,
    ComputerUseError,
    ComputerUseReadiness,
    ComputerUseService,
)
from ..ocr import OCR_PLUGIN_ID
from .types import PluginRegistryError, PreparedPluginPackage

logger = logging.getLogger(__name__)

_BUILTIN_FIXED_PLUGIN_IDS = frozenset((COMPUTER_USE_PLUGIN_ID, BROWSER_QA_PLUGIN_ID, OCR_PLUGIN_ID))


class FixedCapabilityRegistry:
    def __init__(
        self,
        *,
        store: LocalStore,
        data_dir: Path,
        fixed_packages: dict[str, Path],
        ingest_package: Callable[..., PreparedPluginPackage],
        discard_new_blob: Callable[[PreparedPluginPackage], Awaitable[None]],
    ) -> None:
        self._store = store
        self._data_dir = data_dir
        self._fixed_packages = fixed_packages
        self._ingest_package = ingest_package
        self._discard_new_blob = discard_new_blob
        self._fixed_capabilities_ready_for: set[str] = set()
        self._fixed_capability_lock = asyncio.Lock()

    async def computer_use_readiness(self, *, principal_id: str) -> dict[str, Any]:
        package = await self._ensure_computer_use(principal_id)
        if package is None:
            return {
                "state": "blocked",
                "revision": 0,
                "step": None,
                "action_id": None,
                "can_recheck": False,
                "code": "unsupported_platform",
            }
        flow = await self._store.get_plugin_setup_flow(
            principal_id=principal_id, plugin_id=COMPUTER_USE_PLUGIN_ID
        )
        try:
            async with ComputerUseService(package, workspace_root=self._data_dir) as service:
                return await ComputerUseReadiness(service).inspect(
                    stage=str(flow["stage"]), revision=int(flow["revision"])
                )
        except ComputerUseError as exc:
            raise PluginRegistryError(
                "computer_use_readiness_failed", str(exc), status_code=503
            ) from exc

    async def advance_computer_use_setup(
        self,
        *,
        principal_id: str,
        command_id: str,
        expected_revision: int,
        action_id: str,
    ) -> dict[str, Any]:
        payload = {
            "type": "plugin.setup.advance",
            "plugin_id": COMPUTER_USE_PLUGIN_ID,
            "expected_revision": expected_revision,
            "action_id": action_id,
        }
        replay = await self._store.accepted_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.setup.advance",
            payload=payload,
        )
        if replay is not None:
            return replay
        package = await self._ensure_computer_use(principal_id)
        if package is None:
            raise PluginRegistryError(
                "unsupported_platform",
                "Computer Use is unavailable on this platform",
                status_code=409,
            )
        flow = await self._store.get_plugin_setup_flow(
            principal_id=principal_id, plugin_id=COMPUTER_USE_PLUGIN_ID
        )
        stage = str(flow["stage"])
        if int(flow["revision"]) != expected_revision:
            raise PluginRegistryError(
                "plugin_setup_stale", "Computer Use setup state changed", status_code=409
            )
        next_stage = ComputerUseReadiness.stage_after(action_id, stage)
        try:
            async with ComputerUseService(package, workspace_root=self._data_dir) as service:
                snapshot = await ComputerUseReadiness(service).advance(
                    action_id=action_id,
                    stage=stage,
                    revision=expected_revision,
                )
        except ComputerUseError as exc:
            raise PluginRegistryError(
                "computer_use_setup_failed", str(exc), status_code=503
            ) from exc
        try:
            advanced = await self._store.begin_plugin_setup_action(
                principal_id=principal_id,
                plugin_id=COMPUTER_USE_PLUGIN_ID,
                expected_revision=expected_revision,
                next_stage=next_stage,
            )
        except PluginStateError as exc:
            raise PluginRegistryError(exc.code, str(exc), status_code=409) from exc
        if int(snapshot["revision"]) != int(advanced["revision"]):
            raise PluginRegistryError(
                "plugin_setup_state_invalid", "Computer Use setup revision changed", status_code=500
            )
        receipt = {
            "type": "plugin.setup.advance",
            "command_id": command_id,
            "plugin_id": COMPUTER_USE_PLUGIN_ID,
            "readiness": snapshot,
        }
        return await self._store.record_command_receipt(
            principal_id=principal_id,
            command_id=command_id,
            command_type="plugin.setup.advance",
            payload=payload,
            receipt=receipt,
        )

    async def _ensure_computer_use(self, principal_id: str) -> Path | None:
        source = self._fixed_packages.get(COMPUTER_USE_PLUGIN_ID)
        if source is None or not source.is_file():
            return None
        async with self._fixed_capability_lock:
            return await self._ensure_fixed_plugin(principal_id, COMPUTER_USE_PLUGIN_ID, source)

    async def initialize_fixed_capabilities(self, principal_id: str) -> None:
        if principal_id in self._fixed_capabilities_ready_for:
            return
        async with self._fixed_capability_lock:
            if principal_id in self._fixed_capabilities_ready_for:
                return
            for plugin_id, source in self._fixed_packages.items():
                if not source.is_file():
                    raise PluginRegistryError(
                        "builtin_capability_unavailable",
                        f"configured fixed capability package for {plugin_id} must be a regular file",
                        status_code=500,
                    )
            for plugin_id, source in self._fixed_packages.items():
                await self._ensure_fixed_plugin(principal_id, plugin_id, source)
            # Disable any fixed-capability installation whose source package
            # was not provided during this startup.  Otherwise a stale
            # (e.g. older-version) built-in stays enabled and gets bound to
            # every run, where allowlist validation then fails the whole run.
            for plugin_id in _BUILTIN_FIXED_PLUGIN_IDS:
                if plugin_id in self._fixed_packages:
                    continue
                disabled = await self._store.discard_stale_fixed_capability(
                    principal_id=principal_id,
                    plugin_id=plugin_id,
                )
                if disabled:
                    logger.warning(
                        "Fixed capability %s package not available; installation disabled",
                        plugin_id,
                    )
            self._fixed_capabilities_ready_for.add(principal_id)

    async def _ensure_fixed_plugin(self, principal_id: str, plugin_id: str, source: Path) -> Path:
        prepared = await asyncio.to_thread(
            self._ingest_package,
            str(source),
            True,
            None,
            True,
        )
        if prepared.manifest["id"] != plugin_id:
            await self._discard_new_blob(prepared)
            raise PluginRegistryError(
                "builtin_capability_unavailable",
                "fixed capability package identity changed",
                status_code=409,
            )
        current = await self._store.get_plugin(principal_id=principal_id, plugin_id=plugin_id)
        payload = {
            "type": "runtime.builtin.ensure",
            "plugin_id": plugin_id,
            "digest": prepared.digest,
        }
        try:
            if current is None:
                await self._store.install_plugin_command(
                    principal_id=principal_id,
                    command_id=f"builtin-install:{prepared.digest}",
                    command_payload=payload,
                    manifest=prepared.manifest,
                    digest=prepared.digest,
                    signature_status="unsigned",
                    signer_key_id=None,
                    compatibility=prepared.compatibility,
                    source="runtime_builtin",
                    command_type="runtime.builtin.ensure",
                    receipt_type="runtime.builtin.ensure",
                )
            elif current["digest"] != prepared.digest:
                await self._store.update_plugin_command(
                    principal_id=principal_id,
                    command_id=f"builtin-update:{prepared.digest}",
                    command_payload=payload,
                    plugin_id=plugin_id,
                    manifest=prepared.manifest,
                    digest=prepared.digest,
                    signature_status="unsigned",
                    signer_key_id=None,
                    compatibility=prepared.compatibility,
                    source="runtime_builtin",
                    command_type="runtime.builtin.ensure",
                    receipt_type="runtime.builtin.ensure",
                )
        except (PluginStateError, PluginVersionConflictError) as exc:
            await self._discard_new_blob(prepared)
            raise PluginRegistryError(
                "builtin_capability_unavailable", str(exc), status_code=409
            ) from exc
        return prepared.destination
