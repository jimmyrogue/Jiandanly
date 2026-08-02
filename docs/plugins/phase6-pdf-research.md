# Phase 6 PDF capability decision

> Decision date: 2026-07-16. Primary stage: P10. Adjacent stages: P6 immutable input and Runtime Asset lease, P11 cleanup, P12 Artifact settlement.

## Outcome

PDF becomes a deterministic Managed Worker plugin backed by a standalone, exact `org.mupdf.runtime` asset. It does not call the current core `pdfplumber`/`pdfminer-six` compatibility parser, a host PDF program, or a chat model's implicit PDF support.

The first package exposes narrow Actions:

| Action | Purpose | Model-visible result | Artifacts |
| --- | --- | --- | --- |
| `pdf.inspect` | bounded page count and document state | bounded metadata | none |
| `pdf.extract_text` | explicit page window and character ceiling | page-numbered text chunks | optional full-window UTF-8 text |
| `pdf.render_pages` | explicit page numbers and DPI | page/output mapping | bounded PNG set |

Encrypted PDFs are detected but v1 does not accept a password argument. Action arguments are Runtime provenance, so passwords must not be placed there. OCR is also not hidden inside PDF extraction: pages without an embedded text layer return an explicit `ocr_required` indication for the separate OCR plugin.

## Existing-agent comparison

- Codex still tracks native PDF support as an open feature request; current behavior is not a reusable deterministic parser contract: <https://github.com/openai/codex/issues/1797>.
- Pi exposes packages and extensions but no pinned PDF engine or PDF Artifact protocol: <https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md>.
- Deep Agents can return typed PDF bytes from a backend, but that delegates interpretation to the selected model and therefore depends on model capability: <https://docs.langchain.com/oss/javascript/deepagents/backends>.
- LangChain offers many PDF loaders. PyMuPDF4LLM's page splitting, OCR alternatives, table strategies, and model option show why loading is a policy choice rather than one universal Agent primitive: <https://docs.langchain.com/oss/python/integrations/document_loaders/pymupdf4llm>.

SheJane therefore keeps page selection, limits, engine identity, output type, and provenance in explicit Actions.

## Why a standalone MuPDF asset

The repository already builds MuPDF 1.27.2 inside `org.libreoffice.runtime` for Office preview. Requiring that combined asset for `pdf.inspect` would force PDF-only users to install the much larger LibreOffice distribution. The long-term shape is a standalone content-addressed MuPDF asset that PDF and Office can share.

Office keeps its already-validated combined asset during the transition. It may switch to `org.mupdf.runtime` only after the standalone asset passes the same Office render goldens on every supported platform; no verified Office baseline is discarded merely to remove temporary duplication.

MuPDF is AGPL, so the asset must include the exact corresponding source, license material, build configuration, third-party component inventory, SBOM, platform toolchain, executable closure, and canonical Runtime Asset digest. The open-source license is compatible with SheJane's AGPL distribution, but it remains visible in the plugin UI and package metadata: <https://mupdf.com/releases>.

## Runtime Asset contract

```text
id       = org.mupdf.runtime
version  = 1.27.2+shejane.1
platform = linux/arm64 | linux/amd64 | windows/arm64 | windows/amd64
digest   = canonical SheJane Runtime Asset SHA-256
```

The payload contains only reviewed MuPDF command-line executables and their asset-local runtime files. v1 needs `mutool`; `mutool run` is not exposed as an Action and no Action accepts arbitrary command, JavaScript, option, page expression, or output path. The Worker constructs all command lines from validated fields.

The current `linux/arm64` candidate is built twice offline from the locked official HTTPS source in the exact Debian arm64 OCI/toolchain environment. The local asset archives are byte-for-byte identical:

```text
archive SHA-256  = aa3ea71d5a060e9662b6f9f821f5c0fe61bd307c14eeaeb0771e308e4c8df783
canonical digest = sha256:988979ecee53d458d6ab3ff91316425679fdaf66ffecf1b49f1d90c9e7202fbe
```

The builder additionally checks that OCR is reported disabled and that the final binary contains no Tesseract, Brotli, ZXing, or libarchive entry-point markers. MuPDF still compiles some feature wrapper translation units; those bodies are preprocessor-disabled and are not evidence that the third-party implementation was linked.

## Limits and result semantics

- `pdf.inspect`: at most 100,000 pages reported; metadata strings are length bounded and control characters removed.
- `pdf.extract_text`: explicit one-based `start_page`, bounded `page_count`, and aggregate `max_characters`; page boundaries remain explicit.
- `pdf.render_pages`: unique one-based page numbers, at most 16 pages, DPI 72-300, PNG only.
- Worker stdout/stderr, wall time, Artifact bytes, page count, image dimensions, and output file count remain host bounded.
- PDF bytes remain Runtime-owned `/input` files. Text/PNG bodies use normal Artifact settlement; no PDF or page image is put into SQLite, receipts, or model context as base64.
- A malformed, truncated, or unsupported PDF fails closed and leaves no committed Artifact. `pdf.inspect` may report `encrypted: true`; text extraction and rendering reject that input without producing Artifacts.

## Release Gate

1. exact source-byte lock with the upstream authentication level stated honestly, toolchain lock, reproducible package, license bundle, third-party inventory, and SBOM pass;
2. page count, page-window text, Unicode, blank/scanned pages, and selected-page PNG goldens pass;
3. hostile/truncated corpus fails within limits without host reads, network, descendants, or partial output;
4. large PDFs stay file-backed and cancellation cleans the Worker and `mutool` process tree;
5. packaged-app sandbox conformance passes on the native target;
6. Plugin Registry remains fail closed until the platform's general Managed Worker resource/isolation Gate passes.

The `linux/arm64` asset and frozen onedir Worker currently pass items 1-4 in the real macOS-arm64 VM: inspect, explicit page-window text extraction, Unicode preservation, blank/textless-page OCR signaling, exact selected-page PNG goldens, hostile/truncated input cleanup, mid-render cancellation, empty output after cancellation, and a successful replay proving the VM remains healthy. The general fourteen-mode VM isolation Gate also passes locally. The signed/notarized packaged-app release job must reproduce these results before item 5/6 can open the Registry; Linux amd64 and Windows remain unsupported until they receive their own native assets and Gates.

## Migration boundary

The current `ReadOnlyFileBackend` / `pdfplumber` path remains a legacy core capability while the plugin is unavailable. It is removed only after the PDF plugin is installed by default, explicitly selected through the same plugin reference flow, has passed a release cycle, and old Runs remain readable. There is no silent fallback between the plugin and the legacy parser.
