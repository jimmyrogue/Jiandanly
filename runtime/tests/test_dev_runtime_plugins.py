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
    assert "ocr-0.1.2-darwin-arm64.shejane-plugin" in script
    assert script.count("prepare_fixed_capability_args") == 3
