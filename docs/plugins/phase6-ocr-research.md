# Phase 6 OCR capability decision

> Decision date: 2026-07-16. Primary stage: P10. Adjacent stages: P6 immutable input/Artifact and Runtime Asset leases, P9 review, P11 cleanup, P12 receipt and Artifact settlement.

## Outcome

OCR is a deterministic Managed Worker plugin backed by a platform-specific, content-addressed `org.rapidocr.runtime` asset. The first implementation freezes:

```text
RapidOCR   3.9.1
ONNX Runtime 1.27.0
model      PP-OCRv6 small detection + recognition
orientation PP-OCRv4 mobile classifier, enabled with exact frozen model
provider   CPUExecutionProvider only
threads    intra-op 1, inter-op 1
network    none
```

The plugin exposes one bounded batch Action, `ocr.recognize_images`. It accepts an ordered `input_ids` list of 1-16 immutable image attachments or same-Run PNG/JPEG/WebP/TIFF/BMP Artifacts. Results contain normalized integer quadrilaterals, text, five-decimal confidence, image dimensions, a bounded joined text view, truncation state, exact engine/model identity, and optional UTF-8/JSON Artifacts.

PDF OCR is composition rather than a second PDF engine: `pdf.render_pages` produces selected PNG Artifacts, then `ocr.recognize_images` consumes those Artifact IDs in order. Runtime now resolves file-backed same-Run Artifacts as read-only plugin inputs and includes the whole ordered ID/MIME/size/SHA-256 binding in downstream receipt identity. The OCR plugin therefore does not embed MuPDF or receive a shared writable workspace.

## Existing-agent comparison

- Codex image input ultimately depends on the selected multimodal model; its open PDF request describes OCR/image conversion as inconsistent rather than a fixed local engine contract: <https://github.com/openai/codex/issues/1797>.
- Pi can resize and send images to the selected LLM and extensions may return image content blocks, but packages run with full host access and there is no pinned OCR result contract: <https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md> and <https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md>.
- Deep Agents makes image files available through a shared virtual filesystem and returns them as multimodal blocks. That is useful context plumbing, not deterministic OCR, and its local filesystem backend explicitly carries host-access risk: <https://docs.langchain.com/oss/python/deepagents/backends>.
- LangGraph tools can pass values through graph state, but engine selection, binary/model identity, limits, and Artifact authority remain application responsibilities: <https://langchain-ai.github.io/langgraph/agents/tools/>.

SheJane therefore treats OCR as an explicit versioned Action rather than assuming every model can see an image.

## Engine selection

### Selected: PP-OCRv6 via RapidOCR and ONNX Runtime

PaddleOCR 3.7 introduced PP-OCRv6 in June 2026. Its unified recognition model covers Chinese, English, Japanese and 46 Latin-script languages: <https://github.com/PaddlePaddle/PaddleOCR>. RapidOCR 3.9.1 provides an Apache-2.0 ONNX Runtime implementation and model resolver for PP-OCRv6: <https://rapidai.github.io/RapidOCRDocs/latest/model_list/>. ONNX Runtime's CPU package supports Arm CPUs and macOS as well as the other desktop targets: <https://onnxruntime.ai/docs/get-started/with-python.html>.

The production Apple Silicon gate recognizes simplified Chinese, traditional Chinese, Japanese, English, low-contrast, multi-column, handwriting-style and rotated samples. The production configuration freezes `ch + small` and never exposes arbitrary engine/model switches.

The three current model-byte locks are:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `PP-OCRv6_det_small.onnx` | 9,929,594 | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| `PP-OCRv6_rec_small.onnx` | 21,234,383 | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` |
| `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 585,532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |

These bytes came from RapidOCR's versioned ModelScope `v3.9.1` paths. The release builder must download them ahead of time, verify size and SHA-256, bundle them locally, and pass exact `model_path` values. Runtime execution may not call ModelScope, Hugging Face, Paddle model servers, or any package index.

### Rejected as the primary engine

- Direct PaddlePaddle inference: the current general installation guide is x86-64 oriented and Apple Silicon requires a separate source build. That creates an avoidable cross-platform ABI and release split. Paddle's official ONNX conversion path confirms ONNX is an intended deployment form: <https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/deployment/obtaining_onnx_models.html>.
- Tesseract: excellent for a smaller alternative plugin and offers stable TSV/hOCR output, but it is not the best default for mixed Chinese/Japanese, scene text, handwriting, and current multilingual quality. There is no silent Tesseract fallback; a developer may publish a separate concrete Tesseract plugin.
- Apple Vision / Windows OCR / platform-native APIs: they would make the same plugin version produce materially different schemas and recognition behavior across operating systems.
- Vision-language model OCR: useful later for semantic understanding, but provider/model-dependent, nondeterministic, potentially remote, and not a replacement for local boxes/confidence/text extraction.

## Packaging boundary

`org.rapidocr.runtime` is a self-contained native asset built separately for every supported platform/architecture. Its payload contains a frozen onedir OCR engine, exact ONNX models, RapidOCR data files, native dependency closure, licenses, SBOM, and build provenance. The plugin Worker remains a small JSON-RPC executable and invokes only the asset-local engine.

The engine is frozen onedir rather than PyInstaller onefile: onefile would extract a large Python/ONNX/OpenCV closure for every invocation, complicate sandbox paths, startup, cancellation, and code signing. The Darwin reference pins CPython 3.12.10, uv 0.9.14, PyInstaller 6.21.0, NumPy 2.2.6 and `opencv-python-headless` 4.12.0.88. Standard OpenCV 5.0 was rejected after closure inspection found an unused Tesseract/Leptonica stack; the accepted builder hard-rejects those binary markers. The asset builder signs every native binary after collecting the closure. A later plugin may reuse the same exact asset digest; there is no generic mutable Python environment or runtime package installation.

## Determinism and limits

- CPU provider only; CoreML, Azure, DirectML, CUDA, OpenVINO and dynamic provider selection are disabled in v1.
- ONNX Runtime intra/inter-op threads are both 1. Model and engine paths are explicit. Locale/timezone are fixed by the Action envelope.
- Orientation classification uses only the exact bundled PP-OCRv4 mobile classifier. Hidden rotation retry, remote model, and model fallback are disabled; `Global.use_cls` is true so 180-degree inputs use the declared frozen classifier rather than an undocumented retry.
- At most 16 inputs, 50 megapixels per image, 160 megapixels total, 16,384 pixels on either axis, 10,000 returned lines, and 200,000 inline characters. Action timeout/memory/output limits remain host-enforced.
- Coordinates are rounded and clamped to decoded image bounds; confidence is rounded to five decimals; embedded NUL/control characters are removed; input order and engine reading order are preserved.
- Image bytes and large results remain file-backed. Optional text/JSON use normal Artifact settlement; images or ONNX tensors never enter SQLite or model context as base64.
- Decode failure, malformed model output, timeout, cancellation, output overflow, or an unsupported image fails closed and leaves no committed Artifact.

`input_stable` means the same plugin digest, Runtime Asset digest, ordered input digests, arguments, CPU provider, and platform target should produce the same normalized logical result. Cross-platform floating-point parity is checked with exact text/boxes and bounded confidence tolerance; release evidence must not claim byte-identical inference across different CPU/OS implementations without proof.

## Release Gate

1. signed or otherwise honestly authenticated source/wheel/model locks, exact hashes, license bundle, SBOM, and native closure pass;
2. two same-host builds are byte-for-byte identical and install to the same canonical Runtime Asset digest;
3. printed Chinese/English/Japanese/Latin, mixed-language, rotated-EXIF, low contrast, handwriting, empty image, and multi-column goldens pass;
4. oversized, decompression-bomb, malformed and adversarial images fail within time/memory/output limits;
5. ordered multi-Artifact chaining, cancellation, descendant cleanup, and packaged-app sandbox conformance pass;
6. Windows/Linux/macOS assets pass equivalent contracts and the general Managed Worker resource/isolation Gate before Registry enablement.

## Current implementation evidence

The OCR Action, strict input/output schemas, Managed Worker, command contribution and deterministic plugin packager are implemented. The Worker validates ordered immutable inputs, exact engine identity, dimensions/pixel budgets, line/confidence/polygon shape, bounded child output and optional Artifact cleanup. A fake-engine contract suite covers ordered batches, filtering and fail-closed engine substitution.

The `darwin/arm64` Runtime Asset reference builder verifies every package archive and model by filename, size and SHA-256, installs wheels with locked uv in offline mode, freezes a PyInstaller onedir engine, removes only non-runtime wheel `RECORD` entries that contain random build paths, rejects Tesseract/Leptonica, signs and checks every Mach-O closure member, and emits licenses, SBOM and build provenance. Two clean builds were byte-for-byte identical:

```text
archive SHA-256   9fc292f7b06b3dbc6b5748d999426030b4508deed257bc588e687744b9cefbba
canonical digest  sha256:253253d7a05718c8ff6889ea93bd2d00ef74588358f8e1690605169389501f6e
archive size      103,914,391 bytes
```

Runtime installed that final Darwin archive and the real Managed Worker recognized
`SheJane OCR 2026` twice with identical normalized output and identical text/JSON
Artifact hashes.

The actual macOS arm64 execution target is now a Linux/arm64 asset and frozen onedir
Worker. Two full builds produced byte-identical archives, and the production VM suite
passes deterministic replay, Chinese/English, low contrast, multi-column layout,
handwriting-style text, 180-degree orientation, hostile images, cancellation with no
partial Artifact, and post-cancel replay:

```text
archive SHA-256   c2e86a0ab167b653cc196a1153914ead15a9e8c3426d15609e9c47a6200723cb
canonical digest  sha256:5a11d7117d6de267cd51b0201386c2434a771439de2d588932743ebbf7f0b148
plugin digest     sha256:b5643b9c20862b5933a59360a3084771283e638f5df54849a52960c6b6cd010e
```

The final packaged-app Gate is wired into `release-client.yml`, but no real signed
and notarized release run has supplied release evidence yet. Registry therefore stays
closed. Japanese and broader real handwriting corpora, Linux/amd64, Windows, and each
platform's native parity Gate remain open.
