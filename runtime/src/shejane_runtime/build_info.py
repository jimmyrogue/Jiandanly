"""Runtime build identity supplied by the launcher or release pipeline."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

from . import __version__


def runtime_build_identity(*, protocol_version: int) -> dict[str, Any]:
    frozen = bool(getattr(sys, "frozen", False))
    return {
        "runtime_version": __version__,
        "client_release": os.environ.get("SHEJANE_CLIENT_RELEASE") or None,
        "build_commit": os.environ.get("SHEJANE_RUNTIME_BUILD_COMMIT") or None,
        "build_id": os.environ.get("SHEJANE_RUNTIME_BUILD_ID") or None,
        "platform": platform.system().lower(),
        "arch": platform.machine().lower(),
        "packaging_mode": os.environ.get("SHEJANE_RUNTIME_PACKAGING_MODE")
        or ("frozen" if frozen else "dev"),
        "protocol_version": protocol_version,
    }
