# Third-Party Notices

SheJane includes and depends on third-party software, fonts, and tools. Those components remain under their own licenses and are not relicensed under the SheJane community or commercial license.

Exact dependency versions are recorded in `pnpm-lock.yaml`, `pyproject.toml`, and `uv.lock`. Distributions must retain the license and notice files supplied with each dependency.

## JavaScript and Electron

The Client client and Runtime SDK use packages under the MIT, ISC, BSD, Apache-2.0, and other permissive licenses.

Direct runtime dependencies include React, Lexical, Radix UI, Tailwind CSS, Tabler Icons, ExcelJS, docx-preview, highlight.js, react-markdown, Electron Updater, and related utilities. Their package metadata and installed license files contain the applicable copyright notices and terms.

The following items need separate attention:

- `lightningcss` and its platform packages are licensed under the Mozilla Public License 2.0 (`MPL-2.0`).
- `caniuse-lite` data is licensed under Creative Commons Attribution 4.0 (`CC-BY-4.0`).
- `jszip` is offered under `MIT OR GPL-3.0-or-later`; SheJane uses it under the MIT option.
- Electron and Chromium distributions include their own third-party license notices. Packaged applications must retain those notices.

## Python Runtime

Direct Python dependencies include LangChain, LangGraph, Deep Agents, FastAPI, Uvicorn, HTTPX, Pydantic, aiosqlite, MarkItDown, python-docx, python-pptx, openpyxl, PyYAML, Structlog, pyperclip, and SSE-Starlette.

Their declared licenses are primarily MIT, BSD, or Apache-2.0. Optional browser support also brings its own Playwright, browser, and Chromium notices when installed.

## Project assets and external services

SheJane logos and application icons are governed by [TRADEMARKS.md](TRADEMARKS.md), not by third-party dependency licenses.

External APIs, model providers, hosted services, user-installed MCP servers, skills, plugins, models, datasets, and user-supplied assets are not included in the SheJane source license. Their separate terms apply when a user enables them.

Report a missing or incorrect notice to [tliang92@gmail.com](mailto:tliang92@gmail.com).
