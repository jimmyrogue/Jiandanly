from pathlib import Path


def test_dev_runtime_passes_built_browser_qa_and_ocr_packages() -> None:
    script = (Path(__file__).parents[2] / "scripts" / "dev.sh").read_text(encoding="utf-8")

    for flag in (
        "--browser-qa-package",
        "--browser-qa-runtime-asset",
        "--ocr-package",
        "--ocr-runtime-asset",
    ):
        assert flag in script
    assert "ocr-0.1.4-darwin-arm64.shejane-plugin" in script
    assert script.count("prepare_fixed_capability_args") == 3


def test_dev_runtime_fixed_capability_flags_have_no_leading_whitespace() -> None:
    script = (Path(__file__).parents[2] / "scripts" / "dev.sh").read_text(encoding="utf-8")

    flag_lines = [line for line in script.splitlines() if "|${ROOT_DIR}/runtime/plugins/" in line]

    assert flag_lines
    assert all(line.startswith("--") for line in flag_lines)
