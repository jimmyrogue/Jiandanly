from __future__ import annotations

from pathlib import Path

SPEC = Path(__file__).resolve().parents[1] / "shejane-runtime.spec"


def test_frozen_runtime_does_not_copy_python_sources_as_data() -> None:
    spec = SPEC.read_text(encoding="utf-8")

    assert "collect_all(pkg, include_py_files=False)" in spec
    assert 'collect_all("wasmtime", include_py_files=False)' in spec
