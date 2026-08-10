"""Stable assembly point for plugin persistence domains."""

from __future__ import annotations

from .plugin_catalog import PluginCatalogStore
from .plugin_installations import PluginInstallationStore
from .plugin_packages import PluginPackageStore
from .plugin_setup import PluginSetupStore


class PluginStore(
    PluginCatalogStore,
    PluginPackageStore,
    PluginInstallationStore,
    PluginSetupStore,
):
    """Plugin catalog and lifecycle behavior exposed through ``LocalStore``."""
