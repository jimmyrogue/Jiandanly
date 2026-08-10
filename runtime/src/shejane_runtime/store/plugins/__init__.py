"""Stable assembly point for plugin persistence domains."""

from __future__ import annotations

from .catalog import PluginCatalogStore
from .installations import PluginInstallationStore
from .packages import PluginPackageStore
from .setup import PluginSetupStore


class PluginStore(
    PluginCatalogStore,
    PluginPackageStore,
    PluginInstallationStore,
    PluginSetupStore,
):
    """Plugin catalog and lifecycle behavior exposed through ``LocalStore``."""
