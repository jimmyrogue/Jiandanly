"""office.* — read, outline, and edit tools for Word (.docx) and Excel (.xlsx).

Phase 1 (already shipped) — read-only:
  * office.read       — full content as LLM-ready markdown
  * office.outline    — cheap structural summary

Phase 2 (this module) — edits via COPY-ON-FIRST-WRITE:
  Docx: find_replace, insert_paragraph, update_paragraph,
        delete_paragraph, apply_style
  Xlsx: set_cells, set_formula, set_cell_format, merge_cells,
        add_row, read_range

The user's hard constraint for Phase 2: the original file is NEVER
modified. Every write tool copies the original to a sibling
`<basename>.edited.<ext>` on first write and targets the copy
thereafter. Repeated edits land in the same copy. The user can reset
edits by deleting `xxx.edited.docx` in Finder.

Because the original is untouched, rollback remains predictable. The Runtime
still reviews each concrete write operation before execution.

All write tools also use an atomic write pattern: write to
`<target>.tmp`, re-open with the appropriate library to verify it's a
valid OOXML file, then `os.replace(tmp, target)`. A mid-write failure
leaves `target` exactly as it was (the last known-good edit).

Path safety: read tools accept either a path under the authorized workspace
or an exact `/attachments/...` route owned by the current Run. Write tools
remain restricted to the authorized workspace and never mutate attachments.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.text.paragraph import Paragraph as DocxParagraph
from langchain_core.tools import tool

from ..document_markdown import document_to_markdown
from .office_common import (
    MARKDOWN_CHAR_CAP as _MARKDOWN_CHAR_CAP,
)
from .office_common import (
    atomic_write as _atomic_write,
)
from .office_common import (
    ensure_copy_for_write as _ensure_copy_for_write,
)
from .office_common import (
    resolve_read_source as _resolve_read_source,
)
from .office_common import (
    resolve_write_target as _resolve_write_target,
)
from .office_common import (
    validate_path as _validate_path,
)
from .office_common import (
    write_error as _write_error,
)
from .office_common import (
    write_result as _write_result,
)
from .office_pptx import (
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
from .office_xlsx import (
    _outline_xlsx,
    get_column_letter,
    office_add_row,
    office_merge_cells,
    office_read_range,
    office_set_cell_format,
    office_set_cells,
    office_set_formula,
)

# ═══════════════════════════════════════════════════════════════════════
# READ tools (Phase 1)
# ═══════════════════════════════════════════════════════════════════════


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
    assert source is not None and kind is not None  # for type checker
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


def _outline_docx(path: Any) -> dict[str, Any]:
    """Cheap structural summary of a .docx — heading text and paragraph count."""
    doc = DocxDocument(path)
    headings: list[dict[str, Any]] = []
    paragraph_count = 0
    table_count = len(doc.tables)
    for para in doc.paragraphs:
        paragraph_count += 1
        style_name = (para.style.name if para.style is not None else "") or ""
        if style_name.startswith("Heading"):
            level_str = style_name.split()[-1] if " " in style_name else "1"
            try:
                level = int(level_str)
            except ValueError:
                level = 1
            text = para.text.strip()
            if text:
                headings.append({"level": level, "text": text})
    return {
        "headings": headings,
        "paragraph_count": paragraph_count,
        "table_count": table_count,
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
            details = _outline_docx(parser_input)
        elif kind == "excel":
            details = _outline_xlsx(parser_input)
        else:  # powerpoint
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


# ═══════════════════════════════════════════════════════════════════════
# DOCX write helpers
# ═══════════════════════════════════════════════════════════════════════


def _find_paragraph(doc: DocxDocumentType, target: str) -> DocxParagraph | None:
    """Return the first paragraph whose .text contains `target` (exact
    substring match), or None. Tables are walked too — paragraphs inside
    cells should be addressable just like top-level body paragraphs."""
    if not target:
        return None
    for para in doc.paragraphs:
        if target in para.text:
            return para
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if target in para.text:
                        return para
    return None


def _set_paragraph_text(para: DocxParagraph, new_text: str) -> None:
    """Replace a paragraph's text with `new_text`, collapsing all
    existing runs into one. This LOSES run-level formatting inside the
    paragraph (bold-in-middle, font color, etc.) but preserves the
    paragraph-level style (Heading 1, Quote, list bullet, etc.). For
    the find_replace / update_paragraph use cases this is the right
    trade — agents are editing content, not micro-formatting."""
    if para.runs:
        # Reuse the first run to keep its char formatting as the new
        # paragraph's font (best-effort), then clear the rest.
        para.runs[0].text = new_text
        for run in para.runs[1:]:
            run.text = ""
    else:
        para.add_run(new_text)


def _insert_paragraph_relative(
    anchor: DocxParagraph, content: str, position: str, style: str | None
) -> DocxParagraph:
    """Insert a new paragraph immediately before or after `anchor`.

    python-docx has no built-in "insert before/after" — we manipulate
    the XML directly. This is the standard recipe from the docx-py
    cookbook.
    """
    from copy import deepcopy

    new_p = deepcopy(anchor._p)
    # Strip the existing runs from the cloned XML so we get a blank
    # paragraph element with the same pPr (paragraph properties).
    for child in list(new_p):
        if child.tag.endswith("}r"):
            new_p.remove(child)
    if position == "before":
        anchor._p.addprevious(new_p)
    else:  # default + 'after'
        anchor._p.addnext(new_p)
    new_para = DocxParagraph(new_p, anchor._parent)
    if style:
        new_para.style = style
    new_para.add_run(content)
    return new_para


# ═══════════════════════════════════════════════════════════════════════
# DOCX write tools
# ═══════════════════════════════════════════════════════════════════════


@tool("office.find_replace")
def office_find_replace(
    path: str,
    find: str,
    replace: str,
    count: int | None = None,
) -> dict[str, Any]:
    """Replace text across a .docx. Writes to a copy named `<basename>.edited.docx`.

    The original file is NEVER modified — you always operate on a copy.
    If the copy already exists from a previous edit, this writes to
    that same copy (chained edits land in one file).

    Args:
        path: .docx to edit. May be the original or its `.edited` copy
              (idempotent: if you pass the edited copy, we write back
              to it).
        find: text to search for (exact substring match).
        replace: text to substitute in.
        count: optional — stop after this many replacements. None =
               replace all.

    Returns:
        dict with `ok`, `original_path`, `edited_path`, `kind`,
        `summary={replaced}`. Use `edited_path` for subsequent reads
        or edits on this document.

    Note on formatting: this tool collapses run-level char formatting
    in each modified paragraph (paragraph-level style is preserved).
    If you need to keep "bold middle word" intact, use
    `office.update_paragraph` instead — it's whole-paragraph anyway.
    """
    resolved, kind, err = _validate_path(path)
    if err is not None:
        return _write_error(None, err)
    assert resolved is not None and kind is not None
    if kind != "word":
        return _write_error(kind, "office.find_replace requires a .docx file", resolved)
    if not find:
        return _write_error(kind, "find text required", resolved)

    target = _ensure_copy_for_write(resolved)
    remaining = count if (count is None or count > 0) else 0

    def _do_write(tmp_path: str) -> dict[str, Any]:
        nonlocal remaining
        doc = DocxDocument(target)
        replaced = 0
        for para in _iter_all_paragraphs(doc):
            if remaining == 0:
                break
            text = para.text
            if find not in text:
                continue
            occurrences = text.count(find)
            if remaining is not None:
                occurrences = min(occurrences, remaining)
            new_text = text.replace(find, replace, occurrences)
            _set_paragraph_text(para, new_text)
            replaced += occurrences
            if remaining is not None:
                remaining -= occurrences
        doc.save(tmp_path)
        return {"replaced": replaced}

    try:
        summary = _atomic_write(target, kind, _do_write)
    except Exception as exc:
        return _write_error(kind, f"write failed: {exc.__class__.__name__}: {exc}", resolved)
    return _write_result(resolved, target, kind, summary)


def _iter_all_paragraphs(doc: DocxDocumentType):
    """Yield body paragraphs + paragraphs nested in table cells."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


@tool("office.insert_paragraph")
def office_insert_paragraph(
    path: str,
    anchor: str,
    content: str,
    position: str = "after",
    style: str | None = None,
) -> dict[str, Any]:
    """Insert a new paragraph relative to an anchor paragraph in a .docx.

    Writes to `<basename>.edited.docx`; original preserved.

    Args:
        path: .docx to edit.
        anchor: text that uniquely identifies the anchor paragraph
                (first paragraph whose text contains this substring).
                Empty string OR the literal "__END__" inserts at the
                very end of the document.
        content: text of the new paragraph.
        position: "before" or "after" the anchor (default "after").
                  Ignored when anchor is "__END__".
        style: optional python-docx style name (e.g. "Heading 1",
               "Normal", "Quote", "List Bullet"). Use `office.outline`
               to see which styles already appear in the document.

    Returns:
        dict with `ok`, `original_path`, `edited_path`, `kind`,
        `summary={inserted_at, style?}`.
    """
    resolved, kind, err = _validate_path(path)
    if err is not None:
        return _write_error(None, err)
    assert resolved is not None and kind is not None
    if kind != "word":
        return _write_error(kind, "office.insert_paragraph requires a .docx file", resolved)
    if position not in {"before", "after"}:
        return _write_error(
            kind, f"position must be 'before' or 'after', got {position!r}", resolved
        )

    target = _ensure_copy_for_write(resolved)

    def _do_write(tmp_path: str) -> dict[str, Any]:
        doc = DocxDocument(target)
        if anchor in ("", "__END__"):
            new_para = (
                doc.add_paragraph(content, style=style) if style else doc.add_paragraph(content)
            )
            doc.save(tmp_path)
            return {"inserted_at": "end", "style": new_para.style.name if new_para.style else None}
        match = _find_paragraph(doc, anchor)
        if match is None:
            raise ValueError(f"anchor not found: {anchor!r}")
        new_para = _insert_paragraph_relative(match, content, position, style)
        doc.save(tmp_path)
        return {
            "inserted_at": f"{position} anchor",
            "anchor_excerpt": match.text[:60],
            "style": new_para.style.name if new_para.style else None,
        }

    try:
        summary = _atomic_write(target, kind, _do_write)
    except Exception as exc:
        return _write_error(kind, f"write failed: {exc.__class__.__name__}: {exc}", resolved)
    return _write_result(resolved, target, kind, summary)


@tool("office.update_paragraph")
def office_update_paragraph(
    path: str,
    target: str,
    content: str,
    style: str | None = None,
) -> dict[str, Any]:
    """Rewrite the first paragraph that contains `target` substring.

    Writes to `<basename>.edited.docx`; original preserved.

    Args:
        path: .docx to edit.
        target: substring that identifies the paragraph to update
                (first match wins; pick something unique).
        content: new full text of that paragraph (replaces ALL existing
                 text in the paragraph).
        style: optional new paragraph style name.

    Returns:
        dict with `ok`, `original_path`, `edited_path`, `kind`,
        `summary={updated_excerpt, style?}`.
    """
    resolved, kind, err = _validate_path(path)
    if err is not None:
        return _write_error(None, err)
    assert resolved is not None and kind is not None
    if kind != "word":
        return _write_error(kind, "office.update_paragraph requires a .docx file", resolved)
    if not target:
        return _write_error(kind, "target text required", resolved)

    target_path = _ensure_copy_for_write(resolved)

    def _do_write(tmp_path: str) -> dict[str, Any]:
        doc = DocxDocument(target_path)
        match = _find_paragraph(doc, target)
        if match is None:
            raise ValueError(f"target paragraph not found: {target!r}")
        old_excerpt = match.text[:60]
        _set_paragraph_text(match, content)
        if style:
            match.style = style
        doc.save(tmp_path)
        return {
            "updated_excerpt": old_excerpt,
            "style": match.style.name if match.style else None,
        }

    try:
        summary = _atomic_write(target_path, kind, _do_write)
    except Exception as exc:
        return _write_error(kind, f"write failed: {exc.__class__.__name__}: {exc}", resolved)
    return _write_result(resolved, target_path, kind, summary)


@tool("office.delete_paragraph")
def office_delete_paragraph(path: str, target: str) -> dict[str, Any]:
    """Delete the first paragraph that contains `target` substring.

    Writes to `<basename>.edited.docx`; original preserved.

    Args:
        path: .docx to edit.
        target: substring that identifies the paragraph (first match wins).

    Returns:
        dict with `ok`, `original_path`, `edited_path`, `kind`,
        `summary={deleted_excerpt}`.
    """
    resolved, kind, err = _validate_path(path)
    if err is not None:
        return _write_error(None, err)
    assert resolved is not None and kind is not None
    if kind != "word":
        return _write_error(kind, "office.delete_paragraph requires a .docx file", resolved)
    if not target:
        return _write_error(kind, "target text required", resolved)

    target_path = _ensure_copy_for_write(resolved)

    def _do_write(tmp_path: str) -> dict[str, Any]:
        doc = DocxDocument(target_path)
        match = _find_paragraph(doc, target)
        if match is None:
            raise ValueError(f"target paragraph not found: {target!r}")
        deleted_excerpt = match.text[:60]
        # Detach the paragraph element from its parent (python-docx has
        # no public `.delete()`; we manipulate XML directly).
        p_element = match._p
        p_element.getparent().remove(p_element)
        doc.save(tmp_path)
        return {"deleted_excerpt": deleted_excerpt}

    try:
        summary = _atomic_write(target_path, kind, _do_write)
    except Exception as exc:
        return _write_error(kind, f"write failed: {exc.__class__.__name__}: {exc}", resolved)
    return _write_result(resolved, target_path, kind, summary)


@tool("office.apply_style")
def office_apply_style(path: str, target: str, style: str) -> dict[str, Any]:
    """Change the paragraph style of the first paragraph matching `target`.

    Writes to `<basename>.edited.docx`; original preserved.

    Args:
        path: .docx to edit.
        target: substring identifying the paragraph (first match wins).
        style: python-docx style name. Common values: "Heading 1",
               "Heading 2", "Heading 3", "Normal", "Quote", "Title",
               "Subtitle", "List Bullet", "List Number".

    Returns:
        dict with `ok`, `original_path`, `edited_path`, `kind`,
        `summary={paragraph_excerpt, old_style, new_style}`.
    """
    resolved, kind, err = _validate_path(path)
    if err is not None:
        return _write_error(None, err)
    assert resolved is not None and kind is not None
    if kind != "word":
        return _write_error(kind, "office.apply_style requires a .docx file", resolved)
    if not target or not style:
        return _write_error(kind, "target and style are both required", resolved)

    target_path = _ensure_copy_for_write(resolved)

    def _do_write(tmp_path: str) -> dict[str, Any]:
        doc = DocxDocument(target_path)
        match = _find_paragraph(doc, target)
        if match is None:
            raise ValueError(f"target paragraph not found: {target!r}")
        old_style = match.style.name if match.style else None
        match.style = style
        doc.save(tmp_path)
        return {
            "paragraph_excerpt": match.text[:60],
            "old_style": old_style,
            "new_style": match.style.name if match.style else None,
        }

    try:
        summary = _atomic_write(target_path, kind, _do_write)
    except Exception as exc:
        return _write_error(kind, f"write failed: {exc.__class__.__name__}: {exc}", resolved)
    return _write_result(resolved, target_path, kind, summary)


# ═══════════════════════════════════════════════════════════════════════
# Stable tool surfaces
# ═══════════════════════════════════════════════════════════════════════

# Phase 1 surface (read-only).
OFFICE_READ_TOOLS = [office_read, office_outline, office_read_range, office_read_slides]

# Phase 2 + Phase 3 surface (writes, all copy-on-first-write — except
# office.create_pptx which produces the original file).
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


# Re-exported for tests that want to construct paths the same way the
# tools do without poking at the private helper.
def edited_copy_path(original_path: str) -> str:
    return _resolve_write_target(original_path)


# Silence unused-import warnings for symbols re-exported by tests.
__all__ = [
    "OFFICE_READ_TOOLS",
    "OFFICE_WRITE_TOOLS",
    "edited_copy_path",
    "get_column_letter",  # tests construct range refs
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
