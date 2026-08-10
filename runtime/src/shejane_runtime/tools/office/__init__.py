"""Stable office.* tool facade and cross-format read entry points.

Word, Excel, and PowerPoint write implementations live in their format-owned
modules. All writes remain copy-on-first-write and atomically replace only the
edited copy. Read tools accept authorized workspace paths or Runtime-owned
attachment routes; write tools remain workspace-only.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from ...document_markdown import document_to_markdown
from .common import (
    MARKDOWN_CHAR_CAP as _MARKDOWN_CHAR_CAP,
)
from .common import (
    resolve_read_source as _resolve_read_source,
)
from .common import (
    resolve_write_target as _resolve_write_target,
)
from .docx import (
    office_apply_style,
    office_delete_paragraph,
    office_find_replace,
    office_insert_paragraph,
    office_update_paragraph,
    outline_docx,
)
from .pptx import (
    _outline_pptx,
    office_add_image_to_slide,
    office_add_slide,
    office_create_pptx,
    office_delete_slide,
    office_read_slides,
    office_reorder_slides,
    office_set_slide_bullets,
    office_set_slide_notes,
    office_set_slide_title,
    office_update_slide,
)
from .xlsx import (
    _outline_xlsx,
    get_column_letter,
    office_add_row,
    office_merge_cells,
    office_read_range,
    office_set_cell_format,
    office_set_cells,
    office_set_formula,
)


@tool("office.read")
def office_read(path: str) -> dict[str, Any]:
    """Read a Word (.docx), Excel (.xlsx), or PowerPoint (.pptx) file as markdown.

    Converts headings, paragraphs, tables, and cells into compact markdown
    the LLM can reason about directly.

    Does NOT open the right-side document preview panel. If you want the
    user to see the file rendered, mention the filename in your reply
    so the renderer makes it clickable. The preview opens when the user
    clicks, or after a successful office.* WRITE tool completes.

    Args:
        path: Exact Runtime `/attachments/...` route, or an existing absolute
              path under the authorized workspace.

    Returns:
        dict with keys:
          ok ("true" / "false")
          path (echoed back, absolute)
          kind ("word", "excel", or "powerpoint")
          markdown (the converted markdown content, possibly truncated)
          truncated ("true" / "false") — set when content exceeded the cap
          error (only present when ok="false")
    """
    source, kind, err = _resolve_read_source(path)
    if err is not None:
        return {"ok": "false", "error": err}
    assert source is not None and kind is not None
    reported_path = path if isinstance(source, bytes) else source
    try:
        text, truncated = document_to_markdown(
            source,
            Path(path).suffix.lower(),
            char_limit=_MARKDOWN_CHAR_CAP,
        )
    except Exception as exc:
        return {
            "ok": "false",
            "path": reported_path,
            "kind": kind,
            "error": f"failed to convert {kind}: {exc.__class__.__name__}: {exc}",
        }
    if truncated:
        text = text[:_MARKDOWN_CHAR_CAP] + "\n\n…(truncated)"
    return {
        "ok": "true",
        "path": reported_path,
        "kind": kind,
        "markdown": text,
        "truncated": "true" if truncated else "false",
    }


@tool("office.outline")
def office_outline(path: str) -> dict[str, Any]:
    """Return a cheap structural summary of a .docx, .xlsx, or .pptx file.

    Use this BEFORE `office.read` when the file is large and you only need
    to know what's in it — for example: "tell me what sheets are in
    Q4.xlsx", "does report.docx have a section about pricing?", or "how
    many slides does pitch.pptx have?". Reading the outline is
    O(metadata); reading the full markdown is O(file).

    Args:
        path: Exact Runtime `/attachments/...` route, or an existing absolute
              path under the authorized workspace.

    Returns:
        dict with keys:
          ok ("true" / "false")
          path (echoed back, absolute)
          kind ("word", "excel", or "powerpoint")
          For .docx: headings, paragraph_count, table_count.
          For .xlsx: sheets (list of {name, rows, columns}).
          For .pptx: slides (list of {index, layout, title, bullets,
                     notes, shape_count, image_count}), slide_count.
          error (only present when ok="false")
    """
    source, kind, err = _resolve_read_source(path)
    if err is not None:
        return {"ok": "false", "error": err}
    assert source is not None and kind is not None
    reported_path = path if isinstance(source, bytes) else source
    parser_input = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        if kind == "word":
            details = outline_docx(parser_input)
        elif kind == "excel":
            details = _outline_xlsx(parser_input)
        else:
            details = _outline_pptx(parser_input)
    except Exception as exc:
        return {
            "ok": "false",
            "path": reported_path,
            "kind": kind,
            "error": f"failed to read {kind} outline: {exc.__class__.__name__}: {exc}",
        }
    return {
        "ok": "true",
        "path": reported_path,
        "kind": kind,
        **details,
    }


OFFICE_READ_TOOLS = [office_read, office_outline, office_read_range, office_read_slides]

OFFICE_WRITE_TOOLS = [
    office_find_replace,
    office_insert_paragraph,
    office_update_paragraph,
    office_delete_paragraph,
    office_apply_style,
    office_set_cells,
    office_set_formula,
    office_set_cell_format,
    office_merge_cells,
    office_add_row,
    office_create_pptx,
    office_add_slide,
    office_update_slide,
    office_delete_slide,
    office_reorder_slides,
    office_set_slide_title,
    office_set_slide_bullets,
    office_set_slide_notes,
    office_add_image_to_slide,
]


def edited_copy_path(original_path: str) -> str:
    return _resolve_write_target(original_path)


__all__ = [
    "OFFICE_READ_TOOLS",
    "OFFICE_WRITE_TOOLS",
    "edited_copy_path",
    "get_column_letter",
    "office_add_image_to_slide",
    "office_add_row",
    "office_add_slide",
    "office_apply_style",
    "office_create_pptx",
    "office_delete_paragraph",
    "office_delete_slide",
    "office_find_replace",
    "office_insert_paragraph",
    "office_merge_cells",
    "office_outline",
    "office_read",
    "office_read_range",
    "office_read_slides",
    "office_reorder_slides",
    "office_set_cell_format",
    "office_set_cells",
    "office_set_formula",
    "office_set_slide_bullets",
    "office_set_slide_notes",
    "office_set_slide_title",
    "office_update_paragraph",
    "office_update_slide",
]
