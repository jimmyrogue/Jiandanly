from pathlib import Path

import shejane_runtime.__main__ as cli_module
from shejane_runtime.__main__ import _parse_args, main


def test_runtime_cli_accepts_desktop_owned_launch_values(tmp_path: Path) -> None:
    args = _parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "18080",
            "--token",
            "pairing-token",
            "--data-dir",
            str(tmp_path),
            "--managed-worker-vm-assets",
            str(tmp_path / "manifest.json"),
            "--managed-worker-linux-assets",
            str(tmp_path / "linux" / "manifest.json"),
            "--computer-use-package",
            str(tmp_path / "computer-use.shejane-plugin"),
            "--browser-qa-package",
            str(tmp_path / "browser-qa.shejane-plugin"),
            "--browser-qa-runtime-asset",
            str(tmp_path / "browser-qa.shejane-runtime-asset"),
            "--ocr-package",
            str(tmp_path / "ocr.shejane-plugin"),
            "--ocr-runtime-asset",
            str(tmp_path / "rapidocr.shejane-runtime-asset"),
            "--fixed-runtime-asset-base-url",
            "https://downloads.example.test/client-v0.1.27",
            "--validate-managed-worker-vm-assets",
        ]
    )

    assert args.host == "127.0.0.1"
    assert args.port == 18080
    assert args.token == "pairing-token"
    assert args.data_dir == tmp_path
    assert args.managed_worker_vm_assets == tmp_path / "manifest.json"
    assert args.managed_worker_linux_assets == tmp_path / "linux" / "manifest.json"
    assert args.computer_use_package == tmp_path / "computer-use.shejane-plugin"
    assert args.browser_qa_package == tmp_path / "browser-qa.shejane-plugin"
    assert args.browser_qa_runtime_asset == tmp_path / "browser-qa.shejane-runtime-asset"
    assert args.ocr_package == tmp_path / "ocr.shejane-plugin"
    assert args.ocr_runtime_asset == tmp_path / "rapidocr.shejane-runtime-asset"
    assert args.fixed_runtime_asset_base_url == "https://downloads.example.test/client-v0.1.27"
    assert args.validate_managed_worker_vm_assets is True


def test_runtime_cli_preflights_vm_assets_without_starting_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    calls: list[Path] = []
    monkeypatch.setattr(
        "shejane_runtime.__main__.load_macos_vm_resources",
        lambda path: calls.append(path),
    )
    monkeypatch.setattr(
        "shejane_runtime.__main__.uvicorn.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("server started")),
    )

    assert (
        main(
            [
                "--managed-worker-vm-assets",
                str(manifest),
                "--validate-managed-worker-vm-assets",
            ]
        )
        == 0
    )
    assert calls == [manifest]


def test_runtime_cli_pins_the_lightweight_loopback_http_stack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = object()
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(cli_module, "create_app", lambda _settings: app)
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda candidate, **kwargs: calls.append((candidate, kwargs)),
    )

    assert main(["--token", "tok", "--data-dir", str(tmp_path)]) == 0
    assert calls == [
        (
            app,
            {
                "host": "127.0.0.1",
                "port": 17371,
                "log_level": "info",
                "access_log": False,
                "loop": "asyncio",
                "http": "h11",
                "ws": "none",
            },
        )
    ]
