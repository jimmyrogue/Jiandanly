# RapidOCR Runtime Asset

This directory defines the content-addressed `org.rapidocr.runtime` asset. The
Darwin, Windows AMD64, and Linux/arm64 builds freeze Python 3.12, RapidOCR 3.9.1,
ONNX Runtime 1.27.0, PP-OCRv6 small detection/recognition models, and the mobile
PP-OCRv4 orientation classifier. Runtime inference uses that exact classifier for
180-degree orientation and permits only `CPUExecutionProvider`.

The lock lists every package archive and model by filename, size, and SHA-256.
Building must be offline after those inputs are supplied. The produced engine is
PyInstaller `onedir`; it never extracts an executable into a temporary directory.
The Linux builder additionally locks its OCI image, Debian snapshot, native wheels,
binutils, and complete package/model input set. The Windows builder locks the exact
CPython 3.12 AMD64 wheel closure, validates both frozen entrypoints as AMD64 PE
executables, and refuses any changed or additional package/model input.
