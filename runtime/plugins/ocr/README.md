# OCR plugin

The OCR plugin recognizes text in an ordered batch of explicitly selected images.
It uses only the exact `org.rapidocr.runtime` asset declared in its installed
manifest: RapidOCR 3.9.1, PP-OCRv6 small detection/recognition models, ONNX Runtime,
and the CPU execution provider.

The plugin does not download models, switch providers, call a vision model, or fall
back to platform OCR. PDF OCR is an explicit composition: render selected pages with
the PDF plugin, then pass those same-Run PNG Artifact IDs to this plugin.

`build_runtime_asset.py` combines the verified native engine asset with the frozen
PyInstaller Worker. `build_package.py` then creates a deterministic metadata-only
`.shejane-plugin` bound to that exact composite Runtime Asset digest. The fixed OCR
host adapter executes the Worker from the verified asset; arbitrary third-party
packages cannot select that adapter. This keeps the general Managed Worker release
gate closed while allowing the trusted native OCR capability on macOS arm64 and
Windows AMD64.

Windows artifacts are produced only on a Windows AMD64 runner because PyInstaller
does not cross-compile. `scripts/build-ocr-windows-amd64.ps1` fetches the exact
locked wheels and models, performs two byte-identical native engine builds, freezes
the Worker, creates the composite Runtime Asset and digest-bound plugin, and runs the
real quality and hostile-input suite before publishing either artifact.
